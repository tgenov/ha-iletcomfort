"""Tests for the iLetComfort config flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests
from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ApiError, AuthError
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
    REGION_US,
)

EMAIL = "user@example.com"
PASSWORD = "s3cret"

ONE_APPLIANCE = [
    {"applianceCode": "APPL1", "applianceName": "Heat Pump"},
]
TWO_APPLIANCES = [
    {"applianceCode": "APPL1", "applianceName": "Garage HP"},
    {"applianceCode": "APPL2", "applianceName": "House HP"},
]


def _patch_client(*, login_return=None, login_side_effect=None,
                  appliances=None, list_side_effect=None):
    """Patch ILetComfortClient used by the config flow.

    Returns the MagicMock class so callers can inspect constructor args.
    """
    mock_instance = MagicMock()

    if login_side_effect is not None:
        mock_instance.login.side_effect = login_side_effect
    else:
        mock_instance.login.return_value = login_return or {
            "accessToken": "tok",
            "uid": "1",
        }

    if list_side_effect is not None:
        mock_instance.list_appliances.side_effect = list_side_effect
    else:
        mock_instance.list_appliances.return_value = appliances or []

    mock_cls = MagicMock(return_value=mock_instance)
    return patch(
        "custom_components.iletcomfort.config_flow.ILetComfortClient",
        mock_cls,
    ), mock_cls, mock_instance


async def test_user_step_form_renders(hass: HomeAssistant):
    """Initial user step should render the form, no errors."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {}


async def test_user_step_us_region_advances_to_device_step(hass: HomeAssistant):
    """Successful US login with one device → advance to device step."""
    patcher, mock_cls, mock_instance = _patch_client(
        appliances=ONE_APPLIANCE,
    )
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "device"
    # Client must be built with the US base URL.
    mock_cls.assert_called_once()
    kwargs = mock_cls.call_args.kwargs
    assert kwargs["api_base"] == "https://us.dollin.net"
    mock_instance.login.assert_called_once_with(EMAIL, PASSWORD)


async def test_user_step_eu_region_uses_eu_api_base(hass: HomeAssistant):
    """Selecting EU must route the client to eu.dollin.net."""
    patcher, mock_cls, _ = _patch_client(appliances=ONE_APPLIANCE)
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_EU,
            },
        )

    assert mock_cls.call_args.kwargs["api_base"] == "https://eu.dollin.net"


async def test_auth_error_maps_to_invalid_auth(hass: HomeAssistant):
    """AuthError from the client must produce 'invalid_auth', not 'cannot_connect'."""
    patcher, _, _ = _patch_client(login_side_effect=AuthError("bad password"))
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )

    assert result["type"] == data_entry_flow.FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


@pytest.mark.parametrize(
    "exc",
    [
        requests.exceptions.ConnectionError("connection refused"),
        requests.exceptions.Timeout("timed out"),
        requests.exceptions.RequestException("generic transport failure"),
    ],
)
async def test_network_errors_map_to_cannot_connect(
    hass: HomeAssistant, exc: Exception
):
    """requests.exceptions.* from the client must produce 'cannot_connect'.

    Before the fix, the flow caught built-in ConnectionError/TimeoutError
    which never matched requests.exceptions.* — real network failures fell
    through to the 'unknown' branch.
    """
    patcher, _, _ = _patch_client(login_side_effect=exc)
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_api_error_maps_to_cannot_connect(hass: HomeAssistant):
    """Non-auth API errors (e.g. signature failed) → cannot_connect."""
    patcher, _, _ = _patch_client(
        login_side_effect=ApiError("code=3301, msg=Signature Failed")
    )
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_empty_appliance_list_shows_no_devices(hass: HomeAssistant):
    """Login succeeds but no appliances on account → 'no_devices'."""
    patcher, _, _ = _patch_client(appliances=[])
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )

    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "no_devices"}


async def test_device_step_creates_entry_with_full_data(hass: HomeAssistant):
    """Picking a device must create an entry with email/password/region/code."""
    patcher, _, _ = _patch_client(appliances=ONE_APPLIANCE)
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_EU,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_APPLIANCE_CODE: "APPL1"},
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "iLetComfort (Heat Pump)"
    assert result["data"] == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_REGION: REGION_EU,
        CONF_APPLIANCE_CODE: "APPL1",
    }


async def test_device_step_unique_id_is_email_plus_code(hass: HomeAssistant):
    """Unique ID format must be '<lowercase email>:<appliance code>'."""
    patcher, _, _ = _patch_client(appliances=ONE_APPLIANCE)
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: "User@Example.com",
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_APPLIANCE_CODE: "APPL1"},
        )

    entry = next(iter(hass.config_entries.async_entries(DOMAIN)))
    assert entry.unique_id == "user@example.com:APPL1"


async def test_multi_device_account_can_add_each_device_separately(
    hass: HomeAssistant,
):
    """Two devices on the same account → two entries with different unique IDs."""
    patcher, _, _ = _patch_client(appliances=TWO_APPLIANCES)
    with patcher:
        # First device
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_APPLIANCE_CODE: "APPL1"},
        )
        assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY

        # Second device on same account
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_APPLIANCE_CODE: "APPL2"},
        )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    unique_ids = {e.unique_id for e in hass.config_entries.async_entries(DOMAIN)}
    assert unique_ids == {f"{EMAIL}:APPL1", f"{EMAIL}:APPL2"}


async def test_same_device_added_twice_aborts(hass: HomeAssistant):
    """Re-adding the same email+appliance must abort 'already_configured'."""
    # Pre-existing entry, v2 format.
    MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"{EMAIL}:APPL1",
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_REGION: REGION_US,
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=2,
    ).add_to_hass(hass)

    patcher, _, _ = _patch_client(appliances=ONE_APPLIANCE)
    with patcher:
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                CONF_EMAIL: EMAIL,
                CONF_PASSWORD: PASSWORD,
                CONF_REGION: REGION_US,
            },
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={CONF_APPLIANCE_CODE: "APPL1"},
        )

    assert result["type"] == data_entry_flow.FlowResultType.ABORT
    assert result["reason"] == "already_configured"
