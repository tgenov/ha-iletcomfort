"""C3 basic status layout for sn8 171H120F (Rotenso Aquami, #124).

Rotenso frames are real captures from 2026-11-17 (EU cloud).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.components.climate import HVACMode
from homeassistant.components.water_heater import STATE_HEAT_PUMP
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_OFF
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort import water_heater
from custom_components.iletcomfort.api import (
    STATUS_MODE_COOL,
    STATUS_MODE_HEAT,
    STATUS_MODE_OFF,
    ITSSensors,
    extract_c3_body,
    parse_hex_response,
)
from custom_components.iletcomfort.c3_basic import C3_MODE_COOL, decode_c3_basic
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
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
