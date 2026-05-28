"""DataUpdateCoordinator for the iLetComfort integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
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
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REGION_URLS,
)

_LOGGER = logging.getLogger(__name__)

# Number of consecutive polls in which both status and sensors fall back to
# cache before we surface a "device appears offline" Repair card. At the
# default 60s poll interval this is ~5 minutes — long enough to ignore a
# one-off cloud blip, short enough to be useful when the device is really
# stuck (issue #5).
OFFLINE_REPAIR_THRESHOLD = 5
OFFLINE_REPAIR_ID = "device_offline_{entry_id}"


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
        region = entry.data.get(CONF_REGION, DEFAULT_REGION)
        api_base = REGION_URLS.get(region, REGION_URLS[DEFAULT_REGION])
        self.client = ILetComfortClient(api_base=api_base)
        self.appliance_code: str = entry.data.get(CONF_APPLIANCE_CODE, "")
        self._token_file = (
            Path(hass.config.path(".storage"))
            / f"iletcomfort_token_{entry.entry_id}"
        )
        self._last_on_state: tuple[int, int] | None = None
        # Track per-query cache-fallback state so a persistently failing
        # device (e.g. the truncated frames in issue #5) warns once on entry
        # and stays quiet at DEBUG afterwards instead of spamming every poll.
        self._status_degraded = False
        self._sensors_degraded = False
        self._consecutive_both_degraded = 0
        self._repair_issued = False

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
        except (ApiError, Exception) as err:
            if self.data is not None:
                _LOGGER.warning("Poll error, using cached data: %s", err)
                return self.data
            raise UpdateFailed(f"Error: {err}") from err

    @staticmethod
    def _log_cache_fallback(what: str, err: Exception, already_degraded: bool) -> bool:
        """Log a query cache-fallback, warning only on entry into the degraded state.

        Returns the new degraded flag (always True). The first failure logs at
        WARNING; while the condition persists subsequent polls log at DEBUG so an
        expected, repeating transient (e.g. issue #5 truncated frames) does not
        spam the log every poll.
        """
        msg = "%s query failed, using cache: %s"
        if already_degraded:
            _LOGGER.debug(msg, what, err)
        else:
            _LOGGER.warning(msg, what, err)
        return True

    async def _poll(self) -> dict[str, Any]:
        """Run the actual polling calls in the executor."""
        cached = self.data or {}

        try:
            status: ITSStatus = await self.hass.async_add_executor_job(
                self.client.query_status, self.appliance_code,
            )
            self._status_degraded = False
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            status = cached.get("status")
            if status is None:
                raise
            self._status_degraded = self._log_cache_fallback(
                "Status", err, self._status_degraded,
            )

        await asyncio.sleep(2)

        try:
            sensors: ITSSensors = await self.hass.async_add_executor_job(
                self.client.query_sensors, self.appliance_code,
            )
            self._sensors_degraded = False
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            sensors = cached.get("sensors")
            if sensors is None:
                raise
            self._sensors_degraded = self._log_cache_fallback(
                "Sensors", err, self._sensors_degraded,
            )

        if status.raw_body:
            _LOGGER.debug(
                "STATUS RAW: %s",
                ",".join(f"{b:02x}" for b in status.raw_body),
            )
            _LOGGER.debug(
                "STATUS: mode=%d set_temp=%d tr_temp=%s trdh_def=%s "
                "ef1=0x%02x ef2=0x%02x status_flags=0x%02x",
                status.mode,
                status.set_temperature,
                status.tr_temperature,
                status.trdh_def,
                status.enable_flags_1,
                status.enable_flags_2,
                status.status_flags_raw,
            )

        if sensors.raw_body:
            _LOGGER.debug(
                "SENSORS RAW: %s",
                ",".join(f"{b:02x}" for b in sensors.raw_body),
            )

        # Track last on-state for power restore
        if status.mode != 0:
            set_mode = QUERY_TO_SET_MODE.get(status.mode)
            if set_mode is not None:
                temp = int(status.t5s_def) if status.t5s_def is not None else status.set_temperature
                self._last_on_state = (set_mode, temp)

        self._update_offline_repair()

        return {"status": status, "sensors": sensors}

    def _update_offline_repair(self) -> None:
        """Surface or clear the 'device appears offline' Repair card.

        A user-visible Repair card is created once both queries have been
        falling back to cache for OFFLINE_REPAIR_THRESHOLD consecutive polls
        (issue #5 — vendor cloud / device-offline state). The card is cleared
        on the first poll where either query succeeds, so transient blips
        don't churn the Repairs panel.
        """
        both_degraded = self._status_degraded and self._sensors_degraded

        if both_degraded:
            self._consecutive_both_degraded += 1
            if (
                self._consecutive_both_degraded >= OFFLINE_REPAIR_THRESHOLD
                and not self._repair_issued
            ):
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    OFFLINE_REPAIR_ID.format(entry_id=self.entry.entry_id),
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="device_offline",
                    translation_placeholders={
                        "appliance_code": self.appliance_code,
                    },
                )
                self._repair_issued = True
            return

        self._consecutive_both_degraded = 0
        if self._repair_issued:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                OFFLINE_REPAIR_ID.format(entry_id=self.entry.entry_id),
            )
            self._repair_issued = False

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
        """Send a SET command with auto re-auth, then refresh data."""
        try:
            await self.hass.async_add_executor_job(
                lambda: self.client.set_device(
                    self.appliance_code,
                    last_on_state=self._last_on_state,
                    **kwargs,
                )
            )
        except AuthError:
            _LOGGER.info("Auth error during set, re-authenticating")
            await self._async_login()
            await self.hass.async_add_executor_job(
                lambda: self.client.set_device(
                    self.appliance_code,
                    last_on_state=self._last_on_state,
                    **kwargs,
                )
            )
        await self.async_request_refresh()
