"""C3 basic status layout for sn8 171H120F (Rotenso Aquami, #124).

Rotenso frames are real captures from 2026-11-17 (EU cloud).
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_C3_BASIC_CONTROL,
    CONF_OPERATION_MODE,
    CONF_REGION,
    DOMAIN,
    OPERATION_MODE_PHONE_APP,
    REGION_EU,
)
from custom_components.iletcomfort.switch import (
    ILetComfortC3CurveSwitch,
    ILetComfortC3FeatureSwitch,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.model_profiles import (
    AQUAPURA_SN8,
    ATW_SN8,
    atw_c3_basic,
    decode_atw_status,
)
from custom_components.iletcomfort.water_heater import ILetComfortDhw


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
    """The #124 bug: heating on in the app, HA showed Off."""
    status = decode_atw_status(ROTENSO_ISSUE124_HEATING)
    assert status.mode == STATUS_MODE_HEAT
    assert status.mode_name == "Heat"
    assert status.c3_basic.zone1_power
    # The #124 reporter's app showed a 24 °C target at that moment; the
    # established byte[9] / 2 mapping is kept for this layout.
    assert status.t5s_def == 24.0


def test_rotenso_baseline_status():
    status = decode_atw_status(_body(ROTENSO_BASELINE))
    assert status.mode == STATUS_MODE_OFF
    assert status.mode_name == "Off"
    assert status.t5s_def == 24.0  # mapping unchanged
    assert status.set_temperature == 55
    assert status.box_bottom_temp == 16.0
    assert status.comp_running is None
    assert status.comp_frq is None


def test_rotenso_powered_zone_in_cooling_mode_is_cool_not_heat():
    status = decode_atw_status(_cooling(ROTENSO_ISSUE124_HEATING))
    assert status.mode == STATUS_MODE_COOL
    assert status.mode_name == "Cool"


def test_italtherm_heat_off_decode_is_unchanged():
    """byte[1] bit 0 on the 25-byte [2]=0x07 layout is not read as power."""
    status = decode_atw_status(ITALTHERM_STATE2)
    assert status.c3_basic is not None and status.c3_basic.zone1_power
    assert status.mode == 0 and status.mode_name == "Off"  # ITSStatus default
    assert status.t5s_def == 29.0
    assert status.comp_running is False


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
        coordinator.data = {
            "status": decode_atw_status(frame),
            "sensors": ITSSensors(),
        }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    return coordinator


def test_climate_reports_heat_and_both_setpoint_candidates(hass: HomeAssistant):
    climate = ILetComfortClimate(_coordinator(hass, ROTENSO_ISSUE124_HEATING))
    assert climate.hvac_mode is HVACMode.HEAT
    assert climate.target_temperature == 24.0
    attrs = climate.extra_state_attributes
    assert attrs["zone1_water_target"] == 40.0
    assert attrs["room_target"] == 24.0
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
    coordinator.data = {
        "status": decode_atw_status(_body(ROTENSO_BASELINE)),
        "sensors": ITSSensors(),
    }
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()

    assert [e.unique_id for e in added] == ["APPL1_dhw"]


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


def _baseline() -> C3BasicStatus:
    return decode_c3_basic(_body(ROTENSO_BASELINE))


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


@pytest.mark.parametrize(
    "changes",
    [{"zone1_power": True}, {"zone1_water_target": 20}, {"zone1_curve": False}],
)
def test_zone1_writes_refused_in_cooling_mode(changes):
    """A Heat request must never power on (or tune) an existing Cool mode."""
    cooling = decode_c3_basic(_cooling(_body(ROTENSO_BASELINE)))
    with pytest.raises(UnsupportedC3Write):
        apply_basic_changes(cooling, **changes)


def test_cooling_mode_still_allows_zone1_off_and_dhw():
    cooling = decode_c3_basic(_cooling(_body(ROTENSO_BASELINE)))
    frame = bytes.fromhex(
        build_basic_set(apply_basic_changes(cooling, zone1_power=False, dhw_target=50))
    )
    assert frame[12] == C3_MODE_COOL  # mode written back unchanged
    assert frame[11] & 0x01 == 0


def test_room_temperature_setpoint_write_is_refused():
    body = bytearray(_body(ROTENSO_BASELINE))
    body[2] &= ~0x10  # room-temperature control: not validated
    with pytest.raises(UnsupportedC3Write):
        apply_basic_changes(decode_c3_basic(body), zone1_water_target=30)


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


# --- client ----------------------------------------------------------------

def test_client_set_c3_basic_reads_then_writes_one_change():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(side_effect=[ROTENSO_BASELINE, ROTENSO_DHW_ON])
    result = client.set_c3_basic("APPL1", sn8=ATW_SN8, dhw_target=54)
    sent = [c.args[1] for c in client.send_hex_command.call_args_list]
    assert sent == [build_c3_query(0x01), SET_DHW_54]
    assert result["sent"] == SET_DHW_54


@pytest.mark.parametrize("sn8", [None, AQUAPURA_SN8, "17100003"])
def test_client_refuses_c3_writes_for_other_models(sn8):
    client = ILetComfortClient()
    client.send_hex_command = MagicMock()
    with pytest.raises(ApiError):
        client.set_c3_basic("APPL1", sn8=sn8, dhw_target=54)
    with pytest.raises(ApiError):
        client.set_c3_feature("APPL1", sn8=sn8, feature=FEATURE_SILENT, on=True)
    client.send_hex_command.assert_not_called()


def test_client_refuses_unknown_layout():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(
        return_value="aa,10,c3,00,00,00,00,00,00,03,01,08,15,a0,03,07,1d,1e"
    )
    with pytest.raises(ApiError):
        client.set_c3_basic("APPL1", sn8=ATW_SN8, dhw_target=54)
    assert client.send_hex_command.call_count == 1


def test_client_heat_request_on_cooling_unit_sends_nothing():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(return_value=ROTENSO_COOLING_REPLY)
    with pytest.raises(UnsupportedC3Write):
        client.set_c3_basic("APPL1", sn8=ATW_SN8, zone1_power=True)
    assert client.send_hex_command.call_count == 1  # the status query only


def test_client_set_c3_feature_copies_schedule():
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(side_effect=[SILENCE_REPLY, SET_ACK_SILENCE])
    client.set_c3_feature("APPL1", sn8=ATW_SN8, feature=FEATURE_SILENT, on=True)
    assert client.send_hex_command.call_args_list[1].args[1] == SET_SILENT_ON


@pytest.mark.parametrize(
    "reply",
    [
        "aa,0b,c3,00,00,00,00,00,00,03,05,2b",  # stub: subtype only
        "aa,0f,c3,00,00,00,00,00,00,03,05,00,0c,00,0f,00",  # truncated schedule
    ],
)
def test_client_feature_incomplete_reply_sends_no_set(reply):
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(return_value=reply)
    with pytest.raises(ValueError):
        client.set_c3_feature("APPL1", sn8=ATW_SN8, feature=FEATURE_SILENT, on=True)
    assert client.send_hex_command.call_count == 1  # the query only


# --- coordinator -------------------------------------------------------------

def _control_coordinator(hass: HomeAssistant, enabled: bool = True):
    coordinator = _coordinator(hass, _body(ROTENSO_BASELINE))
    coordinator._c3_basic_control = enabled
    coordinator.async_request_refresh = AsyncMock()
    coordinator._async_login = AsyncMock()
    return coordinator


async def test_c3_write_requires_opt_in(hass: HomeAssistant):
    coordinator = _control_coordinator(hass, enabled=False)
    with pytest.raises(HomeAssistantError, match="not enabled"):
        await coordinator.async_set_c3_basic(dhw_target=54)
    coordinator.client.set_c3_basic.assert_not_called()


async def test_c3_write_forwards_sn8_and_refreshes(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    await coordinator.async_set_c3_basic(dhw_target=54)
    coordinator.client.set_c3_basic.assert_called_once_with(
        "APPL1", sn8=ATW_SN8, dhw_target=54
    )
    coordinator.async_request_refresh.assert_awaited_once()


async def test_c3_feature_write_reauths_once(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.client.set_c3_feature.side_effect = [AuthError("evicted"), {}]
    await coordinator.async_set_c3_feature(FEATURE_ECO, True)
    coordinator._async_login.assert_awaited_once()
    assert coordinator.client.set_c3_feature.call_count == 2


async def test_c3_write_blocked_in_phone_app_mode(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator._operation_mode = OPERATION_MODE_PHONE_APP
    with pytest.raises(HomeAssistantError, match="HA primary"):
        await coordinator.async_set_c3_basic(zone1_power=True)
    coordinator.client.set_c3_basic.assert_not_called()


@pytest.mark.parametrize(
    "error",
    [ApiError("code=1214"), ValueError("out of range"), UnsupportedC3Write("cool")],
)
async def test_c3_write_errors_surface_in_ha(hass: HomeAssistant, error):
    coordinator = _control_coordinator(hass)
    coordinator.client.set_c3_basic.side_effect = error
    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coordinator.async_set_c3_basic(dhw_target=70)
    coordinator.async_request_refresh.assert_awaited_once()


async def test_legacy_set_device_uses_the_shared_write_path(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.client.set_device.side_effect = ApiError("code=1214")
    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coordinator.async_set_device(temperature=40)
    coordinator.async_request_refresh.assert_awaited_once()


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
    client.send_hex_command = device.send_hex_command
    coordinator.client = client

    await asyncio.gather(
        coordinator.async_set_c3_basic(dhw_target=54),
        coordinator.async_set_c3_basic(zone1_water_target=28),
    )

    assert len(device.sets) == 2
    assert device.body[8] == 54  # DHW change survived
    assert device.body[6] == 28  # Zone-1 change survived


# --- entities ----------------------------------------------------------------

async def test_climate_control_heat_off_and_setpoint(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.async_set_c3_basic = AsyncMock()
    climate = ILetComfortClimate(coordinator)

    assert climate.hvac_modes == [HVACMode.OFF, HVACMode.HEAT]
    assert climate.hvac_mode is HVACMode.OFF
    # With control enabled the climate shows the field it writes.
    assert climate.target_temperature == 29.0
    assert (climate.min_temp, climate.max_temp) == (25.0, 55.0)

    await climate.async_set_hvac_mode(HVACMode.HEAT)
    coordinator.async_set_c3_basic.assert_awaited_with(zone1_power=True)
    await climate.async_turn_off()
    coordinator.async_set_c3_basic.assert_awaited_with(zone1_power=False)
    await climate.async_set_temperature(**{ATTR_TEMPERATURE: 99})
    coordinator.async_set_c3_basic.assert_awaited_with(zone1_water_target=55.0)
    with pytest.raises(HomeAssistantError):
        await climate.async_set_hvac_mode(HVACMode.COOL)


async def test_climate_control_reports_cooling_with_cooling_limits(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.data["status"] = decode_atw_status(_cooling(ROTENSO_ISSUE124_HEATING))
    climate = ILetComfortClimate(coordinator)

    assert climate.hvac_mode is HVACMode.COOL
    assert HVACMode.COOL in climate.hvac_modes
    assert (climate.min_temp, climate.max_temp) == (5.0, 25.0)


async def test_climate_heat_request_on_cooling_unit_is_refused_end_to_end(
    hass: HomeAssistant,
):
    coordinator = _control_coordinator(hass)
    coordinator.data["status"] = decode_atw_status(_cooling(_body(ROTENSO_BASELINE)))
    client = ILetComfortClient()
    client.send_hex_command = MagicMock(return_value=ROTENSO_COOLING_REPLY)
    coordinator.client = client

    with pytest.raises(HomeAssistantError, match="Heat mode"):
        await ILetComfortClimate(coordinator).async_set_hvac_mode(HVACMode.HEAT)
    assert client.send_hex_command.call_count == 1  # no SET sent


async def test_climate_without_opt_in_keeps_existing_behaviour(hass: HomeAssistant):
    climate = ILetComfortClimate(_control_coordinator(hass, enabled=False))
    assert climate.target_temperature == 24.0
    assert HVACMode.FAN_ONLY in climate.hvac_modes


async def test_water_heater_writes_only_with_opt_in(hass: HomeAssistant):
    read_only = ILetComfortDhw(_control_coordinator(hass, enabled=False))
    assert read_only.supported_features == 0

    coordinator = _control_coordinator(hass)
    coordinator.async_set_c3_basic = AsyncMock()
    entity = ILetComfortDhw(coordinator)
    assert entity.supported_features != 0
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 54})
    coordinator.async_set_c3_basic.assert_awaited_with(dhw_target=54.0)
    await entity.async_set_operation_mode(STATE_HEAT_PUMP)
    coordinator.async_set_c3_basic.assert_awaited_with(dhw_power=True)
    await entity.async_turn_off()
    coordinator.async_set_c3_basic.assert_awaited_with(dhw_power=False)


async def test_switches(hass: HomeAssistant):
    coordinator = _control_coordinator(hass)
    coordinator.async_set_c3_basic = AsyncMock()
    coordinator.async_set_c3_feature = AsyncMock()

    curve = ILetComfortC3CurveSwitch(coordinator)
    assert curve.is_on is True
    await curve.async_turn_off()
    coordinator.async_set_c3_basic.assert_awaited_with(zone1_curve=False)

    silent = ILetComfortC3FeatureSwitch(
        coordinator, FEATURE_SILENT, "Silent Mode", "mdi:volume-off",
        lambda b: b.silent_mode,
    )
    assert silent.unique_id == "APPL1_silent_mode"
    assert silent.is_on is False
    await silent.async_turn_on()
    coordinator.async_set_c3_feature.assert_awaited_with(FEATURE_SILENT, True)


async def test_c3_switches_added_only_with_opt_in(hass: HomeAssistant):
    coordinator = _control_coordinator(hass, enabled=False)
    added: list = []
    await switch.async_setup_entry(hass, coordinator.entry, added.extend)
    coordinator.async_update_listeners()
    assert [e.unique_id for e in added] == ["APPL1_boost"]

    coordinator._c3_basic_control = True
    coordinator.async_update_listeners()
    coordinator.async_update_listeners()
    assert sorted(e.unique_id for e in added) == [
        "APPL1_boost", "APPL1_eco_mode", "APPL1_silent_mode", "APPL1_zone1_curve",
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
