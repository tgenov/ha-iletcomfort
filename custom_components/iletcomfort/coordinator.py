"""DataUpdateCoordinator for the iLetComfort integration."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HassJob, HassJobType, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import requests

from .api import (
    AppCert,
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSSensors,
    ITSStatus,
    QUERY_TO_SET_MODE,
    mask_identifier,
)
from .c3_basic import C3BasicStatus
from .capabilities import DeviceCapabilities, ProbeState
from .const import (
    CONF_APPLIANCE_CODE,
    CONF_C3_BASIC_CONTROL,
    CONF_ENABLE_MQTT_PUSH,
    CONF_KJRH120L_ZONE1_CAPABLE,
    CONF_OPERATION_MODE,
    CONF_REGION,
    DEFAULT_C3_BASIC_CONTROL,
    DEFAULT_ENABLE_MQTT_PUSH,
    DEFAULT_OPERATION_MODE,
    DEFAULT_REGION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    OPERATION_MODE_PHONE_APP,
    REGION_URLS,
    MQTT_STATUS_STALE_AFTER,
)
from .model_profiles import (
    ATW_ECO_MIN_LEN,
    ATW_ECO_SUBTYPE,
    ATW_HMI_PARAMS_MIN_LEN,
    ATW_HMI_PARAMS_SUBTYPE,
    ATW_SILENT_MIN_LEN,
    ATW_SILENT_SUBTYPE,
    ATW_UNIT_PARAMS_MIN_LEN,
    ATW_UNIT_PARAMS_SUBTYPE,
    ModelProfile,
    apply_atw_eco,
    apply_atw_hmi_params,
    apply_atw_silent,
    apply_atw_unit_params,
    atw_sensors_are_placeholder,
    kjrh120l_has_zone1,
    resolve_profile,
)
from .mqtt import ILetComfortPushClient
from .protocols import HeatPumpState, get_adapter, resolve_state

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
        self._region = region
        operation_mode = entry.options.get(CONF_OPERATION_MODE)
        if operation_mode is None:
            operation_mode = (
                OPERATION_MODE_PHONE_APP
                if entry.options.get(
                    CONF_ENABLE_MQTT_PUSH, DEFAULT_ENABLE_MQTT_PUSH
                )
                else DEFAULT_OPERATION_MODE
            )
        self._operation_mode: str = operation_mode
        # Options this coordinator was built with. The entry update listener
        # reloads only when these change, so a data-only update (the KJRH-120L
        # Zone 1 latch) does not reload the entry.
        self.options_snapshot: dict[str, Any] = dict(entry.options)
        self._push_enabled = operation_mode == OPERATION_MODE_PHONE_APP
        self._push_client: ILetComfortPushClient | None = None
        # Cloud metadata for this appliance (applianceType, modelNumber, sn8, …),
        # cached for model selection and diagnostics. Failed/incomplete lookups
        # are retried until the model code is available. See
        # ``_ensure_appliance_meta``.
        self.appliance_meta: dict[str, Any] | None = None
        self._token_file = (
            Path(hass.config.path(".storage"))
            / f"iletcomfort_token_{entry.entry_id}"
        )
        self._last_on_state: tuple[int, int] | None = None
        # Serialises writes for this appliance: a C3 basic SET is built from a
        # fresh status read, so overlapping read/modify/write transactions
        # would otherwise undo each other's changes.
        self._write_lock = asyncio.Lock()
        self._c3_basic_control: bool = bool(
            entry.options.get(CONF_C3_BASIC_CONTROL, DEFAULT_C3_BASIC_CONTROL)
        )
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
        self._last_push_status_at: float | None = None
        self._push_status_watchdog: Callable[[], None] | None = None
        # Centralized runtime probe / capability state (#171).
        self.capabilities = DeviceCapabilities()

    @property
    def _atw_unit_params_supported(self) -> bool | None:
        return self.capabilities.atw_unit_params.supported

    @_atw_unit_params_supported.setter
    def _atw_unit_params_supported(self, value: bool | None) -> None:
        self.capabilities.atw_unit_params.supported = value

    @property
    def _atw_hmi_params_supported(self) -> bool | None:
        return self.capabilities.atw_hmi_params.supported

    @_atw_hmi_params_supported.setter
    def _atw_hmi_params_supported(self, value: bool | None) -> None:
        self.capabilities.atw_hmi_params.supported = value

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

    @property
    def c3_basic_control_enabled(self) -> bool:
        """Return whether the owner opted into C3 basic control for this unit."""
        return self._c3_basic_control

    @property
    def c3_basic(self) -> C3BasicStatus | None:
        """Decoded C3 basic status for an ATW unit (sn8 171H120F), else None."""
        state = resolve_state(self.sn8, self.data)
        if state is None:
            return None
        basic = state.atw_c3_basic
        return basic if isinstance(basic, C3BasicStatus) else None

    @property
    def c3_basic_controllable(self) -> C3BasicStatus | None:
        """The C3 basic status when the owner enabled C3 basic control.

        Control is opt-in (options flow): it is hardware-validated on one
        model variant only, so other 171H120F variants keep their existing
        behaviour until an owner confirms it on their unit.
        """
        return self.c3_basic if self._c3_basic_control else None

    @property
    def kjrh120l_zone1_capable(self) -> bool:
        """Return whether this KJRH-120L has ever reported a Zone 1.

        True once ``_observe_kjrh120l_zone1`` has latched the flag in
        ``entry.data``. The profile is checked as well, so the flag never
        enables Zone 1 entities on another model.
        """
        return (
            resolve_profile(self.sn8) is ModelProfile.KJRH120L
            and self.entry.data.get(CONF_KJRH120L_ZONE1_CAPABLE) is True
        )

    @callback
    def _observe_kjrh120l_zone1(self, status: ITSStatus) -> None:
        """Latch Zone 1 capability the first time a KJRH-120L frame shows it.

        ``kjrh120l_has_zone1`` reads body[8:10], the Zone 1 / Zone 2 power
        state, which drops to 00 00 while Zone 1 is off (snapshot 2026-11-18).
        One positive frame is therefore kept in ``entry.data`` and never
        cleared by a later frame.
        """
        if self.kjrh120l_zone1_capable:
            return
        if resolve_profile(self.sn8) is not ModelProfile.KJRH120L:
            return
        if not kjrh120l_has_zone1(status.raw_body):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_KJRH120L_ZONE1_CAPABLE: True},
        )

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
        previous_profile = resolve_profile(self.sn8)
        await self._ensure_appliance_meta()
        if resolve_profile(self.sn8) is not previous_profile:
            # Cached values and power-restore state used the old byte layout.
            # They cannot be used as fallback for the newly discovered model.
            self.data = None  # type: ignore[assignment]
            self._last_on_state = None
            self.capabilities.reset_probes()
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

        # Apply any model-specific sensors override via the resolved protocol
        # adapter (e.g. ATW/AQUAPURA route tank temp into th_temp).
        adapter = get_adapter(sn8)
        profile = adapter.profile
        sensors = adapter.decode_sensors(sensors, status)

        raw_frames: dict[int, bytes] = {}
        if status.raw_body:
            raw_frames[0x01] = bytes(status.raw_body)
        if self._sensors_degraded:
            cached_0x02 = (cached.get("raw_frames") or {}).get(0x02)
            if cached_0x02:
                raw_frames[0x02] = bytes(cached_0x02)
        elif sensors.raw_body and sensors.raw_body[:1] == b"\x02":
            raw_frames[0x02] = bytes(sensors.raw_body)

        # On ATW (171H120F) units whose 0x02 frame is a non-telemetry schedule
        # template, probe subtype 0x10 once for live telemetry (#120). Units
        # that reject 0x10 on the initial probe (e.g. code=1214 on Rotenso)
        # latch unsupported; transient errors after 0x10 is already confirmed
        # retry on the next poll without permanently disabling telemetry.
        if profile is ModelProfile.ATW and atw_sensors_are_placeholder(
            raw_frames.get(0x02, b"")
        ):
            unit_body = await self._async_probe_atw_subtype(
                label="unit parameters",
                subtype=ATW_UNIT_PARAMS_SUBTYPE,
                min_len=ATW_UNIT_PARAMS_MIN_LEN,
                probe=self.capabilities.atw_unit_params,
                fetcher=self.client.query_atw_unit_params,
                raw_frames=raw_frames,
            )
            if unit_body is not None:
                status, sensors = apply_atw_unit_params(unit_body, status, sensors)
                hmi_body = await self._async_probe_atw_subtype(
                    label="HMI parameters",
                    subtype=ATW_HMI_PARAMS_SUBTYPE,
                    min_len=ATW_HMI_PARAMS_MIN_LEN,
                    probe=self.capabilities.atw_hmi_params,
                    fetcher=self.client.query_atw_hmi_params,
                    raw_frames=raw_frames,
                )
                if hmi_body is not None:
                    status = apply_atw_hmi_params(hmi_body, status)

        # Probe 0x05 (Silent/Super-Silent) and 0x07 (ECO) on C3 basic ATW units (#139/#188).
        if profile is ModelProfile.ATW and status.c3_basic is not None:
            silent_body = await self._async_probe_atw_subtype(
                label="silent",
                subtype=ATW_SILENT_SUBTYPE,
                min_len=ATW_SILENT_MIN_LEN,
                probe=self.capabilities.atw_silent,
                fetcher=self.client.query_atw_silent,
                raw_frames=raw_frames,
            )
            if silent_body is not None:
                status = apply_atw_silent(silent_body, status)
            eco_body = await self._async_probe_atw_subtype(
                label="eco",
                subtype=ATW_ECO_SUBTYPE,
                min_len=ATW_ECO_MIN_LEN,
                probe=self.capabilities.atw_eco,
                fetcher=self.client.query_atw_eco,
                raw_frames=raw_frames,
            )
            if eco_body is not None:
                status = apply_atw_eco(eco_body, status)

        self._update_offline_repair()
        self._observe_kjrh120l_zone1(status)

        state = HeatPumpState(
            profile=profile,
            variant=adapter.describe_variant(status),
            status=status,
            sensors=sensors,
            raw_frames=dict(raw_frames),
            has_authoritative_raw_frames=True,
        )
        return {
            "status": status,
            "sensors": sensors,
            "raw_frames": raw_frames,
            "state": state,
        }

    async def _async_probe_atw_subtype(
        self,
        *,
        label: str,
        subtype: int,
        min_len: int,
        probe: ProbeState,
        fetcher: Callable[[str], bytes],
        raw_frames: dict[int, bytes],
    ) -> bytes | None:
        """Probe one optional ATW C3 subtype, disabling for the session on ApiError."""
        if not probe.should_probe:
            return None
        try:
            body = await self.hass.async_add_executor_job(
                fetcher, self.appliance_code,
            )
        except AuthError:
            raise
        except ApiError as err:
            if probe.supported is None:
                probe.mark_unsupported(str(err))
                _LOGGER.info(
                    "ATW subtype 0x%02X %s not supported (%s); "
                    "disabling 0x%02X polling for this session",
                    subtype,
                    label,
                    err,
                    subtype,
                )
            else:
                _LOGGER.debug(
                    "Transient ATW subtype 0x%02X %s ApiError (retrying next poll): %s",
                    subtype,
                    label,
                    err,
                )
        except Exception as err:  # noqa: BLE001 — transient error, retry later
            _LOGGER.debug(
                "ATW subtype 0x%02X %s query failed: %s", subtype, label, err
            )
        else:
            if isinstance(body, (bytes, bytearray)) and len(body) >= min_len:
                probe.mark_supported()
                raw = bytes(body)
                raw_frames[subtype] = raw
                return raw
        return None

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
        """Cache this appliance's cloud metadata for model selection.

        The ``list_appliances`` response carries fields (e.g. ``applianceType``,
        ``modelNumber``, ``sn8``) that a maintainer can use to identify the
        device class for model-specific frame decoding (issue #22). This is
        used for both decoding and control. A failed lookup is retried on the
        next poll; it is logged at DEBUG and leaves ``appliance_meta`` as None.
        """
        if self.sn8 is not None:
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
        except Exception as err:  # noqa: BLE001 — retry on the next poll
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

        await self.async_start_push()

    async def async_execute_command(self, action: Callable[[], Any]) -> Any:
        """Run a blocking write command with uniform safety, re-auth, and refresh.

        Centralizes the write contract for every protocol dialect (#172):
        1. Rejects writes in read-only phone-app coexistence mode.
        2. Serialises transactions per appliance via ``self._write_lock``
           (spanning fresh read → SET → any auth retry).
        3. Retries once on ``AuthError`` after re-authenticating.
        4. Translates client/validation/network errors into ``HomeAssistantError``
           without triggering a spurious refresh.
        5. Requests a coordinator refresh on success.
        """
        if self._operation_mode == OPERATION_MODE_PHONE_APP:
            raise HomeAssistantError(
                "Control is disabled in phone app coexistence mode; "
                "switch to HA primary mode to send commands"
            )

        try:
            async with self._write_lock:
                try:
                    result = await self.hass.async_add_executor_job(action)
                except AuthError:
                    _LOGGER.info("Auth error during set, re-authenticating")
                    await self._async_login()
                    result = await self.hass.async_add_executor_job(action)
        except (
            ApiError,
            AuthError,
            ValueError,
            requests.RequestException,
        ) as err:
            raise HomeAssistantError(f"Heat pump command failed: {err}") from err
        await self.async_request_refresh()
        return result

    async def async_set_device(self, **kwargs: Any) -> None:
        """Send a SET command with auto re-auth, then refresh data.

        The appliance ``sn8`` is forwarded so the client can branch the write
        encoding per model (e.g. the KJRH-120L's short commands vs the legacy
        C3 SET frame); see ``model_profiles`` and ``ILetComfortClient.set_device``.
        """
        sn8 = self.sn8

        def _send() -> Any:
            return self.client.set_device(
                self.appliance_code,
                sn8=sn8,
                last_on_state=self._last_on_state,
                **kwargs,
            )

        await self.async_execute_command(_send)

    def _require_c3_basic_control(self) -> None:
        if self.c3_basic_controllable is None:
            raise HomeAssistantError(
                "C3 basic control is not enabled or not supported for this unit"
            )

    async def async_set_c3_basic(self, **changes: Any) -> None:
        """Change C3 basic fields (Zone-1/DHW power, setpoints, curve)."""
        self._require_c3_basic_control()
        sn8 = self.sn8
        await self.async_execute_command(
            lambda: self.client.set_c3_basic(self.appliance_code, sn8=sn8, **changes)
        )

    async def async_set_c3_feature(
        self,
        feature: str,
        on: bool,
        *,
        silent_level: int | None = None,
    ) -> None:
        """Switch the C3 silent (with optional level 0/1/2) or ECO mode."""
        self._require_c3_basic_control()
        sn8 = self.sn8
        await self.async_execute_command(
            lambda: self.client.set_c3_feature(
                self.appliance_code,
                sn8=sn8,
                feature=feature,
                on=on,
                silent_level=silent_level,
            )
        )

    # -- MQTT real-time push (issue #55, phone-app coexistence mode) --

    async def async_start_push(self) -> None:
        """Start the MQTT push listener if the option is enabled.

        No-op in HA-primary mode. Phone-app coexistence disables account polling
        before connecting; if push cannot start, entities are marked unavailable
        rather than silently restarting the login war.
        """
        if not self._push_enabled or self._push_client is not None:
            return
        self.update_interval = None
        self._push_client = ILetComfortPushClient(
            self.hass,
            self.client,
            region=self._region,
            appliance_code=self.appliance_code,
            on_status=self._push_status_threadsafe,
            on_connected_change=self._push_connected_threadsafe,
            get_certificate=self._async_get_push_certificate,
        )
        try:
            await self._push_client.async_start()
        except Exception as err:  # noqa: BLE001 — push is best-effort
            _LOGGER.warning(
                "MQTT push failed to start; phone app mode remains read-only "
                "and unavailable: %s",
                err,
            )
            self._push_client = None
            self.async_set_update_error(
                UpdateFailed("MQTT push failed to start in phone app mode")
            )

    async def _async_get_push_certificate(self) -> AppCert:
        """Mint a push certificate, re-authenticating only when required."""
        try:
            return await self.hass.async_add_executor_job(
                self.client.create_app_cert
            )
        except AuthError:
            _LOGGER.info("Auth error during MQTT certificate issuance or renewal")
            await self._async_login()
            return await self.hass.async_add_executor_job(
                self.client.create_app_cert
            )

    async def async_stop_push(self) -> None:
        """Stop the MQTT push listener, if running."""
        self._cancel_push_status_watchdog()
        self._last_push_status_at = None
        if self._push_client is None:
            return
        try:
            await self._push_client.async_stop()
        finally:
            self._push_client = None

    def _push_status_threadsafe(self, status: ITSStatus) -> None:
        """Marshal a status push from paho's thread onto the event loop."""
        self.hass.loop.call_soon_threadsafe(self._apply_push_status, status)

    def _push_connected_threadsafe(self, connected: bool) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_push_connected, connected)

    def _apply_push_status(self, status: ITSStatus) -> None:
        """Publish a pushed status to entities, reusing the last polled sensors.

        The push carries only the status frame, so sensor-only values retain the
        initial snapshot from setup. Profile re-decoding matches the poll path so
        push and poll agree for values carried by the status frame.
        """
        self._last_push_status_at = time.monotonic()
        self._schedule_push_status_watchdog()
        if self.data is None:
            # Push started after the first poll, so this is only reached if a
            # push races ahead of it. Ignore it rather than publish a status
            # with no sensors (entities read coordinator.data["sensors"]); the
            # imminent first poll delivers a complete snapshot.
            return
        adapter = get_adapter(self.sn8)
        status = adapter.decode_status(status)
        self._observe_kjrh120l_zone1(status)
        raw_frames = dict(self.data.get("raw_frames") or {})
        if status.raw_body:
            raw_frames[0x01] = bytes(status.raw_body)
        sensors = self.data.get("sensors")
        state = HeatPumpState(
            profile=adapter.profile,
            variant=adapter.describe_variant(status),
            status=status,
            sensors=sensors,
            raw_frames=dict(raw_frames),
            has_authoritative_raw_frames=True,
        )
        self.async_set_updated_data(
            {
                "status": status,
                "sensors": sensors,
                "raw_frames": raw_frames,
                "state": state,
            }
        )

    def _cancel_push_status_watchdog(self) -> None:
        if self._push_status_watchdog is not None:
            self._push_status_watchdog()
            self._push_status_watchdog = None

    @callback
    def _on_push_status_watchdog(self, _now: Any) -> None:
        """Run the MQTT status freshness check on the Home Assistant event loop."""
        self._check_push_status_freshness()

    def _schedule_push_status_watchdog(self) -> None:
        self._cancel_push_status_watchdog()
        self._push_status_watchdog = async_call_later(
            self.hass,
            MQTT_STATUS_STALE_AFTER,
            HassJob(
                self._on_push_status_watchdog,
                "iLetComfort MQTT status freshness",
                cancel_on_shutdown=True,
                job_type=HassJobType.Callback,
            ),
        )

    def _check_push_status_freshness(self) -> None:
        """Mark push unavailable when the connected device stops publishing status."""
        self._push_status_watchdog = None
        if not self._push_enabled or self._operation_mode != OPERATION_MODE_PHONE_APP:
            return
        if self._last_push_status_at is None or (
            time.monotonic() - self._last_push_status_at
            >= MQTT_STATUS_STALE_AFTER
        ):
            self.async_set_update_error(
                UpdateFailed("MQTT device status heartbeat expired")
            )
            return
        self._schedule_push_status_watchdog()

    def _apply_push_connected(self, connected: bool) -> None:
        """Expose transport and device-status freshness without account polling."""
        if self._operation_mode == OPERATION_MODE_PHONE_APP:
            # Polling requires the account session and would periodically evict
            # the official app. Certificate push is the sole transport in this
            # explicitly read-only coexistence mode.
            self.update_interval = None
            if connected:
                self._schedule_push_status_watchdog()
                if not self.last_update_success and self.data is not None:
                    self.async_set_updated_data(self.data)
            else:
                self._cancel_push_status_watchdog()
                self._last_push_status_at = None
                self.async_set_update_error(
                    UpdateFailed("MQTT push disconnected in phone app mode")
                )
