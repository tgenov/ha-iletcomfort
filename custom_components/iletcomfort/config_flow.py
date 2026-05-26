"""Config flow for the iLetComfort integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import ApiError, AuthError, ILetComfortClient
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    REGION_EU,
    REGION_URLS,
    REGION_US,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): vol.In(
            [REGION_US, REGION_EU]
        ),
    }
)


def _appliance_label(appliance: dict[str, Any]) -> str:
    """Build a user-facing label for an appliance entry."""
    for key in ("applianceName", "nickname", "name", "applianceCode"):
        value = appliance.get(key)
        if value:
            return str(value)
    return "Unknown device"


class ILetComfortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iLetComfort."""

    VERSION = 1

    def __init__(self) -> None:
        self._email: str | None = None
        self._password: str | None = None
        self._region: str | None = None
        self._appliances: list[dict[str, Any]] = []

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the credentials + region step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            region = user_input[CONF_REGION]
            api_base = REGION_URLS.get(region, REGION_URLS[DEFAULT_REGION])

            try:
                client = ILetComfortClient(api_base=api_base)
                await self.hass.async_add_executor_job(
                    client.login, email, password,
                )
                appliances = await self.hass.async_add_executor_job(
                    client.list_appliances,
                )
            except AuthError:
                errors["base"] = "invalid_auth"
            except (ConnectionError, TimeoutError):
                errors["base"] = "cannot_connect"
            except ApiError as err:
                _LOGGER.warning("API error during login: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"
            else:
                if not appliances:
                    errors["base"] = "no_devices"
                else:
                    self._email = email
                    self._password = password
                    self._region = region
                    self._appliances = appliances
                    return await self.async_step_device()

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )

    async def async_step_device(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the device-picker step."""
        errors: dict[str, str] = {}

        options = {
            str(a.get("applianceCode", "")): _appliance_label(a)
            for a in self._appliances
            if a.get("applianceCode")
        }

        if user_input is not None:
            appliance_code = user_input[CONF_APPLIANCE_CODE]

            await self.async_set_unique_id(
                f"{(self._email or '').lower()}:{appliance_code}"
            )
            self._abort_if_unique_id_configured()

            label = options.get(appliance_code, appliance_code)
            return self.async_create_entry(
                title=f"iLetComfort ({label})",
                data={
                    CONF_EMAIL: self._email,
                    CONF_PASSWORD: self._password,
                    CONF_REGION: self._region,
                    CONF_APPLIANCE_CODE: appliance_code,
                },
            )

        schema = vol.Schema(
            {vol.Required(CONF_APPLIANCE_CODE): vol.In(options)}
        )
        return self.async_show_form(
            step_id="device",
            data_schema=schema,
            errors=errors,
        )
