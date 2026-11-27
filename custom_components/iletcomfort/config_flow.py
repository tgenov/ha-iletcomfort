"""Config flow for the iLetComfort integration."""

from __future__ import annotations

import logging
from typing import Any, cast

import requests
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .api import ApiError, AuthError, ILetComfortClient
from .session import DollinSessionManager, MemoryTokenStore
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_C3_BASIC_CONTROL,
    CONF_ENABLE_MQTT_PUSH,
    CONF_OPERATION_MODE,
    CONF_REGION,
    DEFAULT_C3_BASIC_CONTROL,
    DEFAULT_ENABLE_MQTT_PUSH,
    DEFAULT_OPERATION_MODE,
    DEFAULT_REGION,
    DOMAIN,
    OPERATION_MODE_HA_PRIMARY,
    OPERATION_MODE_PHONE_APP,
    REGION_EU,
    REGION_URLS,
    REGION_US,
)
from .protocols import get_adapter

_LOGGER = logging.getLogger(__name__)

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(CONF_REGION, default=DEFAULT_REGION): SelectSelector(
            SelectSelectorConfig(
                options=[REGION_US, REGION_EU],
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="region",
            )
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

    VERSION = 2

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> ILetComfortOptionsFlow:
        """Return the operation-mode options flow (issue #55)."""
        return ILetComfortOptionsFlow()

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

            client = ILetComfortClient(api_base=api_base)
            session = DollinSessionManager(
                client,
                email=email,
                password=password,
                token_store=MemoryTokenStore(),
                run_blocking=self.hass.async_add_executor_job,
            )
            appliances: list[dict[str, Any]] | None = None

            try:
                await session.async_login()
            except AuthError as err:
                _LOGGER.warning("Auth error during login: %s", err)
                errors["base"] = "invalid_auth"
            except requests.exceptions.RequestException as err:
                _LOGGER.warning("Network error during login: %s", err)
                errors["base"] = "cannot_connect"
            except ApiError as err:
                _LOGGER.warning("API error during login: %s", err)
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected error during login")
                errors["base"] = "unknown"

            if not errors:
                try:
                    appliances = await session.async_discover_appliances()
                except requests.exceptions.RequestException as err:
                    _LOGGER.warning("Network error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except ApiError as err:
                    _LOGGER.warning("API error during device discovery: %s", err)
                    errors["base"] = "cannot_connect"
                except Exception:
                    _LOGGER.exception("Unexpected error during device discovery")
                    errors["base"] = "unknown"

            if not errors:
                if not appliances:
                    errors["base"] = "no_devices"
                else:
                    self._email = email
                    self._password = password
                    self._region = region
                    self._appliances = appliances
                    return await self.async_step_device()

        # HA 2026.10+ types data_schema as probatio.Schema, while this
        # integration supports HA 2024.1+ where voluptuous.Schema is required.
        return self.async_show_form(
            step_id="user",
            data_schema=cast(Any, STEP_USER_DATA_SCHEMA),
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
            data_schema=cast(Any, schema),
            errors=errors,
        )


class ILetComfortOptionsFlow(OptionsFlow):
    """Options for an iLetComfort entry.

    Select whether HA owns the account session or coexists read-only with the
    phone app over certificate-authenticated MQTT push (issue #55). Changing
    the mode reloads the entry (see the update listener in ``__init__``).
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        offer_c3_control = self._offers_c3_basic_control()
        if user_input is not None:
            data = dict(user_input)
            options = self.config_entry.options
            if not offer_c3_control and CONF_C3_BASIC_CONTROL in options:
                # Keep an earlier choice when the field is not shown (e.g.
                # the model code is not known yet).
                data[CONF_C3_BASIC_CONTROL] = options[CONF_C3_BASIC_CONTROL]
            return self.async_create_entry(data=data)

        current = self.config_entry.options.get(CONF_OPERATION_MODE)
        if current is None:
            current = (
                OPERATION_MODE_PHONE_APP
                if self.config_entry.options.get(
                    CONF_ENABLE_MQTT_PUSH, DEFAULT_ENABLE_MQTT_PUSH
                )
                else DEFAULT_OPERATION_MODE
            )
        fields: dict[Any, Any] = {
            vol.Required(
                CONF_OPERATION_MODE, default=current,
            ): SelectSelector(
                SelectSelectorConfig(
                    options=[
                        OPERATION_MODE_HA_PRIMARY,
                        OPERATION_MODE_PHONE_APP,
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                    translation_key="operation_mode",
                )
            ),
        }
        if offer_c3_control:
            fields[
                vol.Required(
                    CONF_C3_BASIC_CONTROL,
                    default=self.config_entry.options.get(
                        CONF_C3_BASIC_CONTROL, DEFAULT_C3_BASIC_CONTROL
                    ),
                )
            ] = bool
        return self.async_show_form(
            step_id="init", data_schema=cast(Any, vol.Schema(fields))
        )

    def _offers_c3_basic_control(self) -> bool:
        """Offer C3 basic control only where the loaded unit's dialect supports it."""
        coordinator = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        sn8 = getattr(coordinator, "sn8", None)
        return get_adapter(sn8).offers_c3_basic_control
