"""DataUpdateCoordinator for the iLetComfort integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import (
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSSensors,
    ITSStatus,
    QUERY_TO_SET_MODE,
    MODE_OFF,
)
from .const import CONF_APPLIANCE_CODE, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class ILetComfortCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls the iLetComfort cloud API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.entry = entry
        self.client = ILetComfortClient()
        self.appliance_code: str = entry.data.get(CONF_APPLIANCE_CODE, "")
        self._token_file = Path(hass.config.path(".storage")) / "iletcomfort_token"
        self._last_on_state: tuple[int, int] | None = None

    @property
    def last_on_state(self) -> tuple[int, int] | None:
        """Return the last known on-state (set_mode, temperature)."""
        return self._last_on_state

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status and sensors from the heat pump."""
        try:
            return await self._poll()
        except AuthError:
            _LOGGER.info("Auth error during poll, re-authenticating")
            try:
                await self._async_login()
                return await self._poll()
            except (AuthError, ApiError) as err:
                raise UpdateFailed(f"Re-auth failed: {err}") from err
        except ApiError as err:
            raise UpdateFailed(f"API error: {err}") from err
        except Exception as err:
            raise UpdateFailed(f"Unexpected error: {err}") from err

    async def _poll(self) -> dict[str, Any]:
        """Run the actual polling calls in the executor."""
        status: ITSStatus = await self.hass.async_add_executor_job(
            self.client.query_status, self.appliance_code,
        )
        sensors: ITSSensors = await self.hass.async_add_executor_job(
            self.client.query_sensors, self.appliance_code,
        )

        # Track last on-state for power restore
        if status.mode != 0:
            set_mode = QUERY_TO_SET_MODE.get(status.mode)
            if set_mode is not None:
                self._last_on_state = (set_mode, status.set_temperature)

        return {"status": status, "sensors": sensors}

    async def _async_login(self) -> None:
        """Authenticate and store the token."""
        email = self.entry.data[CONF_EMAIL]
        password = self.entry.data[CONF_PASSWORD]

        data = await self.hass.async_add_executor_job(
            self.client.login, email, password,
        )

        # Save token to HA storage
        await self.hass.async_add_executor_job(
            self.client.save_token, self._token_file,
        )

        # Auto-discover appliance if not set
        if not self.appliance_code:
            appliances = await self.hass.async_add_executor_job(
                self.client.list_appliances,
            )
            if appliances:
                self.appliance_code = str(appliances[0].get("applianceCode", ""))
                _LOGGER.info("Discovered appliance: %s", self.appliance_code)

    async def async_first_refresh_with_login(self) -> None:
        """Login first, then do the initial data refresh."""
        # Try loading saved token first
        token_loaded = await self.hass.async_add_executor_job(
            self.client.load_token, self._token_file,
        )
        if not token_loaded:
            await self._async_login()

        await self.async_config_entry_first_refresh()

    async def async_set_device(self, **kwargs: Any) -> None:
        """Send a SET command and refresh data."""
        await self.hass.async_add_executor_job(
            lambda: self.client.set_device(
                self.appliance_code,
                last_on_state=self._last_on_state,
                **kwargs,
            )
        )
        await self.async_request_refresh()
