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
    async_get_config_entry_diagnostics,
)


def _entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
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
    """Email and password must be redacted; region/appliance_code retained."""
    result = await _diagnostics(hass)

    entry_data = result["entry"]["data"]
    assert entry_data[CONF_EMAIL] == "**REDACTED**"
    assert entry_data[CONF_PASSWORD] == "**REDACTED**"
    assert entry_data[CONF_REGION] == REGION_US
    assert entry_data[CONF_APPLIANCE_CODE] == "APPL1"


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
