"""Tests for the iLetComfort DataUpdateCoordinator wiring."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator


def _entry(region: str | None) -> MockConfigEntry:
    data = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_APPLIANCE_CODE: "APPL1",
    }
    if region is not None:
        data[CONF_REGION] = region
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"user@example.com:APPL1",
        data=data,
        version=2,
    )


async def test_coordinator_us_region_routes_to_us_dollin(hass: HomeAssistant):
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    mock_cls.assert_called_once()
    assert mock_cls.call_args.kwargs["api_base"] == "https://us.dollin.net"


async def test_coordinator_eu_region_routes_to_eu_dollin(hass: HomeAssistant):
    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    assert mock_cls.call_args.kwargs["api_base"] == "https://eu.dollin.net"


async def test_coordinator_defaults_to_us_when_region_missing(
    hass: HomeAssistant,
):
    """Legacy v1 entries with no CONF_REGION should still resolve to US."""
    entry = _entry(region=None)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        ILetComfortCoordinator(hass, entry)

    assert mock_cls.call_args.kwargs["api_base"] == "https://us.dollin.net"


async def test_token_file_is_scoped_per_entry(hass: HomeAssistant):
    """The token file path must include the entry_id so multi-entry doesn't collide."""
    entry_a = _entry(REGION_US)
    entry_b = _entry(REGION_US)
    entry_a.add_to_hass(hass)
    entry_b.add_to_hass(hass)

    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord_a = ILetComfortCoordinator(hass, entry_a)
        coord_b = ILetComfortCoordinator(hass, entry_b)

    # Different entries → different token files (entry_id is in the name).
    assert coord_a._token_file != coord_b._token_file
    assert entry_a.entry_id in str(coord_a._token_file)
    assert entry_b.entry_id in str(coord_b._token_file)
    assert coord_a._token_file.name.startswith("iletcomfort_token_")
