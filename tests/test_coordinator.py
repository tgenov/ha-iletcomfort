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


async def test_first_refresh_populates_appliance_meta_by_code(hass: HomeAssistant):
    """async_first_refresh_with_login caches the appliance whose code matches.

    Diagnostic-only metadata (issue #22): given a mocked list_appliances the
    coordinator stores the dict whose ``applianceCode`` equals appliance_code.
    """
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.load_token.return_value = True  # skip login path
    matching = {
        "applianceCode": "APPL1",
        "applianceType": "0xC3",
        "modelNumber": "0",
        "sn8": "171H120F",
        "owner": "someone@example.com",
        "sn": "SECRETSN",
        "name": "Living Room",
        "online": "1",
    }
    other = {"applianceCode": "OTHER", "applianceType": "0x00"}
    client.list_appliances.return_value = [other, matching]

    with patch.object(
        ILetComfortCoordinator, "async_config_entry_first_refresh", new=AsyncMock()
    ):
        await coord.async_first_refresh_with_login()

    assert coord.appliance_meta == matching


async def test_ensure_appliance_meta_failure_leaves_none_and_does_not_block(
    hass: HomeAssistant,
):
    """A list_appliances error must not blank metadata-collection nor block refresh."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.load_token.return_value = True  # skip login path
    client.list_appliances.side_effect = ApiError("boom")

    first_refresh = AsyncMock()
    with patch.object(
        ILetComfortCoordinator,
        "async_config_entry_first_refresh",
        new=first_refresh,
    ):
        await coord.async_first_refresh_with_login()

    assert coord.appliance_meta is None
    first_refresh.assert_awaited_once()


async def test_sn8_property_reads_appliance_meta(hass: HomeAssistant):
    """The coordinator exposes the appliance sn8 used to select a decode profile."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    assert coord.sn8 is None  # no metadata yet
    coord.appliance_meta = {"sn8": "171H120F"}
    assert coord.sn8 == "171H120F"
    coord.appliance_meta = {"sn8": ""}
    assert coord.sn8 is None


async def test_poll_passes_sn8_and_applies_atw_overrides(hass: HomeAssistant):
    """An ATW (sn8 171H120F) poll passes sn8 to query_status and routes the
    DHW tank temp into th_temp (the "DHW Tank Temperature" sensor) while leaving
    twin_temp (Water Inlet) honest."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "171H120F"}
    client = mock_cls.return_value
    # The client already applies the ATW status profile, so its query_status
    # returns box_bottom_temp=46 with twin_temp still 0 from the sensors decode.
    atw_status = ITSStatus(box_bottom_temp=46.0, set_temperature=50, t5s_def=21.0)
    client.query_status.return_value = atw_status
    client.query_sensors.return_value = ITSSensors(twin_temp=0.0)

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    # sn8 must be forwarded to both queries (so KJRH-120L gets the short cmd).
    assert client.query_status.call_args.args == ("APPL1", "171H120F")
    assert client.query_sensors.call_args.args == ("APPL1", "171H120F")
    # th_temp (DHW Tank Temperature sensor) now reflects the tank reading.
    assert result["sensors"].th_temp == 46.0
    # Water Inlet (twin_temp) stays honest — never the tank value.
    assert result["sensors"].twin_temp != 46.0


async def test_poll_standard_leaves_sensors_untouched(hass: HomeAssistant):
    """With no sn8 the poll resolves STANDARD and never rewrites the sensors."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    client.query_status.return_value = ITSStatus(box_bottom_temp=99.0, mode=1)
    sensors = ITSSensors(twin_temp=12.0)
    client.query_sensors.return_value = sensors

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        result = await coord._poll()

    assert client.query_status.call_args.args == ("APPL1", None)
    assert client.query_sensors.call_args.args == ("APPL1", None)
    assert result["sensors"] is sensors  # STANDARD is a no-op (object identity)
    assert result["sensors"].twin_temp == 12.0  # unchanged by STANDARD


async def test_async_set_device_threads_sn8_to_client(hass: HomeAssistant):
    """The SET path must forward the appliance sn8 so the client can branch the
    write encoding per model (KJRH-120L short commands vs the legacy C3 frame)."""
    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": "17100003"}
    client = mock_cls.return_value
    coord.async_request_refresh = AsyncMock()

    await coord.async_set_device(temperature=60)

    assert client.set_device.call_args.args == ("APPL1",)
    assert client.set_device.call_args.kwargs["sn8"] == "17100003"
    assert client.set_device.call_args.kwargs["temperature"] == 60


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


async def test_offline_repair_card_masks_appliance_code_placeholder(
    hass: HomeAssistant,
):
    """The offline Repair card must show a suffix-masked appliance_code, not the
    full device-unique id (it surfaces in shareable screenshots/diagnostics)."""
    coord, client = _degraded_coordinator(hass)
    coord.appliance_code = "153931629126443"
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

    issue = registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.translation_placeholders == {"appliance_code": "15393…"}


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
