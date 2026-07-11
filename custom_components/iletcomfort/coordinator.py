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
    mask_identifier,
)
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REGION_URLS,
)
from .model_profiles import apply_profile_to_sensors, resolve_profile

_LOGGER = logging.getLogger(__name__)

# Number of consecutive polls in which both status and sensors fall back to
# cache before we surface a "device appears offline" Repair card. At the
# default 60s poll interval this is ~5 minutes — long enough to ignore a
# one-off cloud blip, short enough to be useful when the device is really
# stuck (issue #5).
OFFLINE_REPAIR_THRESHOLD = 5
OFFLINE_REPAIR_ID = "device_offline_{entry_id}"

# Number of *consecutive* polls a single query (status or sensors) must fall
# back to cache before its cache-fallback is escalated to a WARNING. On a flaky
# vendor cloud (transient 502/RemoteDisconnected/code=1214 or a local DNS blip)
# an isolated or intermittent failure is expected and harmless — the poll reuses
# the last good data and recovers next time — so those stay at DEBUG. Only a
# failure that *persists* this many polls (~5 min at the 60s interval, the same
# cadence as the offline Repair card) warrants a single WARNING (issue #44).
SUSTAINED_FAILURE_THRESHOLD = 5


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
        # Cloud metadata for this appliance (applianceType, modelNumber, sn8, …),
        # fetched once and surfaced in diagnostics only. See
        # ``_ensure_appliance_meta``.
        self.appliance_meta: dict[str, Any] | None = None
        self._token_file = (
            Path(hass.config.path(".storage"))
            / f"iletcomfort_token_{entry.entry_id}"
        )
        self._last_on_state: tuple[int, int] | None = None
        # Track per-query cache-fallback state. ``_status_degraded`` /
        # ``_sensors_degraded`` drive the offline Repair card (issue #5). The
        # ``*_fail_streak`` counters drive log-level escalation: a query only
        # WARNs once its cache-fallback has persisted SUSTAINED_FAILURE_THRESHOLD
        # consecutive polls, so a flaky-cloud blip stays at DEBUG (issue #44).
        self._status_degraded = False
        self._sensors_degraded = False
        self._status_fail_streak = 0
        self._sensors_fail_streak = 0
        self._consecutive_both_degraded = 0
        self._repair_issued = False

    @property
    def last_on_state(self) -> tuple[int, int] | None:
        """Return the last known on-state (set_mode, temperature)."""
        return self._last_on_state

    @property
    def sn8(self) -> str | None:
        """Return this appliance's sn8 model code, if known.

        Read from the cached cloud metadata (``appliance_meta``); it selects the
        model decode profile (issue #22 / #12). None when metadata is absent,
        which resolves to the STANDARD profile.
        """
        if self.appliance_meta is None:
            return None
        sn8 = self.appliance_meta.get("sn8")
        return str(sn8) if sn8 else None

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
    def _log_cache_fallback(what: str, err: Exception, fail_streak: int) -> int:
        """Log a query cache-fallback and return the updated failure streak.

        ``fail_streak`` is the number of consecutive polls this query has fallen
        back to cache, *before* this failure. A one-off or intermittent blip on a
        flaky vendor cloud is expected and harmless (the poll reuses cached data
        and recovers next time), so it logs at DEBUG. Only when the streak reaches
        SUSTAINED_FAILURE_THRESHOLD do we escalate to a single WARNING; further
        sustained failures fall back to DEBUG so the log isn't flooded (issue #44).
        """
        fail_streak += 1
        msg = "%s query failed, using cache: %s"
        if fail_streak == SUSTAINED_FAILURE_THRESHOLD:
            _LOGGER.warning(msg, what, err)
        else:
            _LOGGER.debug(msg, what, err)
        return fail_streak

    @staticmethod
    def _note_query_recovery(what: str, fail_streak: int) -> int:
        """Reset a query's failure streak on success, returning 0.

        If the query had been in the sustained-WARNING state (its streak reached
        SUSTAINED_FAILURE_THRESHOLD), emit a single INFO so a genuinely stuck
        query that recovers leaves a matching "recovered" breadcrumb; ordinary
        blips below the threshold recover silently (issue #44).
        """
        if fail_streak >= SUSTAINED_FAILURE_THRESHOLD:
            _LOGGER.info(
                "%s query recovered after %d sustained cache-fallback polls",
                what,
                fail_streak,
            )
        return 0

    async def _poll(self) -> dict[str, Any]:
        """Run the actual polling calls in the executor."""
        cached = self.data or {}
        sn8 = self.sn8

        try:
            status: ITSStatus = await self.hass.async_add_executor_job(
                self.client.query_status, self.appliance_code, sn8,
            )
            self._status_degraded = False
            self._status_fail_streak = self._note_query_recovery(
                "Status", self._status_fail_streak,
            )
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            cached_status = cached.get("status")
            if cached_status is None:
                raise
            status = cached_status
            self._status_degraded = True
            self._status_fail_streak = self._log_cache_fallback(
                "Status", err, self._status_fail_streak,
            )

        await asyncio.sleep(2)

        try:
            sensors: ITSSensors = await self.hass.async_add_executor_job(
                self.client.query_sensors, self.appliance_code, sn8,
            )
            self._sensors_degraded = False
            self._sensors_fail_streak = self._note_query_recovery(
                "Sensors", self._sensors_fail_streak,
            )
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            cached_sensors = cached.get("sensors")
            if cached_sensors is None:
                raise
            sensors = cached_sensors
            self._sensors_degraded = True
            self._sensors_fail_streak = self._log_cache_fallback(
                "Sensors", err, self._sensors_fail_streak,
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
            _LOGGER.debug(
                "SENSORS: t3=%s t4=%s t2=%s twin=%s twout=%s "
                "th=%s tf=%s tp=%s t1=%s",
                sensors.t3_temp,
                sensors.t4_temp,
                sensors.t2_temp,
                sensors.twin_temp,
                sensors.twout_temp,
                sensors.th_temp,
                sensors.tf_temp,
                sensors.tp_temp,
                sensors.t1_temp,
            )

        # Track last on-state for power restore
        if status.mode != 0:
            set_mode = QUERY_TO_SET_MODE.get(status.mode)
            if set_mode is not None:
                temp = int(status.t5s_def) if status.t5s_def is not None else status.set_temperature
                self._last_on_state = (set_mode, temp)

        # Apply any model-specific sensors override (e.g. ATW/AQUAPURA route a
        # tank/water temp into twin_temp, the field the climate
        # current_temperature and Water Inlet sensor read). STANDARD is a no-op.
        sensors = apply_profile_to_sensors(resolve_profile(sn8), sensors, status)

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
                        "appliance_code": mask_identifier(self.appliance_code),
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

        await self.hass.async_add_executor_job(
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
                _LOGGER.info(
                    "Discovered appliance: %s",
                    mask_identifier(self.appliance_code),
                )

    async def _ensure_appliance_meta(self) -> None:
        """Cache this appliance's cloud metadata for diagnostics (best-effort).

        The ``list_appliances`` response carries fields (e.g. ``applianceType``,
        ``modelNumber``, ``sn8``) that a maintainer can use to identify the
        device class for model-specific frame decoding (issue #22). This is
        purely diagnostic — it never affects decoding or polling — so it must
        not raise or block setup: any failure is logged at DEBUG and leaves
        ``appliance_meta`` as None.
        """
        if self.appliance_meta is not None:
            return
        try:
            appliances = await self.hass.async_add_executor_job(
                self.client.list_appliances,
            )
            if not appliances:
                return
            for appliance in appliances:
                if str(appliance.get("applianceCode", "")) == str(self.appliance_code):
                    self.appliance_meta = appliance
                    return
            # No code match: if there's exactly one appliance, assume it's ours.
            if len(appliances) == 1:
                self.appliance_meta = appliances[0]
        except Exception as err:  # noqa: BLE001 — diagnostic-only, must not block setup
            _LOGGER.debug("Could not fetch appliance metadata: %s", err)

    async def async_first_refresh_with_login(self) -> None:
        """Login first, then do the initial data refresh."""
        # Try loading saved token first
        token_loaded = await self.hass.async_add_executor_job(
            self.client.load_token, self._token_file,
        )
        if not token_loaded:
            await self._async_login()

        await self._ensure_appliance_meta()

        await self.async_config_entry_first_refresh()

    async def async_set_device(self, **kwargs: Any) -> None:
        """Send a SET command with auto re-auth, then refresh data.

        The appliance ``sn8`` is forwarded so the client can branch the write
        encoding per model (e.g. the KJRH-120L's short commands vs the legacy
        C3 SET frame); see ``model_profiles`` and ``ILetComfortClient.set_device``.
        """
        sn8 = self.sn8
        try:
            await self.hass.async_add_executor_job(
                lambda: self.client.set_device(
                    self.appliance_code,
                    sn8=sn8,
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
                    sn8=sn8,
                    last_on_state=self._last_on_state,
                    **kwargs,
                )
            )
        await self.async_request_refresh()
