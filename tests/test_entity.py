"""Tests for shared device grouping and the ODU Current sensor (issue #10)."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.binary_sensor import (
    BINARY_SENSOR_DESCRIPTIONS,
    ILetComfortBinarySensor,
)
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.error_codes import ERROR_CODES
from custom_components.iletcomfort.select import ILetComfortMuteSelect
from custom_components.iletcomfort.sensor import (
    SENSOR_DESCRIPTIONS,
    ILetComfortSensor,
)
from custom_components.iletcomfort.switch import ILetComfortBoostSwitch


def _coordinator(hass: HomeAssistant) -> ILetComfortCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Pool Heat Pump",
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_US,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.data = {
        "status": ITSStatus(mode=1),
        "sensors": ITSSensors(odu_current=4.0, odu_version="1.2.3"),
    }
    return coord


def test_all_platforms_share_one_device(hass: HomeAssistant):
    """Every platform's entity must attach to the same Device by appliance_code."""
    coord = _coordinator(hass)
    entities = [
        ILetComfortSensor(coord, SENSOR_DESCRIPTIONS[0]),
        ILetComfortBinarySensor(coord, BINARY_SENSOR_DESCRIPTIONS[0]),
        ILetComfortClimate(coord),
        ILetComfortBoostSwitch(coord),
        ILetComfortMuteSelect(coord),
    ]

    expected_identifiers = {(DOMAIN, "APPL1")}
    for ent in entities:
        assert ent.device_info is not None
        assert ent.device_info["identifiers"] == expected_identifiers


def test_device_info_uses_entry_title_and_firmware(hass: HomeAssistant):
    """Device name comes from the entry title; sw_version from odu firmware."""
    coord = _coordinator(hass)
    info = ILetComfortSensor(coord, SENSOR_DESCRIPTIONS[0]).device_info

    assert info is not None
    assert info.get("name") == "Pool Heat Pump"
    assert info.get("manufacturer") == "iLetComfort"
    assert info.get("sw_version") == "1.2.3"


def test_odu_current_sensor_exists_and_reads_scaled_amps(hass: HomeAssistant):
    """The ODU Current sensor (issue #10/#11) must expose the scaled Ampere value.

    odu_current is decoded as fixed-point Amperes (raw / 256), so the sensor
    surfaces the physical value directly rather than the raw 16-bit count.
    """
    coord = _coordinator(hass)
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "odu_current")

    sensor = ILetComfortSensor(coord, desc)
    assert sensor.native_value == 4.0
    assert sensor.extra_state_attributes is None


def _error_code_sensor(hass: HomeAssistant, error_code: int) -> ILetComfortSensor:
    """Build the existing numeric error-code sensor with a selected code."""
    coord = _coordinator(hass)
    coord.data["status"].error_code = error_code
    desc = next(d for d in SENSOR_DESCRIPTIONS if d.key == "error_code")
    return ILetComfortSensor(coord, desc)


def test_known_error_code_adds_panel_code_and_description(hass: HomeAssistant):
    """Known faults retain their numeric state and expose vendor details."""
    assert len(ERROR_CODES) == 57
    sensor = _error_code_sensor(hass, 61)

    assert sensor.native_value == 61
    assert sensor.extra_state_attributes == {
        "panel": "E0",
        "description": "Water flow fault (after 3 consecutive flow faults)",
    }


def test_unknown_error_code_has_no_fault_attributes(hass: HomeAssistant):
    """Unmapped faults pass through without making the sensor unavailable."""
    sensor = _error_code_sensor(hass, 255)

    assert sensor.native_value == 255
    assert sensor.extra_state_attributes == {}


def test_zero_error_code_has_no_fault_attributes(hass: HomeAssistant):
    """The no-fault value does not expose fault metadata."""
    sensor = _error_code_sensor(hass, 0)

    assert sensor.native_value == 0
    assert sensor.extra_state_attributes == {}
