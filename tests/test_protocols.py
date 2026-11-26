"""Tests for ProtocolAdapter registry, HeatPumpState, and async_execute_command (#172)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ApiError, AuthError
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
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
    KJRH120L_SN8,
    ModelProfile,
)
from custom_components.iletcomfort.protocols import (
    AquapuraAdapter,
    AtwAdapter,
    HeatPumpState,
    Kjrh120lAdapter,
    StandardAdapter,
    get_adapter,
)
from tests.test_model_profiles import NORDIS_ATW_STATUS_FRAMES


def test_get_adapter_resolves_all_profiles_and_sn8() -> None:
    assert isinstance(get_adapter(None), StandardAdapter)
    assert isinstance(get_adapter("UNKNOWN8"), StandardAdapter)
    assert isinstance(get_adapter(AQUAPURA_SN8), AquapuraAdapter)
    assert isinstance(get_adapter(ATW_SN8), AtwAdapter)
    assert isinstance(get_adapter(KJRH120L_SN8), Kjrh120lAdapter)
    assert isinstance(get_adapter(ModelProfile.ATW), AtwAdapter)


def test_adapter_build_query_and_variant_description() -> None:
    assert get_adapter(KJRH120L_SN8).build_query(0x01) == "ffff010101ff"
    assert get_adapter(ATW_SN8).build_query(0x01).startswith("aa")

    atw = get_adapter(ATW_SN8)
    raw_status = MagicMock(raw_body=NORDIS_ATW_STATUS_FRAMES["issue132"])
    decoded = atw.decode_status(raw_status)
    assert atw.describe_variant(decoded) == "atw_27_byte"

    state = HeatPumpState(
        profile=ModelProfile.ATW,
        variant="atw_27_byte",
        status=decoded,
    )
    assert state.error_code == 0
    assert state.compressor_running is None


def _coordinator(
    hass: HomeAssistant, *, phone_app_mode: bool = False
) -> ILetComfortCoordinator:
    options = (
        {CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP} if phone_app_mode else {}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        options=options,
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        return ILetComfortCoordinator(hass, entry)


async def test_async_execute_command_blocks_phone_app_mode(hass: HomeAssistant) -> None:
    coord = _coordinator(hass, phone_app_mode=True)
    action = MagicMock()
    with pytest.raises(HomeAssistantError, match="phone app coexistence"):
        await coord.async_execute_command(action)
    action.assert_not_called()


async def test_async_execute_command_reauths_once_and_refreshes(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    coord._async_login = AsyncMock()  # type: ignore[method-assign]
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AuthError("expired")
        return "ok"

    result = await coord.async_execute_command(flaky)
    assert result == "ok"
    assert calls == 2
    coord._async_login.assert_awaited_once()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize(
    "exc",
    [
        ApiError("code=1214"),
        ValueError("out of range"),
        requests.ConnectionError("timeout"),
    ],
)
async def test_async_execute_command_translates_errors_without_refresh(
    hass: HomeAssistant, exc: Exception
) -> None:
    coord = _coordinator(hass)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    def failing() -> None:
        raise exc

    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coord.async_execute_command(failing)
    coord.async_request_refresh.assert_not_awaited()
