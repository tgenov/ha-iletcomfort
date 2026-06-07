"""Tests for the iLetComfort diagnostics handler."""

from __future__ import annotations

import json
from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.diagnostics import (
    _sensors_temperature_scan,
    async_get_config_entry_diagnostics,
)


APPLIANCE_CODE = "153931629126443"


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"user@example.com:{APPLIANCE_CODE}",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: APPLIANCE_CODE,
            CONF_REGION: REGION_US,
        },
        version=2,
    )


async def _diagnostics(hass: HomeAssistant) -> dict:
    """Build a coordinator with known data and return its diagnostics payload."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    status = ITSStatus(mode=1, total_kwh=42, raw_body=bytes([0xaa, 0x01, 0xff]))
    sensors = ITSSensors(twin_temp=21.0, raw_body=bytes([0xbb, 0x02, 0x10]))
    coord.data = {"status": status, "sensors": sensors}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    return await async_get_config_entry_diagnostics(hass, entry)


async def test_diagnostics_redacts_credentials(hass: HomeAssistant):
    """Email/password redacted; region retained; appliance_code suffix-masked."""
    result = await _diagnostics(hass)

    entry_data = result["entry"]["data"]
    assert entry_data[CONF_EMAIL] == "**REDACTED**"
    assert entry_data[CONF_PASSWORD] == "**REDACTED**"
    assert entry_data[CONF_REGION] == REGION_US
    # appliance_code is a device-unique id: suffix-masked, never the full value.
    assert entry_data[CONF_APPLIANCE_CODE] == "15393…"
    assert APPLIANCE_CODE not in json.dumps(result)


async def test_diagnostics_serializes_raw_frames_as_hex(hass: HomeAssistant):
    """raw_body must be the comma-separated hex used by the debug log."""
    result = await _diagnostics(hass)

    assert result["status"]["raw_body"] == "aa,01,ff"
    assert result["status"]["total_kwh"] == 42
    assert result["sensors"]["raw_body"] == "bb,02,10"
    assert result["sensors"]["twin_temp"] == 21.0


async def test_diagnostics_payload_is_json_serializable(hass: HomeAssistant):
    """The whole payload must survive json.dumps (no stray bytes)."""
    result = await _diagnostics(hass)

    # Raises TypeError if any non-serializable value (e.g. bytes) leaked through.
    json.dumps(result)


async def test_diagnostics_handles_missing_coordinator_data(hass: HomeAssistant):
    """A coordinator that has not polled yet must not crash diagnostics."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    # A freshly constructed coordinator has data=None (no poll yet).
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["status"] is None
    assert result["sensors"] is None
    json.dumps(result)


def test_sensors_temperature_scan_decodes_with_offset():
    """Each byte must be decoded as raw_byte - TEMP_OFFSET (35)."""
    # byte 35 → 35 - 35 = 0.0 (water temperature "showing 0 °C" scenario)
    # byte 64 → 64 - 35 = 29.0 (actual water temperature for cross-reference)
    raw = bytes([0x02, 35, 64])
    scan = _sensors_temperature_scan(raw)

    assert scan[0] == 0x02 - 35  # subtype byte: 2 - 35 = -33.0
    assert scan[1] == 0.0         # raw 35 → 0 °C
    assert scan[2] == 29.0        # raw 64 → 29 °C


def test_sensors_temperature_scan_marks_sensor_disconnected_as_none():
    """Byte 239 (35 + 204 = SENSOR_DISCONNECTED) must produce None."""
    # TEMP_OFFSET=35, SENSOR_DISCONNECTED=204 → raw byte = 35 + 204 = 239
    raw = bytes([239])
    scan = _sensors_temperature_scan(raw)

    assert scan[0] is None


def test_sensors_temperature_scan_empty_body():
    """An empty raw body must return an empty dict (no crash)."""
    scan = _sensors_temperature_scan(b"")

    assert scan == {}


async def test_diagnostics_includes_temperature_scan(hass: HomeAssistant):
    """Diagnostics must contain sensors_temperature_scan with correct entries."""
    # raw_body=bytes([0xbb, 0x02, 0x10, 0xef]):
    #   0xbb=187 → 152.0, 0x02=2 → -33.0, 0x10=16 → -19.0,
    #   0xef=239 → 239-35=204=SENSOR_DISCONNECTED → None
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    status = ITSStatus(mode=1, total_kwh=42, raw_body=bytes([0xaa, 0x01, 0xff]))
    sensors = ITSSensors(twin_temp=21.0, raw_body=bytes([0xbb, 0x02, 0x10, 0xef]))
    coord.data = {"status": status, "sensors": sensors}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)
    scan = result["sensors_temperature_scan"]

    assert scan[0] == 187 - 35   # 152.0
    assert scan[1] == 2 - 35     # -33.0
    assert scan[2] == 16 - 35    # -19.0
    assert scan[3] is None        # 239 - 35 = 204 = SENSOR_DISCONNECTED


async def test_diagnostics_includes_appliance_meta_with_redaction(hass: HomeAssistant):
    """appliance_meta is surfaced with owner/sn/name redacted, class fields intact.

    Issue #22: a maintainer needs applianceType/modelNumber/sn8 to identify the
    device class for model-specific decoding, but owner/sn/name are
    account/device-identifying and must be redacted.
    """
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {
        "applianceCode": APPLIANCE_CODE,
        "applianceType": "0xC3",
        "modelNumber": "0",
        "sn8": "171H120F",
        "online": "1",
        "owner": "someone@example.com",
        "sn": "SECRETSN",
        "name": "Living Room",
    }
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)
    appliance = result["appliance"]

    assert appliance["owner"] == "**REDACTED**"
    assert appliance["sn"] == "**REDACTED**"
    assert appliance["name"] == "**REDACTED**"
    assert appliance["applianceType"] == "0xC3"
    assert appliance["modelNumber"] == "0"
    assert appliance["sn8"] == "171H120F"
    # applianceCode is a device-unique id: suffix-masked, full value never leaked.
    assert appliance["applianceCode"] == "15393…"
    assert appliance["online"] == "1"
    assert APPLIANCE_CODE not in json.dumps(result)
    json.dumps(result)


async def test_diagnostics_appliance_meta_none_is_graceful(hass: HomeAssistant):
    """When appliance_meta is None, the appliance key must be None (no crash)."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["appliance"] is None
    json.dumps(result)


async def test_diagnostics_temperature_scan_empty_without_sensors(hass: HomeAssistant):
    """When the coordinator has no data, sensors_temperature_scan must be {}."""
    entry = _entry()
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord

    result = await async_get_config_entry_diagnostics(hass, entry)

    assert result["sensors_temperature_scan"] == {}
    json.dumps(result)
