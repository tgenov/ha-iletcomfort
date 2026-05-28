"""Tests for the iLetComfort DataUpdateCoordinator wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ApiError, ITSSensors, ITSStatus
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import (
    OFFLINE_REPAIR_ID,
    OFFLINE_REPAIR_THRESHOLD,
    ILetComfortCoordinator,
)


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


async def test_poll_falls_back_to_cache_on_truncated_frame(hass: HomeAssistant):
    """A truncated-frame ApiError must keep cached data, not blank the entities.

    Issue #5: the device intermittently returns empty frames; the coordinator
    should preserve the last good ITSStatus/ITSSensors rather than overwriting
    them with all-defaults.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    cached_status = ITSStatus(mode=1)
    cached_sensors = ITSSensors()
    coord.data = {"status": cached_status, "sensors": cached_sensors}

    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert result["status"] is cached_status
    assert result["sensors"] is cached_sensors


async def test_repeated_truncated_polls_warn_once_then_debug(
    hass: HomeAssistant, caplog
):
    """A persistently failing device must warn once, then stay quiet at DEBUG.

    Issue #5: without this, every 60s poll logged a WARNING for the same
    expected transient condition, reproducing the warning spam this change
    set out to remove.
    """
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        with caplog.at_level(logging.WARNING):
            await coord._poll()
        first_warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(first_warnings) == 2  # one each for status + sensors

        caplog.clear()
        with caplog.at_level(logging.WARNING):
            await coord._poll()
        assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def _degraded_coordinator(hass: HomeAssistant) -> tuple[ILetComfortCoordinator, MagicMock]:
    """Build a coordinator wired so both queries fall back to cache."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)
    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    return coord, client


def _issue_id(coord: ILetComfortCoordinator) -> str:
    return OFFLINE_REPAIR_ID.format(entry_id=coord.entry.entry_id)


async def test_offline_repair_card_created_after_threshold(hass: HomeAssistant):
    """After OFFLINE_REPAIR_THRESHOLD consecutive both-degraded polls, a Repair appears."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD - 1):
            await coord._poll()
            assert registry.async_get_issue(DOMAIN, issue_id) is None

        await coord._poll()
        issue = registry.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.severity == ir.IssueSeverity.WARNING
        assert issue.translation_key == "device_offline"


async def test_offline_repair_card_not_created_when_only_one_query_fails(
    hass: HomeAssistant,
):
    """Sensors-only failure (or status-only) must not surface the offline Repair."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.return_value = ITSStatus(mode=1)
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD + 2):
            await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_offline_repair_card_cleared_on_recovery(hass: HomeAssistant):
    """A single healthy poll clears the Repair card."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is None


async def test_offline_repair_card_reraised_after_recovery_then_redegradation(
    hass: HomeAssistant,
):
    """After clear → degraded again, the Repair card must reappear on threshold."""
    coord, client = _degraded_coordinator(hass)
    client.query_status.side_effect = ApiError("truncated frame")
    client.query_sensors.side_effect = ApiError("truncated frame")

    registry = ir.async_get(hass)
    issue_id = _issue_id(coord)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is not None

        # Recover.
        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()
        assert registry.async_get_issue(DOMAIN, issue_id) is None

        # Degrade again.
        client.query_status.side_effect = ApiError("truncated frame")
        client.query_sensors.side_effect = ApiError("truncated frame")
        for _ in range(OFFLINE_REPAIR_THRESHOLD):
            await coord._poll()

    assert registry.async_get_issue(DOMAIN, issue_id) is not None
