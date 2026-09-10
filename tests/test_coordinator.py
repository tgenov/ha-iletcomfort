"""Tests for the iLetComfort DataUpdateCoordinator wiring."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
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
    SUSTAINED_FAILURE_THRESHOLD,
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


async def test_single_transient_failure_logs_debug_not_warning(
    hass: HomeAssistant, caplog
):
    """A one-off cloud/DNS blip (fail then success) must stay at DEBUG.

    Issue #44: on a flaky vendor cloud an isolated 502/RemoteDisconnected/DNS
    failure is expected and harmless (the poll falls back to cache and recovers
    next time), so it must not surface a WARNING.
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

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        # One failing poll ...
        client.query_status.side_effect = ApiError("502 Bad Gateway")
        client.query_sensors.side_effect = ApiError("502 Bad Gateway")
        with caplog.at_level(logging.DEBUG):
            await coord._poll()

        # ... immediately followed by a healthy one.
        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        await coord._poll()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not warnings
    debug_fallbacks = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "using cache" in r.getMessage()
    ]
    assert len(debug_fallbacks) == 2  # status + sensors, both at DEBUG


async def test_intermittent_failures_never_warn(hass: HomeAssistant, caplog):
    """fail → success → fail → success must never escalate to WARNING.

    Issue #44: the old "warn on entry to degraded, reset on success" logic
    re-warned on every fresh failure, so an intermittent flaky cloud produced
    hundreds of WARNINGs. Because success resets the streak, no query ever
    reaches the sustained threshold here.
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
    good_status = ITSStatus(mode=1)
    good_sensors = ITSSensors()

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        with caplog.at_level(logging.WARNING):
            for _ in range(6):
                # Fail this poll.
                client.query_status.side_effect = ApiError("code=1214, msg=System error")
                client.query_sensors.side_effect = ApiError("RemoteDisconnected")
                await coord._poll()
                # Recover next poll.
                client.query_status.side_effect = None
                client.query_status.return_value = good_status
                client.query_sensors.side_effect = None
                client.query_sensors.return_value = good_sensors
                await coord._poll()

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


async def test_sustained_failures_warn_once_at_threshold_then_debug(
    hass: HomeAssistant, caplog
):
    """SUSTAINED_FAILURE_THRESHOLD consecutive failures warn exactly once.

    Issue #44: DEBUG for every poll below the threshold, a single WARNING at
    the threshold poll, then DEBUG again for the ongoing sustained failure so
    a genuinely stuck state is flagged once without flooding the log.
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
    client.query_status.side_effect = ApiError("502 Bad Gateway")
    client.query_sensors.side_effect = ApiError("502 Bad Gateway")

    total_polls = SUSTAINED_FAILURE_THRESHOLD + 2
    warning_polls: list[int] = []

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        with caplog.at_level(logging.DEBUG):
            for poll in range(1, total_polls + 1):
                caplog.clear()
                await coord._poll()
                if [r for r in caplog.records if r.levelno == logging.WARNING]:
                    warning_polls.append(poll)

    # Exactly one poll warned, and it was the threshold poll — one WARNING per
    # query (status + sensors) on that poll only.
    assert warning_polls == [SUSTAINED_FAILURE_THRESHOLD]


async def test_recovery_after_sustained_logs_info_once(hass: HomeAssistant, caplog):
    """Recovering out of the sustained-WARNING state logs a single INFO."""
    import logging

    entry = _entry(REGION_US)
    entry.add_to_hass(hass)
    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortClient"
    ) as mock_cls:
        coord = ILetComfortCoordinator(hass, entry)

    client = mock_cls.return_value
    coord.data = {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    client.query_status.side_effect = ApiError("502 Bad Gateway")
    client.query_sensors.side_effect = ApiError("502 Bad Gateway")

    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep",
        new=AsyncMock(),
    ):
        for _ in range(SUSTAINED_FAILURE_THRESHOLD):
            await coord._poll()

        client.query_status.side_effect = None
        client.query_status.return_value = ITSStatus(mode=1)
        client.query_sensors.side_effect = None
        client.query_sensors.return_value = ITSSensors()
        with caplog.at_level(logging.INFO):
            await coord._poll()

    recovered = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO and "recovered" in r.getMessage()
    ]
    assert len(recovered) == 2  # one each for status + sensors


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


async def test_phone_app_mode_rejects_account_control(hass: HomeAssistant):
    """Read-only coexistence mode never steals the phone session for a write."""
    from homeassistant.exceptions import HomeAssistantError

    from custom_components.iletcomfort.const import OPERATION_MODE_PHONE_APP

    entry = _entry_operation_mode(OPERATION_MODE_PHONE_APP)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coordinator = ILetComfortCoordinator(hass, entry)

    with pytest.raises(HomeAssistantError, match="HA primary"):
        await coordinator.async_set_device(temperature=42)

    cls.return_value.set_device.assert_not_called()


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


async def test_poll_recovers_missing_model_metadata(hass: HomeAssistant):
    """A failed startup lookup must not pin subsequent polls to STANDARD."""
    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    client = cls.return_value
    matching = {"applianceCode": "APPL1", "sn8": "171H120F"}
    client.list_appliances.side_effect = [ApiError("temporary failure"), [matching]]
    await coord._ensure_appliance_meta()
    assert coord.appliance_meta is None
    client.query_status.return_value = ITSStatus()
    client.query_sensors.return_value = ITSSensors()
    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        await coord._poll()
        await coord._poll()
    assert coord.appliance_meta == matching
    client.query_status.assert_called_with("APPL1", "171H120F")
    assert client.list_appliances.call_count == 2


async def test_metadata_never_uses_a_different_appliance(hass: HomeAssistant):
    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    cls.return_value.list_appliances.return_value = [
        {"applianceCode": "OTHER", "sn8": "17100003"}
    ]
    await coord._ensure_appliance_meta()
    assert coord.appliance_meta is None


async def test_model_discovery_drops_incompatible_cache(hass: HomeAssistant):
    """A failed first ATW poll must not serve old STANDARD values."""
    import pytest
    from homeassistant.helpers.update_coordinator import UpdateFailed

    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    client = cls.return_value
    coord.data = {"status": ITSStatus(mode=19), "sensors": ITSSensors()}
    coord._last_on_state = (1, 30)
    client.list_appliances.return_value = [{"applianceCode": "APPL1", "sn8": "171H120F"}]
    client.query_status.side_effect = ApiError("offline")
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()
    assert coord.data is None
    assert coord._last_on_state is None


async def test_metadata_retries_a_record_without_model_code(hass: HomeAssistant):
    entry = _entry(REGION_EU)
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    client = cls.return_value
    client.list_appliances.side_effect = [
        [{"applianceCode": "APPL1"}],
        [{"applianceCode": "APPL1", "sn8": "171H120F"}],
    ]
    await coord._ensure_appliance_meta()
    assert coord.sn8 is None
    await coord._ensure_appliance_meta()
    assert coord.sn8 == "171H120F"


# --- MQTT push wiring (issue #55) -------------------------------------------

def _entry_push(enabled: bool) -> MockConfigEntry:
    from custom_components.iletcomfort.const import CONF_ENABLE_MQTT_PUSH
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_US,
        },
        options={CONF_ENABLE_MQTT_PUSH: enabled},
        version=2,
    )


def _entry_operation_mode(mode: str) -> MockConfigEntry:
    from custom_components.iletcomfort.const import CONF_OPERATION_MODE

    return MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_US,
        },
        options={CONF_OPERATION_MODE: mode},
        version=2,
    )


async def test_push_disabled_by_default_no_client_no_interval_change(hass: HomeAssistant):
    """Flag off: no push client, poll interval unchanged — zero regression."""
    from custom_components.iletcomfort.const import DEFAULT_SCAN_INTERVAL

    entry = _entry(REGION_US)  # no options at all
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortPushClient"
    ) as push_cls:
        await coordinator.async_start_push()

    push_cls.assert_not_called()
    assert coordinator.update_interval.total_seconds() == DEFAULT_SCAN_INTERVAL


async def test_push_enabled_starts_client(hass: HomeAssistant):
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortPushClient"
    ) as push_cls:
        instance = push_cls.return_value
        instance.async_start = AsyncMock()
        await coordinator.async_start_push()

    push_cls.assert_called_once()
    instance.async_start.assert_awaited_once()
    # scoped to this appliance + region
    assert push_cls.call_args.kwargs["appliance_code"] == "APPL1"
    assert push_cls.call_args.kwargs["region"] == REGION_US


async def test_phone_app_mode_starts_push_client(hass: HomeAssistant):
    from custom_components.iletcomfort.const import OPERATION_MODE_PHONE_APP

    entry = _entry_operation_mode(OPERATION_MODE_PHONE_APP)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortPushClient"
    ) as push_cls:
        push_cls.return_value.async_start = AsyncMock()
        await coordinator.async_start_push()

    push_cls.assert_called_once()


async def test_phone_app_mode_push_start_failure_does_not_fall_back_to_polling(
    hass: HomeAssistant,
):
    """A broker/certificate failure cannot restart the account login war."""
    from custom_components.iletcomfort.const import OPERATION_MODE_PHONE_APP

    entry = _entry_operation_mode(OPERATION_MODE_PHONE_APP)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)
    coordinator.async_set_updated_data(
        {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    )

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortPushClient"
    ) as push_cls:
        push_cls.return_value.async_start = AsyncMock(
            side_effect=RuntimeError("broker unavailable")
        )
        await coordinator.async_start_push()

    assert coordinator.update_interval is None
    assert coordinator.last_update_success is False


async def test_push_status_updates_data_keeping_sensors(hass: HomeAssistant):
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    prior_sensors = ITSSensors()
    coordinator.async_set_updated_data(
        {"status": ITSStatus(mode=0), "sensors": prior_sensors}
    )

    pushed = ITSStatus(mode=1, set_temperature=42)
    coordinator._apply_push_status(pushed)

    assert coordinator.data["status"].mode == 1
    assert coordinator.data["status"].set_temperature == 42
    # sensors come from the poll, not the push — keep the last good ones
    assert coordinator.data["sensors"] is prior_sensors


async def test_push_status_ignored_before_first_poll(hass: HomeAssistant):
    """A push arriving before any poll data must not crash."""
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    coordinator._apply_push_status(ITSStatus(mode=1))  # data is None
    # a push before the first poll is ignored, not published with no sensors
    assert coordinator.data is None


async def test_legacy_push_option_migrates_to_phone_app_polling_behavior(
    hass: HomeAssistant,
):
    """Entries created by v0.9-v0.11 keep push without account polling."""
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    coordinator._apply_push_connected(True)

    assert coordinator.update_interval is None


async def test_phone_app_mode_connected_disables_account_polling(
    hass: HomeAssistant,
):
    """Coexistence mode must not periodically steal the phone's account session."""
    from custom_components.iletcomfort.const import OPERATION_MODE_PHONE_APP

    entry = _entry_operation_mode(OPERATION_MODE_PHONE_APP)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    coordinator._apply_push_connected(True)

    assert coordinator.update_interval is None


async def test_phone_app_mode_disconnect_is_unavailable_without_polling(
    hass: HomeAssistant,
):
    """A push outage is visible and never starts an account-session fallback."""
    from custom_components.iletcomfort.const import OPERATION_MODE_PHONE_APP

    entry = _entry_operation_mode(OPERATION_MODE_PHONE_APP)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)
    coordinator.async_set_updated_data(
        {"status": ITSStatus(mode=1), "sensors": ITSSensors()}
    )

    with patch.object(
        coordinator, "async_request_refresh", new=AsyncMock()
    ) as refresh:
        coordinator._apply_push_connected(True)
        coordinator._apply_push_connected(False)
        await hass.async_block_till_done()

    assert coordinator.update_interval is None
    assert coordinator.last_update_success is False
    refresh.assert_not_awaited()


async def test_legacy_push_option_disconnect_does_not_refresh(hass: HomeAssistant):
    """Legacy push entries inherit the no-login fallback guarantee."""
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    with patch.object(
        coordinator, "async_request_refresh", new=AsyncMock()
    ) as refresh:
        coordinator._apply_push_connected(True)
        coordinator._apply_push_connected(False)
        await hass.async_block_till_done()

    refresh.assert_not_awaited()
    assert coordinator.last_update_success is False


async def test_async_stop_push_stops_client(hass: HomeAssistant):
    entry = _entry_push(True)
    entry.add_to_hass(hass)
    coordinator = ILetComfortCoordinator(hass, entry)

    with patch(
        "custom_components.iletcomfort.coordinator.ILetComfortPushClient"
    ) as push_cls:
        instance = push_cls.return_value
        instance.async_start = AsyncMock()
        instance.async_stop = AsyncMock()
        await coordinator.async_start_push()
        await coordinator.async_stop_push()

    instance.async_stop.assert_awaited_once()
