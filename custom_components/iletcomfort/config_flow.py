"""Config flow for the iLetComfort integration."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD

from .api import ApiError, AuthError, ILetComfortClient
from .const import CONF_APPLIANCE_CODE, DOMAIN

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class ILetComfortConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for iLetComfort."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]

            # Check if already configured
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            try:
                client = ILetComfortClient()
                login_data = await self.hass.async_add_executor_job(
                    client.login, email, password,
                )

                # Discover the first appliance
                appliances = await self.hass.async_add_executor_job(
                    client.list_appliances,
                )
                appliance_code = ""
                if appliances:
                    appliance_code = str(appliances[0].get("applianceCode", ""))

                return self.async_create_entry(
                    title=f"iLetComfort ({email})",
                    data={
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                        CONF_APPLIANCE_CODE: appliance_code,
                    },
                )

            except AuthError:
                errors["base"] = "invalid_auth"
            except (ApiError, ConnectionError, TimeoutError):
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during config flow")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_DATA_SCHEMA,
            errors=errors,
        )
