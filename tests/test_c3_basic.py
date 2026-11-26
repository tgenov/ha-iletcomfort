"""C3 basic status layout for sn8 171H120F (Rotenso Aquami, #124).

Rotenso frames are real captures from 2026-11-17 (EU cloud).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
import requests
from homeassistant.components.climate import HVACMode
from homeassistant.components.water_heater import STATE_HEAT_PUMP
from homeassistant.const import (
    ATTR_TEMPERATURE,
    CONF_EMAIL,
    CONF_PASSWORD,
    STATE_OFF,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort import climate as climate_platform
from custom_components.iletcomfort import switch, water_heater
from custom_components.iletcomfort.api import (
    STATUS_MODE_COOL,
    STATUS_MODE_HEAT,
    STATUS_MODE_OFF,
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSSensors,
    build_c3_frame,
    build_c3_query,
    extract_c3_body,
    parse_hex_response,
)
from custom_components.iletcomfort.c3_basic import (
    C3_MODE_COOL,
    FEATURE_ECO,
    FEATURE_SILENT,
    C3BasicStatus,
    UnsupportedC3Write,
    apply_basic_changes,
    build_basic_set,
    build_feature_set,
    decode_c3_basic,
)
from custom_components.iletcomfort.commands import (
    HeatPumpCommand,
    SetDhwTargetCommand,
    SetEcoModeCommand,
    SetFastDhwCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
    SetZoneCurveCommand,
    SetZonePowerCommand,
    SetZoneTargetCommand,
)
from custom_components.iletcomfort.climate import (
    ILetComfortC3Zone2Climate,
    ILetComfortClimate,
)
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_C3_BASIC_CONTROL,
    CONF_OPERATION_MODE,
    CONF_REGION,
    DOMAIN,
    OPERATION_MODE_PHONE_APP,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.model_profiles import (
    AQUAPURA_SN8,
    ATW_SN8,
    atw_c3_basic,
    decode_atw_status,
)
from custom_components.iletcomfort.protocols import (
    WriteContext,
    WriteRefused,
    get_adapter,
    run_write_plan,
)
from custom_components.iletcomfort.select import ILetComfortMuteSelect
from custom_components.iletcomfort.switch import (
    ILetComfortC3CurveSwitch,
    ILetComfortC3FastDhwSwitch,
    ILetComfortC3FeatureSwitch,
    ILetComfortC3Zone2CurveSwitch,
)
from custom_components.iletcomfort.water_heater import ILetComfortDhw
from tests.write_helpers import sent_commands
from tests.snapshot_helpers import make_state


def _body(csv_frame: str) -> bytes:
    """Body (incl. body-type byte) of a comma-separated cloud response."""
    return bytes(extract_c3_body(parse_hex_response(csv_frame))[1])


# Second Rotenso Aquami: Zone-1 and DHW off, curve on, byte[6]=29 (the app's
# heating setpoint on this unit), byte[9]/2=24, DHW 55 °C, tank 16 °C.
ROTENSO_BASELINE = (
    "aa,23,c3,00,00,00,00,00,00,03,01,08,15,a0,03,03,1d,1e,37,30,37,19,19,05,"
    "37,19,19,05,3c,22,3c,14,10,00,80,97"
)
# The same unit with DHW switched on (device's reply to a DHW-power change).
ROTENSO_DHW_ON = (
    "aa,23,c3,00,00,00,00,00,00,02,01,0c,15,a0,03,03,1d,1e,37,30,37,19,19,05,"
    "37,19,19,05,3c,22,3c,14,10,00,80,94"
)
# Issue #124: heating ON in the app (target reported as 24 °C) while the old
# decode showed "Off".
ROTENSO_ISSUE124_HEATING = bytes.fromhex(
    "01 0d 15 90 03 03 28 1e 32 30 41 23 19 05 37 19 19 05 3c 22 3c 14 32 00 80"
)
# Italtherm (#22) state2: byte[1] bit 0 set, documented as space-heat demand.
ITALTHERM_STATE2 = bytes.fromhex(
    "01 0d 07 68 03 03 1e 28 32 3a 37 19 19 05 41 23 19 05 3c 22 3c 14 2e 00 80"
)
# Kaisai Arctic KHC-10RY3-B (#151): dual-zone water-mode (body[2]=0x3f,
# Z1=35 °C, Z2=25 °C, DHW=44 °C, both zones & curves ON).
KAISAI_DUAL_ZONE = bytes.fromhex(
    "01 1f 3f 00 03 03 23 19 2c 30 37 19 19 05 37 19 19 05 3c 22 3c 14 2c 00 80"
)


def _cooling(frame: bytes) -> bytes:
    body = bytearray(frame)
    body[4] = C3_MODE_COOL
    return bytes(body)


# ---------------------------------------------------------------------------
# Raw decode
# ---------------------------------------------------------------------------

def test_decode_rotenso_baseline():
    basic = decode_c3_basic(_body(ROTENSO_BASELINE))
    assert basic is not None
    assert (basic.zone1_power, basic.dhw_power, basic.zone1_curve) == (False, False, True)
    assert basic.zone1_water_temp
    assert basic.zone1_target == 29
    assert basic.room_target == 24.0
    assert basic.zone1_water_range == (25.0, 55.0)
    assert (basic.zone1_cool_min, basic.zone1_cool_max) == (5, 25)
    assert basic.dhw_target == 55
    assert (basic.dhw_min, basic.dhw_max) == (20, 60)
    assert basic.dhw_tank_temp == 16
    assert not basic.silent_mode and not basic.eco_mode
    assert basic.error_code == 0


def test_cooling_mode_uses_cooling_limits():
    basic = decode_c3_basic(_cooling(_body(ROTENSO_BASELINE)))
    assert basic.is_cooling_mode
    assert basic.zone1_water_range == (5.0, 25.0)


def test_silent_and_eco_bits_in_status_byte3():
    body = bytearray(_body(ROTENSO_BASELINE))
    body[3] = 0xA2
    assert decode_c3_basic(body).silent_mode
    body[3] = 0xA8
    assert decode_c3_basic(body).eco_mode


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\x01" * 10,
        bytes([0x02]) + bytes(30),
        # Auto (1) and unknown modes are not decoded.
        bytes([0x01, 0, 0, 0, 1]) + bytes(20),
        bytes([0x01, 0, 0, 0, 7]) + bytes(20),
    ],
)
def test_decode_rejects_non_basic_frames(body):
    assert decode_c3_basic(body) is None


# ---------------------------------------------------------------------------
# Profile decode (ITSStatus)
# ---------------------------------------------------------------------------

def test_issue124_heating_shows_heat_not_off():
    """The #124 frame has zone1_power=True and body[2] & 0x10 -> body[6] (40 °C)."""
    status = decode_atw_status(ROTENSO_ISSUE124_HEATING)
    assert status.mode == STATUS_MODE_HEAT
    assert status.mode_name == "Heat"
    assert status.c3_basic is not None and status.c3_basic.zone1_power
    assert status.t5s_def == 40.0


def test_rotenso_baseline_status():
    status = decode_atw_status(_body(ROTENSO_BASELINE))
    assert status.mode == STATUS_MODE_OFF
    assert status.mode_name == "Off"
    assert status.t5s_def == 29.0
    assert status.set_temperature == 55
    assert status.box_bottom_temp == 16.0
    assert status.comp_running is None
    assert status.comp_frq is None


def test_rotenso_powered_zone_in_cooling_mode_is_cool_not_heat():
    status = decode_atw_status(_cooling(ROTENSO_ISSUE124_HEATING))
    assert status.mode == STATUS_MODE_COOL
    assert status.mode_name == "Cool"


def test_italtherm_room_mode_decodes_heat_power_cool_and_room_target():
    """Italtherm [2]=0x07 (room-temp mode) reads Zone-1 power and room target body[9]/2."""
    status = decode_atw_status(ITALTHERM_STATE2)
    assert status.c3_basic is not None and status.c3_basic.zone1_power
    assert not status.c3_basic.zone1_water_temp
    assert status.mode == STATUS_MODE_HEAT and status.mode_name == "Heat"
    assert status.t5s_def == 29.0
    assert status.comp_running is None

    cool_status = decode_atw_status(_cooling(ITALTHERM_STATE2))
    assert cool_status.mode == STATUS_MODE_COOL and cool_status.mode_name == "Cool"


def test_atw_c3_basic_is_gated_on_sn8():
    status = decode_atw_status(_body(ROTENSO_BASELINE))
    assert atw_c3_basic(ATW_SN8, status) is not None
    assert atw_c3_basic(None, status) is None
    assert atw_c3_basic(AQUAPURA_SN8, status) is None
    assert atw_c3_basic(ATW_SN8, None) is None


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

def _coordinator(
    hass: HomeAssistant,
    frame: bytes | None = None,
    sn8: str | None = ATW_SN8,
) -> ILetComfortCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Heat pump",
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coordinator = ILetComfortCoordinator(hass, entry)
    coordinator.appliance_meta = {"sn8": sn8} if sn8 else None
    if frame is not None:
        coordinator.data = make_state(decode_atw_status(frame), ITSSensors(), sn8=sn8)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


def test_climate_reports_heat_and_both_setpoint_candidates(hass: HomeAssistant):
    climate = ILetComfortClimate(_coordinator(hass, ROTENSO_ISSUE124_HEATING))
    assert climate.hvac_mode is HVACMode.HEAT
    assert climate.target_temperature == 40.0
    attrs = climate.extra_state_attributes
    assert attrs["zone1_water_target"] == 40.0
    assert attrs["room_target"] == 24.0
    assert attrs["zone1_temp_mode"] == "water"
    assert attrs["weather_curve"] is True


def test_climate_reports_cool_for_powered_zone_in_cooling_mode(hass: HomeAssistant):
    climate = ILetComfortClimate(
        _coordinator(hass, _cooling(ROTENSO_ISSUE124_HEATING))
    )
    assert climate.hvac_mode is HVACMode.COOL


def test_water_heater_reads_dhw_state(hass: HomeAssistant):
    entity = ILetComfortDhw(_coordinator(hass, _body(ROTENSO_BASELINE)))
    assert entity.available
    assert entity.current_operation == STATE_OFF
    assert entity.current_temperature == 16.0
    assert entity.target_temperature == 55.0
    assert (entity.min_temp, entity.max_temp) == (20.0, 60.0)
    assert entity.supported_features == 0

    on = ILetComfortDhw(_coordinator(hass, _body(ROTENSO_DHW_ON)))
    assert on.current_operation == STATE_HEAT_PUMP


async def test_water_heater_added_once_model_is_known(hass: HomeAssistant):
    """sn8/status unknown at setup (first start): added on a later update."""
    coordinator = _coordinator(hass, frame=None, sn8=None)
    added: list = []

    await water_heater.async_setup_entry(hass, coordinator.entry, added.extend)
    assert added == []

    coordinator.appliance_meta = {"sn8": ATW_SN8}
    coordinator.data = make_state(decode_atw_status(_body(ROTENSO_BASELINE)), ITSSensors(), sn8=ATW_SN8)
    assert len(coordinator._listeners) == 1
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()

    assert [e.unique_id for e in added] == ["APPL1_dhw"]
    assert coordinator._listeners == {}  # unsubscribed once added


@pytest.mark.parametrize(("low", "high"), [(0, 0), (0, 60), (60, 20)])
def test_water_heater_ignores_zeroed_or_inverted_limits(hass: HomeAssistant, low, high):
    body = bytearray(_body(ROTENSO_BASELINE))
    body[21], body[20] = low, high  # DHW min, max
    entity = ILetComfortDhw(_coordinator(hass, bytes(body)))
    default = ILetComfortDhw(_coordinator(hass, None, sn8=None))

    assert (entity.min_temp, entity.max_temp) == (default.min_temp, default.max_temp)


async def test_water_heater_not_added_for_other_models(hass: HomeAssistant):
    coordinator = _coordinator(hass, _body(ROTENSO_BASELINE), sn8=AQUAPURA_SN8)
    added: list = []

    await water_heater.async_setup_entry(hass, coordinator.entry, added.extend)
    coordinator.async_update_listeners()

    assert added == []


# ===========================================================================
# Control (opt-in): standard Midea C3 SET
# ===========================================================================

ROTENSO_SILENCE_QUERY = bytes.fromhex("05 00 0c 00 0f 00 16 00 07 00")
ROTENSO_ECO_QUERY = bytes.fromhex("07 00 08 00 13 00")
SILENCE_REPLY = "aa,14,c3,00,00,00,00,00,00,03,05,00,0c,00,0f,00,16,00,07,00,e9"
SET_ACK_SILENCE = "aa,0b,c3,00,00,00,00,00,00,02,05,2b"
# Baseline reply with byte[4] (mode) switched to Cool.
ROTENSO_COOLING_REPLY = ROTENSO_BASELINE.replace(",a0,03,03,", ",a0,02,03,", 1)

# The exact SET frames the hardware accepted (each sent once, then reverted).
SET_DHW_54 = "aa12c3000000000000020100031d1e36300183"
SET_RESTORE = "aa12c3000000000000020100031d1e37300182"
SET_CURVE_OFF = "aa12c3000000000000020100031d1e37300083"
SET_ZONE1_28 = "aa12c3000000000000020100031c1e37300183"
SET_DHW_ON = "aa12c3000000000000020104031d1e3730017e"
SET_SILENT_ON = "aa14c30000000000000205010c000f0016000700e9"
SET_SILENT_OFF = "aa14c30000000000000205000c000f0016000700ea"
SET_ECO_ON = "aa11c3000000000000020701080013000007"
SET_ECO_OFF = "aa11c3000000000000020700080013000008"


def _rotenso_set(
    *,
    power: int = 0x00,
    mode: int = 0x03,
    zone1: int = 0x1D,
    dhw: int = 0x37,
    flags: int = 0x01,
) -> str:
    """Basic SET for the Rotenso baseline with selected fields changed.

    With no arguments it equals the hardware-accepted ``SET_RESTORE`` frame.
    """
    return build_c3_frame(
        0x02, bytes([0x01, power, mode, zone1, 0x1E, dhw, 0x30, flags])
    )


def _kaisai_set(
    *, power: int = 0x07, zone2: int = 0x19, flags: int = 0x03
) -> str:
    """Basic SET written back from the Kaisai dual-zone frame (#151)."""
    return build_c3_frame(
        0x02, bytes([0x01, power, 0x03, 0x23, zone2, 0x2C, 0x30, flags])
    )


def _baseline() -> C3BasicStatus:
    basic = decode_c3_basic(_body(ROTENSO_BASELINE))
    assert basic is not None
    return basic


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ({"dhw_target": 54}, SET_DHW_54),
        ({"dhw_target": 55}, SET_RESTORE),
        ({"zone1_curve": False}, SET_CURVE_OFF),
        ({"zone1_water_target": 28}, SET_ZONE1_28),
        ({"dhw_power": True}, SET_DHW_ON),
        ({}, SET_RESTORE),
    ],
)
def test_basic_set_matches_hardware_validated_frames(changes, expected):
    assert build_basic_set(apply_basic_changes(_baseline(), **changes)) == expected


def test_zone1_power_sets_only_bit0():
    frame = bytes.fromhex(
        build_basic_set(apply_basic_changes(_baseline(), zone1_power=True))
    )
    assert frame[11] == 0x01
    assert frame[12:18] == bytes.fromhex(SET_RESTORE)[12:18]


@pytest.mark.parametrize(
    ("body_type", "expected"),
    [
        # Byte-identical to the pre-refactor builder's output.
        (0x01, "aa0bc300000000000003012e"),
        (0x02, "aa0bc300000000000003022d"),
        (0x05, "aa0bc300000000000003052a"),
        (0x07, "aa0bc3000000000000030728"),
    ],
)
def test_query_builder_is_the_shared_frame_builder(body_type, expected):
    assert build_c3_query(body_type) == expected
    assert build_c3_frame(0x03, bytes([body_type])) == expected


@pytest.mark.parametrize(
    "changes", [{"dhw_target": 61}, {"dhw_target": 19}, {"zone1_water_target": 56}]
)
def test_out_of_range_is_rejected(changes):
    with pytest.raises(ValueError):
        apply_basic_changes(_baseline(), **changes)


def test_zeroed_or_inverted_bounds_reject_setpoint_writes():
    body = bytearray(_body(ROTENSO_BASELINE))
    body[21], body[20] = 0, 0
    zeroed = decode_c3_basic(body)
    assert zeroed is not None
    with pytest.raises(ValueError):
        apply_basic_changes(zeroed, dhw_target=50)

    body = bytearray(_body(ROTENSO_BASELINE))
    body[11], body[10] = 55, 25
    inverted = decode_c3_basic(body)
    assert inverted is not None
    with pytest.raises(ValueError):
        apply_basic_changes(inverted, zone1_water_target=35)


@pytest.mark.parametrize(
    "changes",
    [{"zone1_power": True}, {"zone1_water_target": 20}, {"zone1_curve": False}],
)
def test_zone1_writes_refused_in_cooling_mode(changes):
    """A Heat request must never power on (or tune) an existing Cool mode."""
    cooling = decode_c3_basic(_cooling(_body(ROTENSO_BASELINE)))
    assert cooling is not None
    with pytest.raises(UnsupportedC3Write):
        apply_basic_changes(cooling, **changes)


def test_cooling_mode_still_allows_zone1_off_and_dhw():
    cooling = decode_c3_basic(_cooling(_body(ROTENSO_BASELINE)))
    assert cooling is not None
    frame = bytes.fromhex(
        build_basic_set(apply_basic_changes(cooling, zone1_power=False, dhw_target=50))
    )
    assert frame[12] == C3_MODE_COOL  # mode written back unchanged
    assert frame[11] & 0x01 == 0


def test_room_temperature_setpoint_write_and_mode_guards():
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x10  # room-temperature control
    room_status = decode_c3_basic(body)
    assert room_status is not None

    # Water target is refused in room-temperature mode.
    with pytest.raises(UnsupportedC3Write):
        apply_basic_changes(room_status, zone1_water_target=30)

    # Room target (0.5 °C resolution) is encoded as raw * 2 at body[6] (frame[16]).
    updated = apply_basic_changes(room_status, room_target=22.5)
    assert updated.room_target == 22.5
    frame = bytes.fromhex(build_basic_set(updated))
    assert frame[16] == 45  # 22.5 * 2 == 0x2d

    # Room target is refused in water-temperature mode.
    with pytest.raises(UnsupportedC3Write):
        apply_basic_changes(_baseline(), room_target=22.0)

    # Mutual exclusion of water and room targets.
    with pytest.raises(UnsupportedC3Write, match="Cannot set Zone-1 water and room"):
        apply_basic_changes(_baseline(), zone1_water_target=30, room_target=22.0)

    # Room target out of bounds is rejected.
    with pytest.raises(ValueError, match="outside"):
        apply_basic_changes(room_status, room_target=40.0)


def test_dhw_capability_guard_and_fast_dhw_write():
    # Fast DHW sets bit 3 (0x08) in SET flags byte (frame[17]).
    frame = bytes.fromhex(
        build_basic_set(apply_basic_changes(_baseline(), fast_dhw=True))
    )
    assert frame[17] & 0x08 == 0x08

    # Without cap_dhw, all DHW writes are refused.
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x04
    no_dhw = decode_c3_basic(body)
    assert no_dhw is not None
    for kwargs in ({"dhw_power": True}, {"dhw_target": 50}, {"fast_dhw": True}):
        with pytest.raises(UnsupportedC3Write, match="DHW"):
            apply_basic_changes(no_dhw, **kwargs)


def test_build_feature_set_explicit_silent_levels_preserve_schedule():
    # Airwell #139 C8 capture: Super Silent bit (0x08) retained even when OFF.
    airwell_off_super = bytes.fromhex("05 08 06 1e 09 1e 16 00 07 00")

    # Level 1 must set bit 0 and explicitly clear bit 3 (0x08 -> 0x01).
    lvl1 = bytes.fromhex(build_feature_set(airwell_off_super, True, silent_level=1))
    assert lvl1[10] == 0x05
    assert lvl1[11] == 0x01
    assert lvl1[12:20] == airwell_off_super[2:]

    # Level 2 sets both bit 0 and bit 3 (0x08 -> 0x09).
    lvl2 = bytes.fromhex(build_feature_set(airwell_off_super, True, silent_level=2))
    assert lvl2[11] == 0x09
    assert lvl2[12:20] == airwell_off_super[2:]

    # Level 0 clears bit 0 while keeping bit 3 & schedule intact (0x09 -> 0x08).
    airwell_on_super = bytes.fromhex("05 09 06 1e 09 1e 16 00 07 00")
    lvl0 = bytes.fromhex(build_feature_set(airwell_on_super, False, silent_level=0))
    assert lvl0[11] == 0x08
    assert lvl0[12:20] == airwell_on_super[2:]

    # Invalid silent_level, mismatched on/silent_level, or ECO with silent_level is rejected.
    with pytest.raises(ValueError, match="silent_level"):
        build_feature_set(airwell_off_super, True, silent_level=3)
    with pytest.raises(ValueError, match="inconsistent"):
        build_feature_set(airwell_off_super, True, silent_level=0)
    with pytest.raises(ValueError, match="inconsistent"):
        build_feature_set(airwell_off_super, False, silent_level=1)
    with pytest.raises(ValueError, match="silent_level"):
        build_feature_set(ROTENSO_ECO_QUERY, True, silent_level=1)


def test_atw_dialect_refuses_legacy_write_without_sending():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(ApiError, match="171H120F"):
        _run(client, ATW_SN8, SetTargetTemperatureCommand(40), opt_in=False)
    client.send_hex_command.assert_not_called()


@pytest.mark.parametrize(
    ("query", "on", "expected"),
    [
        (ROTENSO_SILENCE_QUERY, True, SET_SILENT_ON),
        (ROTENSO_SILENCE_QUERY, False, SET_SILENT_OFF),
        (ROTENSO_ECO_QUERY, True, SET_ECO_ON),
        (ROTENSO_ECO_QUERY, False, SET_ECO_OFF),
    ],
)
def test_feature_set_keeps_schedule(query, on, expected):
    assert build_feature_set(query, on) == expected


@pytest.mark.parametrize(
    "query",
    [
        b"\x05",  # subtype only
        ROTENSO_SILENCE_QUERY[:-1],  # one schedule byte missing
        b"\x07",
        ROTENSO_ECO_QUERY[:-1],
    ],
)
def test_feature_set_rejects_incomplete_replies(query):
    with pytest.raises(ValueError, match="incomplete"):
        build_feature_set(query, True)


def test_feature_set_rejects_other_bodies():
    with pytest.raises(ValueError):
        build_feature_set(_body(ROTENSO_BASELINE), True)


def test_feature_set_preserves_upper_bits_and_accepts_full_eco_payload():
    silence_upper = bytearray(ROTENSO_SILENCE_QUERY)
    silence_upper[1] = 0x08
    frame = bytes.fromhex(build_feature_set(silence_upper, True))
    assert frame[11] == 0x09

    eco_six_bytes = ROTENSO_ECO_QUERY + b"\x2a"
    eco_frame = bytes.fromhex(build_feature_set(eco_six_bytes, True))
    assert eco_frame[10:17] == bytes([0x07, 0x01, 0x08, 0x00, 0x13, 0x00, 0x2A])


# --- dialect plan over the client transport ---------------------------------

def _run(
    client: ILetComfortClient,
    sn8: str | None,
    command: HeatPumpCommand,
    *,
    opt_in: bool = True,
    status_body: bytes | None = None,
) -> dict:
    """Plan ``command`` in the sn8's dialect and run it over ``client``."""
    body = _body(ROTENSO_BASELINE) if status_body is None else status_body
    ctx = WriteContext(status=decode_atw_status(body), c3_basic_control=opt_in)
    plan = get_adapter(sn8).plan_write(command, ctx)
    return run_write_plan(plan, lambda command_hex: client.send_hex_command("APPL1", command_hex))


def test_c3_basic_write_reads_then_writes_one_change():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(  # type: ignore[method-assign]
        side_effect=[ROTENSO_BASELINE, ROTENSO_DHW_ON]
    )
    result = _run(client, ATW_SN8, SetDhwTargetCommand(54))
    sent = [c.args[1] for c in client.send_hex_command.call_args_list]
    assert sent == [build_c3_query(0x01), SET_DHW_54]
    assert result["sent"] == SET_DHW_54


@pytest.mark.parametrize("sn8", [None, AQUAPURA_SN8, "17100003"])
@pytest.mark.parametrize(
    "command",
    [
        SetFastDhwCommand(enabled=True),
        SetZoneCurveCommand(zone=1, enabled=False),
        SetSilentModeCommand(enabled=True),
        SetEcoModeCommand(enabled=True),
    ],
)
def test_c3_writes_refused_for_other_models(sn8, command):
    client = ILetComfortClient()
    client.send_hex_command = MagicMock()  # type: ignore[method-assign]
    with pytest.raises(WriteRefused, match="not enabled"):
        _run(client, sn8, command)
    client.send_hex_command.assert_not_called()


def test_c3_write_refuses_unknown_layout():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(  # type: ignore[method-assign]
        return_value="aa,10,c3,00,00,00,00,00,00,03,01,08,15,a0,03,07,1d,1e"
    )
    with pytest.raises(ApiError):
        _run(client, ATW_SN8, SetDhwTargetCommand(54))
    assert client.send_hex_command.call_count == 1


def test_c3_heat_request_on_cooling_unit_sends_nothing():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(  # type: ignore[method-assign]
        return_value=ROTENSO_COOLING_REPLY
    )
    with pytest.raises(UnsupportedC3Write):
        _run(client, ATW_SN8, SetZonePowerCommand(zone=1, on=True))
    assert client.send_hex_command.call_count == 1  # the status query only


def test_c3_feature_write_copies_schedule():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(  # type: ignore[method-assign]
        side_effect=[SILENCE_REPLY, SET_ACK_SILENCE]
    )
    _run(client, ATW_SN8, SetSilentModeCommand(enabled=True))
    assert client.send_hex_command.call_args_list[1].args[1] == SET_SILENT_ON


@pytest.mark.parametrize(
    "reply",
    [
        "aa,0b,c3,00,00,00,00,00,00,03,05,2b",  # stub: subtype only
        "aa,0f,c3,00,00,00,00,00,00,03,05,00,0c,00,0f,00",  # truncated schedule
    ],
)
def test_c3_feature_incomplete_reply_sends_no_set(reply):
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(return_value=reply)  # type: ignore[method-assign]
    with pytest.raises(ValueError):
        _run(client, ATW_SN8, SetSilentModeCommand(enabled=True))
    assert client.send_hex_command.call_count == 1  # the query only


# --- coordinator -------------------------------------------------------------

def _control_coordinator(
    hass: HomeAssistant, enabled: bool = True
) -> ILetComfortCoordinator:
    coordinator = _coordinator(hass, _body(ROTENSO_BASELINE))
    coordinator._c3_basic_control = enabled
    coordinator.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
    coordinator._async_login = AsyncMock()  # type: ignore[method-assign]
    return coordinator


async def test_c3_write_requires_opt_in(hass: HomeAssistant):
    coordinator = _control_coordinator(hass, enabled=False)
    with pytest.raises(HomeAssistantError, match="not enabled"):
        await coordinator.async_send_command(SetDhwTargetCommand(54))
    coordinator.client.send_hex_command.assert_not_called()  # type: ignore[attr-defined]


async def test_c3_write_reads_fresh_then_sets_and_refreshes(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    send = coordinator.client.send_hex_command  # type: ignore[attr-defined]
    send.side_effect = [ROTENSO_BASELINE, ROTENSO_DHW_ON]
    await coordinator.async_send_command(SetDhwTargetCommand(54))
    assert send.call_args_list == [
        call("APPL1", build_c3_query(0x01)),
        call("APPL1", SET_DHW_54),
    ]
    coordinator.async_request_refresh.assert_awaited_once()  # type: ignore[attr-defined]


async def test_c3_feature_write_reauths_once_and_rereads(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    send = coordinator.client.send_hex_command  # type: ignore[attr-defined]
    eco_reply = "aa,10,c3,00,00,00,00,00,00,03,07,00,08,00,13,00,00"
    send.side_effect = [AuthError("evicted"), eco_reply, "ack"]
    await coordinator.async_send_command(SetEcoModeCommand(enabled=True))
    coordinator._async_login.assert_awaited_once()  # type: ignore[attr-defined]
    # The retry repeats the fresh read before encoding the SET.
    assert [c.args[1] for c in send.call_args_list] == [
        build_c3_query(0x07),
        build_c3_query(0x07),
        SET_ECO_ON,
    ]


async def test_c3_write_blocked_in_phone_app_mode(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator._operation_mode = OPERATION_MODE_PHONE_APP
    with pytest.raises(HomeAssistantError, match="HA primary"):
        await coordinator.async_send_command(SetZonePowerCommand(zone=1, on=True))
    coordinator.client.send_hex_command.assert_not_called()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "error",
    [
        ApiError("code=1214"),
        ValueError("out of range"),
        UnsupportedC3Write("cool"),
        requests.Timeout("timeout"),
    ],
)
async def test_c3_write_errors_surface_in_ha(hass: HomeAssistant, error):
    coordinator = _control_coordinator(hass)
    coordinator.client.send_hex_command.side_effect = error  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coordinator.async_send_command(SetDhwTargetCommand(54))
    coordinator.async_request_refresh.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("reply", "command"),
    [
        (ROTENSO_COOLING_REPLY, SetZonePowerCommand(zone=1, on=True)),
        (ROTENSO_BASELINE, SetDhwTargetCommand(70)),
        ("aa,0b,c3,00,00,00,00,00,00,03,05,2b", SetSilentModeCommand(enabled=True)),
    ],
)
async def test_c3_refusals_after_fresh_read_surface_in_ha(
    hass: HomeAssistant, reply, command
):
    coordinator = _control_coordinator(hass)
    send = coordinator.client.send_hex_command  # type: ignore[attr-defined]
    send.return_value = reply
    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coordinator.async_send_command(command)
    assert send.call_count == 1  # the fresh read only; no SET
    coordinator.async_request_refresh.assert_not_awaited()  # type: ignore[attr-defined]


async def test_legacy_write_uses_the_shared_write_path(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.appliance_meta = None  # STANDARD: legacy ITS SET
    coordinator.client.send_hex_command.side_effect = ApiError("code=1214")  # type: ignore[attr-defined]
    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coordinator.async_send_command(SetTargetTemperatureCommand(40))
    coordinator.async_request_refresh.assert_not_awaited()  # type: ignore[attr-defined]


class _FakeDevice:
    """Thread-safe fake unit that answers queries and applies basic SETs.

    The first transaction waits (bounded) after its status read until a second
    transaction has also read. Without serialisation both read the same old
    status and the later SET silently undoes the earlier change.
    """

    def __init__(self) -> None:
        self.body = bytearray(_body(ROTENSO_BASELINE))
        self.lock = threading.Lock()
        self.reads = 0
        self.second_read = threading.Event()
        self.sets: list[bytes] = []

    def send_hex_command(self, _code: str, command_hex: str) -> str:
        frame = bytes.fromhex(command_hex)
        with self.lock:
            if frame[9] == 0x02:  # basic SET: [power, mode, z1, z2, dhw, room, flags]
                self.sets.append(frame)
                self.body[6] = frame[13]
                self.body[8] = frame[15]
                return "aa,0b,c3,00,00,00,00,00,00,02,01,00"
            self.reads += 1
            first_read = self.reads == 1
            reply = (
                "aa,23,c3,00,00,00,00,00,00,03,"
                + ",".join(f"{b:02x}" for b in self.body)
                + ",00"
            )
        if first_read:
            self.second_read.wait(timeout=0.5)
        else:
            self.second_read.set()
        return reply


async def test_overlapping_c3_writes_keep_both_changes(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    device = _FakeDevice()
    client = ILetComfortClient()
    client.send_hex_command = device.send_hex_command  # type: ignore[method-assign]
    coordinator.client = client

    await asyncio.gather(
        coordinator.async_send_command(SetDhwTargetCommand(54)),
        coordinator.async_send_command(SetZoneTargetCommand(zone=1, temperature=28)),
    )

    assert len(device.sets) == 2
    assert device.body[8] == 54  # DHW change survived
    assert device.body[6] == 28  # Zone-1 change survived


# --- entities ----------------------------------------------------------------

async def test_climate_control_heat_off_and_setpoint(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    climate = ILetComfortClimate(coordinator)

    assert climate.hvac_modes == [HVACMode.OFF, HVACMode.HEAT]
    assert climate.hvac_mode is HVACMode.OFF
    # With control enabled the climate shows the field it writes.
    assert climate.target_temperature == 29.0
    assert (climate.min_temp, climate.max_temp) == (25.0, 55.0)

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    await climate.async_turn_on()
    await climate.async_turn_off()
    await climate.async_set_temperature(**{ATTR_TEMPERATURE: 99})
    assert sent_commands(coordinator, _body(ROTENSO_BASELINE)) == [
        _rotenso_set(power=0x01),
        _rotenso_set(power=0x01),
        SET_RESTORE,
        _rotenso_set(zone1=55),
    ]
    with pytest.raises(HomeAssistantError):
        await climate.async_set_hvac_mode(HVACMode.COOL)
    assert coordinator.async_execute_plan.await_count == 4


async def test_climate_control_reports_cooling_with_cooling_limits(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.data = make_state(decode_atw_status(_cooling(ROTENSO_ISSUE124_HEATING)), coordinator.data.sensors, sn8=ATW_SN8)
    climate = ILetComfortClimate(coordinator)

    assert climate.hvac_mode is HVACMode.COOL
    assert HVACMode.COOL in climate.hvac_modes
    assert (climate.min_temp, climate.max_temp) == (5.0, 25.0)


async def test_climate_control_room_mode_and_invalid_bounds(hass: HomeAssistant):
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x10  # room-temperature mode
    body[19], body[18] = 0, 0  # invalid/zeroed room limits
    coordinator = _control_coordinator(hass)
    coordinator.data = make_state(decode_atw_status(bytes(body)), coordinator.data.sensors, sn8=ATW_SN8)
    climate = ILetComfortClimate(coordinator)

    assert climate.target_temperature == 24.0
    assert climate.target_temperature_step == 0.5
    assert (climate.min_temp, climate.max_temp) == (10.0, 40.0)


async def test_climate_heat_request_on_cooling_unit_is_refused_end_to_end(
    hass: HomeAssistant,
):
    coordinator = _control_coordinator(hass)
    coordinator.data = make_state(decode_atw_status(_cooling(_body(ROTENSO_BASELINE))), coordinator.data.sensors, sn8=ATW_SN8)
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(return_value=ROTENSO_COOLING_REPLY)  # type: ignore[method-assign]
    coordinator.client = client

    with pytest.raises(HomeAssistantError, match="Heat mode"):
        await ILetComfortClimate(coordinator).async_set_hvac_mode(HVACMode.HEAT)
    assert client.send_hex_command.call_count == 1  # no SET sent

    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    await ILetComfortClimate(coordinator).async_set_hvac_mode(HVACMode.OFF)
    assert sent_commands(coordinator, _cooling(_body(ROTENSO_BASELINE))) == [
        _rotenso_set(mode=C3_MODE_COOL)
    ]


async def test_climate_room_mode_set_temperature_writes_room_target(
    hass: HomeAssistant,
):
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x10  # room-temperature control
    coordinator = _control_coordinator(hass)
    coordinator.data = make_state(decode_atw_status(bytes(body)), coordinator.data.sensors, sn8=ATW_SN8)
    client = ILetComfortClient()
    reply = (
        "aa,23,c3,00,00,00,00,00,00,03,"
        + ",".join(f"{b:02x}" for b in body)
        + ",00"
    )
    client.send_hex_command = MagicMock(  # type: ignore[method-assign]
        side_effect=[reply, "aa,0b,c3,00,00,00,00,00,00,02,01,00"]
    )
    coordinator.client = client

    await ILetComfortClimate(coordinator).async_set_temperature(
        **{ATTR_TEMPERATURE: 22.5}
    )
    assert client.send_hex_command.call_count == 2
    sent = bytes.fromhex(client.send_hex_command.call_args_list[1].args[1])
    assert sent[9] == 0x02  # MSG_SET
    assert sent[16] == 45  # 22.5 * 2


async def test_climate_without_opt_in_shows_read_only_c3_basic_state(
    hass: HomeAssistant,
):
    coordinator = _control_coordinator(hass, enabled=False)
    climate = ILetComfortClimate(coordinator)
    assert climate.target_temperature == 29.0
    assert climate.hvac_modes == [HVACMode.OFF, HVACMode.HEAT]
    assert HVACMode.FAN_ONLY not in climate.hvac_modes
    assert (climate.min_temp, climate.max_temp) == (25.0, 55.0)
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    # Opt-in disabled refuses writes cleanly instead of sending legacy 62-byte ITS SET.
    with pytest.raises(HomeAssistantError, match="disabled by default"):
        await climate.async_set_hvac_mode(HVACMode.HEAT)
    coordinator.async_execute_plan.assert_not_awaited()
    coordinator.client.send_hex_command.assert_not_called()  # type: ignore[attr-defined]


async def test_water_heater_writes_only_with_opt_in(hass: HomeAssistant):
    read_only = ILetComfortDhw(_control_coordinator(hass, enabled=False))
    assert read_only.supported_features == 0

    coordinator = _control_coordinator(hass)
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    entity = ILetComfortDhw(coordinator)
    assert entity.supported_features != 0
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 54})
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 99})
    await entity.async_set_operation_mode(STATE_HEAT_PUMP)
    await entity.async_set_operation_mode(STATE_OFF)
    await entity.async_turn_on()
    await entity.async_turn_off()
    assert sent_commands(coordinator, _body(ROTENSO_BASELINE)) == [
        SET_DHW_54,
        _rotenso_set(dhw=60),
        SET_DHW_ON,
        SET_RESTORE,
        SET_DHW_ON,
        SET_RESTORE,
    ]
    with pytest.raises(HomeAssistantError):
        await entity.async_set_operation_mode("electric")


async def test_switches(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    fresh = {0x01: _body(ROTENSO_BASELINE), 0x05: ROTENSO_SILENCE_QUERY}

    curve = ILetComfortC3CurveSwitch(coordinator)
    assert curve.is_on is True
    await curve.async_turn_off()
    assert sent_commands(coordinator, fresh) == [SET_CURVE_OFF]

    silent = ILetComfortC3FeatureSwitch(
        coordinator,
        FEATURE_SILENT,
        "Silent Mode",
        "mdi:volume-off",
        lambda b: b.silent_mode,
        status_state_fn=lambda s: s.atw_silent_mode,
    )
    assert silent.unique_id == "APPL1_silent_mode"
    assert silent.is_on is False
    await silent.async_turn_on()
    assert sent_commands(coordinator, fresh)[-1] == SET_SILENT_ON

    # ECO switch reads live 0x07 atw_eco_mode even when 0x01 basic.eco_mode is False.
    eco = ILetComfortC3FeatureSwitch(
        coordinator,
        FEATURE_ECO,
        "ECO Mode",
        "mdi:leaf",
        lambda b: b.eco_mode,
        status_state_fn=lambda s: s.atw_eco_mode,
    )
    assert eco.is_on is False
    coordinator.data.status.atw_eco_mode = True
    assert eco.is_on is True

    fast_dhw = ILetComfortC3FastDhwSwitch(coordinator)
    assert fast_dhw.unique_id == "APPL1_fast_dhw"
    assert fast_dhw.is_on is False
    await fast_dhw.async_turn_on()
    await fast_dhw.async_turn_off()
    assert sent_commands(coordinator, fresh)[-2:] == [
        _rotenso_set(flags=0x09),
        SET_RESTORE,
    ]


async def test_mute_select_routes_to_c3_feature_when_opt_in(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    select = ILetComfortMuteSelect(coordinator)

    await select.async_select_option("Level 1")
    await select.async_select_option("Level 2")
    await select.async_select_option("Off")
    plans = [c.args[0] for c in coordinator.async_execute_plan.await_args_list]
    assert {plan.fresh_read.subtype for plan in plans} == {0x05}  # never legacy ITS
    # Encoded from a Super-Silent reply: Level 1 clears bit 3, Level 2 keeps
    # it, Off clears only bit 0; the schedule bytes are copied unchanged.
    super_silent = bytes([0x05, 0x09]) + ROTENSO_SILENCE_QUERY[2:]
    schedule = ROTENSO_SILENCE_QUERY[2:]
    assert sent_commands(coordinator, super_silent) == [
        build_c3_frame(0x02, bytes([0x05, 0x01]) + schedule),
        build_c3_frame(0x02, bytes([0x05, 0x09]) + schedule),
        build_c3_frame(0x02, bytes([0x05, 0x08]) + schedule),
    ]

    # Without opt-in, legacy set_device refuses ATW without sending any command.
    read_only = _control_coordinator(hass, enabled=False)
    client = ILetComfortClient()
    client.send_hex_command = MagicMock()  # type: ignore[method-assign]
    read_only.client = client
    with pytest.raises(HomeAssistantError, match="171H120F"):
        await ILetComfortMuteSelect(read_only).async_select_option("Level 1")
    client.send_hex_command.assert_not_called()


async def test_c3_switches_added_only_with_opt_in(hass: HomeAssistant):
    coordinator = _control_coordinator(hass, enabled=False)
    added: list = []
    await switch.async_setup_entry(hass, coordinator.entry, added.extend)
    assert [e.unique_id for e in added] == ["APPL1_boost"]
    assert coordinator._listeners == {}

    opt_in = _coordinator(hass, frame=None, sn8=None)
    opt_in._c3_basic_control = True
    added_opt_in: list = []
    await switch.async_setup_entry(hass, opt_in.entry, added_opt_in.extend)
    assert [e.unique_id for e in added_opt_in] == ["APPL1_boost"]
    # Two listeners while sn8 is unknown: KJRH-120L Zone-1 and C3 switches.
    assert len(opt_in._listeners) == 2

    opt_in.appliance_meta = {"sn8": ATW_SN8}
    opt_in.data = make_state(decode_atw_status(_body(ROTENSO_BASELINE)), ITSSensors(), sn8=ATW_SN8)
    opt_in.async_update_listeners()
    opt_in.async_update_listeners()
    assert sorted(e.unique_id for e in added_opt_in) == [
        "APPL1_boost",
        "APPL1_eco_mode",
        "APPL1_fast_dhw",
        "APPL1_silent_mode",
        "APPL1_zone1_curve",
    ]
    assert opt_in._listeners == {}


async def test_c3_switches_omit_fast_dhw_when_unit_lacks_cap_dhw(hass: HomeAssistant):
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x04  # no DHW capability
    coordinator = _coordinator(hass, bytes(body))
    coordinator._c3_basic_control = True
    added: list = []
    await switch.async_setup_entry(hass, coordinator.entry, added.extend)
    assert sorted(e.unique_id for e in added) == [
        "APPL1_boost",
        "APPL1_eco_mode",
        "APPL1_silent_mode",
        "APPL1_zone1_curve",
    ]


# --- options flow --------------------------------------------------------------

async def test_options_flow_offers_c3_control_for_171h120f(hass: HomeAssistant):
    coordinator = _coordinator(hass, _body(ROTENSO_BASELINE))
    entry = coordinator.entry

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k.schema): k for k in result["data_schema"].schema}
    assert keys[CONF_C3_BASIC_CONTROL].default() is False

    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_OPERATION_MODE: "ha_primary", CONF_C3_BASIC_CONTROL: True},
    )
    assert entry.options[CONF_C3_BASIC_CONTROL] is True


async def test_options_flow_hides_c3_control_for_other_models(hass: HomeAssistant):
    coordinator = _coordinator(hass, _body(ROTENSO_BASELINE), sn8=AQUAPURA_SN8)

    result = await hass.config_entries.options.async_init(coordinator.entry.entry_id)
    keys = {str(k.schema) for k in result["data_schema"].schema}
    assert CONF_C3_BASIC_CONTROL not in keys


async def test_options_flow_preserves_c3_control_when_hidden(hass: HomeAssistant):
    coordinator = _coordinator(hass, frame=None, sn8=None)
    entry = coordinator.entry
    hass.config_entries.async_update_entry(
        entry, options={CONF_C3_BASIC_CONTROL: True}
    )

    result = await hass.config_entries.options.async_init(entry.entry_id)
    keys = {str(k.schema) for k in result["data_schema"].schema}
    assert CONF_C3_BASIC_CONTROL not in keys

    await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_OPERATION_MODE: "ha_primary"},
    )
    assert entry.options[CONF_C3_BASIC_CONTROL] is True


# ===========================================================================
# Dual-zone ATW (Zone 2, #151 Kaisai Arctic KHC-10RY3-B)
# ===========================================================================

def test_decode_and_write_kaisai_dual_zone():
    basic = decode_c3_basic(KAISAI_DUAL_ZONE)
    assert basic is not None
    assert basic.double_zone is True
    assert basic.zone2_water_temp is True
    assert basic.zone2_power is True
    assert basic.zone2_curve is True
    assert basic.zone2_target == 25
    assert basic.zone2_water_range == (25.0, 55.0)
    assert (basic.zone2_cool_min, basic.zone2_cool_max) == (5, 25)

    # Zone-2 power off clears bit 1 (0x02) in SET power byte (frame[11]),
    # Zone-2 target writes frame[14], Zone-2 curve clears bit 1 (0x02) in frame[17].
    updated = apply_basic_changes(
        basic, zone2_power=False, zone2_water_target=30, zone2_curve=False
    )
    frame = bytes.fromhex(build_basic_set(updated))
    assert frame[11] & 0x02 == 0
    assert frame[11] & 0x01 == 0x01  # Zone 1 untouched
    assert frame[14] == 30
    assert frame[17] & 0x02 == 0
    assert frame[17] & 0x01 == 0x01  # Zone-1 curve untouched

    # Single-zone unit refuses all Zone-2 writes.
    for kwargs in (
        {"zone2_power": True},
        {"zone2_water_target": 30},
        {"zone2_curve": False},
    ):
        with pytest.raises(UnsupportedC3Write, match="Zone-2"):
            apply_basic_changes(_baseline(), **kwargs)

    # Cool mode refuses Zone-2 power-on / target / curve, but allows Zone-2 power-off.
    cooling = decode_c3_basic(_cooling(KAISAI_DUAL_ZONE))
    assert cooling is not None
    assert cooling.zone2_water_range == (5.0, 25.0)
    for kwargs in (
        {"zone2_power": True},
        {"zone2_water_target": 20},
        {"zone2_curve": False},
    ):
        with pytest.raises(UnsupportedC3Write, match="Heat mode"):
            apply_basic_changes(cooling, **kwargs)
    off_cool = bytes.fromhex(
        build_basic_set(apply_basic_changes(cooling, zone2_power=False))
    )
    assert off_cool[11] & 0x02 == 0
    assert off_cool[12] == C3_MODE_COOL

    # Out-of-range Zone-2 target is rejected.
    with pytest.raises(ValueError, match="Zone-2 target"):
        apply_basic_changes(basic, zone2_water_target=60)

    # Zone-2 without water-temp flag refuses zone2_water_target.
    no_z2_water = bytearray(KAISAI_DUAL_ZONE)
    no_z2_water[2] &= ~0x20
    no_z2_water_basic = decode_c3_basic(no_z2_water)
    assert no_z2_water_basic is not None
    with pytest.raises(UnsupportedC3Write, match="water-temperature"):
        apply_basic_changes(no_z2_water_basic, zone2_water_target=30)


async def test_zone2_climate_added_only_for_double_zone_units(hass: HomeAssistant):
    # Single-zone Rotenso: only primary Heat Pump climate entity.
    single = _coordinator(hass, _body(ROTENSO_BASELINE))
    added_single: list = []
    await climate_platform.async_setup_entry(hass, single.entry, added_single.extend)
    single.async_update_listeners()
    assert [e.unique_id for e in added_single] == ["APPL1_climate"]

    # Dual-zone Kaisai (#151): adds Zone 2 dynamically once status arrives.
    dual = _coordinator(hass, frame=None, sn8=None)
    added_dual: list = []
    await climate_platform.async_setup_entry(hass, dual.entry, added_dual.extend)
    assert [e.unique_id for e in added_dual] == ["APPL1_climate"]

    dual.appliance_meta = {"sn8": ATW_SN8}
    dual.data = make_state(decode_atw_status(KAISAI_DUAL_ZONE), ITSSensors(), sn8=ATW_SN8)
    dual.async_update_listeners()
    dual.async_update_listeners()
    assert [e.unique_id for e in added_dual] == ["APPL1_climate", "APPL1_zone2"]

    await single.async_shutdown()
    await dual.async_shutdown()


async def test_zone2_climate_read_only_and_opt_in_control(hass: HomeAssistant):
    # Read-only by default.
    ro = _coordinator(hass, KAISAI_DUAL_ZONE)
    zone2_ro = ILetComfortC3Zone2Climate(ro)
    assert zone2_ro.available is True
    assert zone2_ro.supported_features == 0
    assert zone2_ro.hvac_mode is HVACMode.HEAT
    assert zone2_ro.target_temperature == 25.0
    assert zone2_ro.current_temperature is None
    assert (zone2_ro.min_temp, zone2_ro.max_temp) == (25.0, 55.0)
    assert zone2_ro.extra_state_attributes == {
        "zone2_water_target": 25.0,
        "weather_curve": True,
    }
    with pytest.raises(HomeAssistantError, match="disabled by default"):
        await zone2_ro.async_set_hvac_mode(HVACMode.OFF)

    # Opt-in enabled: routes Heat/Off and water setpoint to async_set_c3_basic.
    ctrl = _coordinator(hass, KAISAI_DUAL_ZONE)
    ctrl._c3_basic_control = True
    ctrl.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    zone2 = ILetComfortC3Zone2Climate(ctrl)
    assert zone2.supported_features != 0
    await zone2.async_turn_off()
    await zone2.async_turn_on()
    await zone2.async_set_temperature(**{ATTR_TEMPERATURE: 99})
    assert sent_commands(ctrl, KAISAI_DUAL_ZONE) == [
        _kaisai_set(power=0x05),
        _kaisai_set(power=0x07),
        _kaisai_set(zone2=55),
    ]
    with pytest.raises(HomeAssistantError):
        await zone2.async_set_hvac_mode(HVACMode.COOL)
    assert ctrl.async_execute_plan.await_count == 3

    # Cool mode reports COOL with Zone-2 cooling limits.
    ctrl.data = make_state(decode_atw_status(_cooling(KAISAI_DUAL_ZONE)), ctrl.data.sensors, sn8=ATW_SN8)
    assert zone2.hvac_mode is HVACMode.COOL
    assert HVACMode.COOL in zone2.hvac_modes
    assert (zone2.min_temp, zone2.max_temp) == (5.0, 25.0)

    # Without zone2_water_temp (body[2] & 0x20 == 0), target_temperature is None
    # and TARGET_TEMPERATURE feature is omitted while raw attribute stays for diagnostics.
    no_z2_water = bytearray(KAISAI_DUAL_ZONE)
    no_z2_water[2] &= ~0x20
    ctrl.data = make_state(decode_atw_status(bytes(no_z2_water)), ctrl.data.sensors, sn8=ATW_SN8)
    assert zone2.target_temperature is None
    assert zone2.extra_state_attributes["zone2_water_target"] == 25.0
    assert zone2.supported_features == (
        zone2.supported_features & ~0x01  # TARGET_TEMPERATURE bit cleared
    )


async def test_zone2_curve_switch_added_for_dual_zone_opt_in(hass: HomeAssistant):
    coordinator = _coordinator(hass, KAISAI_DUAL_ZONE)
    coordinator._c3_basic_control = True
    coordinator.async_execute_plan = AsyncMock()  # type: ignore[method-assign]
    added: list = []
    await switch.async_setup_entry(hass, coordinator.entry, added.extend)
    assert sorted(e.unique_id for e in added) == [
        "APPL1_boost",
        "APPL1_eco_mode",
        "APPL1_fast_dhw",
        "APPL1_silent_mode",
        "APPL1_zone1_curve",
        "APPL1_zone2_curve",
    ]
    z2_curve = next(e for e in added if isinstance(e, ILetComfortC3Zone2CurveSwitch))
    assert z2_curve.available is True
    assert z2_curve.is_on is True
    await z2_curve.async_turn_off()
    assert sent_commands(coordinator, KAISAI_DUAL_ZONE) == [_kaisai_set(flags=0x01)]

    # If a later status lacks double_zone, the switch becomes unavailable.
    coordinator.data = make_state(decode_atw_status(_body(ROTENSO_BASELINE)), coordinator.data.sensors, sn8=ATW_SN8)
    assert z2_curve.available is False
