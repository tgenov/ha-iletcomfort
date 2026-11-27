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
    QUERY_TO_SET_MODE,
    ApiError,
    AppCert,
    AuthError,
    ILetComfortClient,
    ITSSensors,
    ITSStatus,
    mask_identifier,
)
from .c3_basic import C3BasicStatus
from .capabilities import DeviceCapabilities, ProbeState
from .commands import HeatPumpCommand
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
from .mqtt import ILetComfortPushClient
from .protocols.atw import (
    ATW_ECO_SUBTYPE,
    ATW_HMI_PARAMS_SUBTYPE,
    ATW_SILENT_SUBTYPE,
    ATW_UNIT_PARAMS_SUBTYPE,
)
from .protocols import (
    ClimateView,
    OptionalProbe,
    EntityCapabilities,
    HeatPumpState,
    ViewContext,
    build_snapshot,
    get_adapter,
    merge_atw_energy_notify,
    project_climate_view,
    project_entity_capabilities,
    project_zone2_climate_view,
    run_write_plan,
    WriteContext,
    WritePlan,
    WriteRefused,
)
from .session import DollinSessionManager, SessionPolicyError

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


class _ClientFileTokenStore:
    """TokenStore adapter delegating to client.load_token/save_token (#109)."""

    def __init__(self, client: ILetComfortClient, filepath: Path) -> None:
        self._client = client
        self._filepath = filepath

    def load(self) -> str | None:
        if self._client.load_token(self._filepath):
            token = self._client.access_token
            if isinstance(token, str) and token:
                return token
            return "__loaded__"
        return None

    def save(self, token: str | None) -> None:
        if token is not None:
            self._client.access_token = token
        self._client.save_token(self._filepath)


class ILetComfortCoordinator(DataUpdateCoordinator[HeatPumpState]):
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
        self.session = DollinSessionManager(
            self.client,
            email=entry.data.get(CONF_EMAIL, ""),
            password=entry.data.get(CONF_PASSWORD, ""),
            token_store=_ClientFileTokenStore(self.client, self._token_file),
            operation_mode=operation_mode,
            run_blocking=hass.async_add_executor_job,
            reauth_hook=lambda: self._async_login(),
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
        # Last valid optional ATW subtype bodies (0x10, 0x0A, 0x05, 0x07),
        # retained as transient-error fallback so a single cloud blip never
        # drops telemetry to unknown or resets TOTAL_INCREASING energy (#118).
        self._last_atw_subtypes: dict[int, bytes] = {}
        # Sentinel-filtered ATW 0x04 energy state from MQTT pushes (latest
        # valid value of each field). raw_frames[0x04] keeps the literal frame.
        self._atw_energy_notify: bytes | None = None

    @property
    def _operation_mode(self) -> str:
        return self.session.policy.operation_mode

    @_operation_mode.setter
    def _operation_mode(self, value: str) -> None:
        self.session.set_operation_mode(value)

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
        if self.data is None:
            return None
        basic = self.data.atw_c3_basic
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
        ``entry.data``. The dialect is checked as well, so the flag never
        enables Zone 1 entities on a dialect that does not latch it.
        """
        return (
            get_adapter(self.sn8).latches_zone1_capability
            and self.entry.data.get(CONF_KJRH120L_ZONE1_CAPABLE) is True
        )

    @property
    def view_context(self) -> ViewContext:
        """Owner options and latched observations for dialect projections (#192)."""
        return ViewContext(
            c3_basic_control=self._c3_basic_control,
            kjrh120l_zone1_latched=self.kjrh120l_zone1_capable,
        )

    @property
    def entity_capabilities(self) -> EntityCapabilities:
        """Which optional entities the selected dialect exposes now (#192)."""
        return project_entity_capabilities(self.sn8, self.data, self.view_context)

    @property
    def climate_view(self) -> ClimateView:
        """The main climate entity as the selected dialect projects it (#192)."""
        return project_climate_view(self.sn8, self.data, self.view_context)

    @property
    def zone2_climate_view(self) -> ClimateView:
        """The Zone-2 climate entity as the selected dialect projects it (#192)."""
        return project_zone2_climate_view(self.sn8, self.data, self.view_context)

    @callback
    def _observe_kjrh120l_zone1(self, status: ITSStatus) -> None:
        """Latch Zone 1 capability the first time a KJRH-120L frame shows it.

        The KJRH-120L dialect's ``observed_zone1`` (``kjrh120l_has_zone1``)
        reads body[8:10], the Zone 1 / Zone 2 power
        state, which drops to 00 00 while Zone 1 is off (snapshot 2026-11-18).
        One positive frame is therefore kept in ``entry.data`` and never
        cleared by a later frame.
        """
        if self.kjrh120l_zone1_capable:
            return
        adapter = get_adapter(self.sn8)
        if not adapter.latches_zone1_capability:
            return
        if not adapter.observed_zone1(status):
            return
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={**self.entry.data, CONF_KJRH120L_ZONE1_CAPABLE: True},
        )

    async def _async_update_data(self) -> HeatPumpState:
        """Fetch status and sensors from the heat pump."""
        try:
            return await self.session.async_run_authenticated_coro(
                self._poll,
                reason="poll",
                reauth_error_types=(ApiError,),
            )
        except AuthError as err:
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

    async def _poll(self) -> HeatPumpState:
        """Run the actual polling calls in the executor."""
        previous_adapter = get_adapter(self.sn8)
        await self._ensure_appliance_meta()
        if get_adapter(self.sn8) is not previous_adapter:
            # Cached values and power-restore state used the old byte layout.
            # They cannot be used as fallback for the newly discovered model.
            self.data = None  # type: ignore[assignment]
            self._last_on_state = None
            self._last_atw_subtypes.clear()
            self.capabilities.reset_probes()
        cached: HeatPumpState | None = self.data
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
            if cached is None:
                raise
            status = cached.status
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
            cached_sensors = cached.sensors if cached is not None else None
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

        adapter = get_adapter(sn8)

        raw_frames: dict[int, bytes] = {}
        if status.raw_body:
            raw_frames[0x01] = bytes(status.raw_body)
        if self._sensors_degraded:
            cached_0x02 = cached.raw_frames.get(0x02) if cached is not None else None
            if cached_0x02:
                raw_frames[0x02] = bytes(cached_0x02)
        elif sensors.raw_body and sensors.raw_body[:1] == b"\x02":
            raw_frames[0x02] = bytes(sensors.raw_body)

        # The dialect decides which optional subtypes to probe (e.g. ATW 0x10
        # then 0x0A on a 0x02 schedule template, #120; 0x05/0x07 on a C3 basic
        # status, #139/#188). Units that reject a subtype on the initial probe
        # (e.g. code=1214 on Rotenso) latch unsupported; transient errors after
        # a subtype is confirmed retry on the next poll and reuse its last frame.
        for spec in adapter.optional_probes(status, raw_frames):
            if spec.requires is not None and spec.requires not in raw_frames:
                continue
            await self._async_probe_optional_subtype(spec, raw_frames)

        self._update_offline_repair()
        self._observe_kjrh120l_zone1(status)

        # A poll's freshly queried 0x10 counters supersede any pushed 0x04
        # energy state (raw_frames is rebuilt without 0x04 too).
        self._atw_energy_notify = None
        # Decode + overlay every successfully probed subtype in one pure step,
        # shared with the MQTT push paths so push and poll stay in parity (#225).
        return build_snapshot(adapter, status, sensors, raw_frames)

    def _probe_target(
        self, subtype: int
    ) -> tuple[ProbeState, Callable[[str], bytes]]:
        """Return the session probe state and client fetcher for ``subtype``."""
        targets: dict[int, tuple[ProbeState, Callable[[str], bytes]]] = {
            ATW_UNIT_PARAMS_SUBTYPE: (
                self.capabilities.atw_unit_params,
                self.client.query_atw_unit_params,
            ),
            ATW_HMI_PARAMS_SUBTYPE: (
                self.capabilities.atw_hmi_params,
                self.client.query_atw_hmi_params,
            ),
            ATW_SILENT_SUBTYPE: (
                self.capabilities.atw_silent,
                self.client.query_atw_silent,
            ),
            ATW_ECO_SUBTYPE: (
                self.capabilities.atw_eco,
                self.client.query_atw_eco,
            ),
        }
        return targets[subtype]

    async def _async_probe_optional_subtype(
        self, spec: OptionalProbe, raw_frames: dict[int, bytes]
    ) -> bytes | None:
        """Probe one optional C3 subtype, disabling for the session on ApiError."""
        subtype, label, min_len = spec.subtype, spec.label, spec.min_len
        probe, fetcher = self._probe_target(subtype)
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
                self._last_atw_subtypes[subtype] = raw
                raw_frames[subtype] = raw
                return raw
            _LOGGER.debug(
                "ATW subtype 0x%02X %s returned short/invalid reply (%d bytes)",
                subtype,
                label,
                len(body) if isinstance(body, (bytes, bytearray)) else 0,
            )
        if probe.supported is True and subtype in self._last_atw_subtypes:
            cached = self._last_atw_subtypes[subtype]
            raw_frames[subtype] = cached
            return cached
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
        await self.session.async_login()

        # Auto-discover appliance if not set
        if not self.appliance_code:
            appliances = await self.session.async_discover_appliances()
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
            appliances = await self.session.async_discover_appliances()
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
        await self.session.async_load_or_login()

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
        self._require_writable()

        try:
            async with self._write_lock:
                result = await self.session.async_run_authenticated(
                    action, reason="set"
                )
        except (
            ApiError,
            AuthError,
            ValueError,
            requests.RequestException,
        ) as err:
            raise HomeAssistantError(f"Heat pump command failed: {err}") from err
        await self.async_request_refresh()
        return result

    def _require_writable(self) -> None:
        """Reject writes in read-only phone-app coexistence mode."""
        try:
            self.session.policy.require_writable()
        except SessionPolicyError as err:
            raise HomeAssistantError(str(err)) from err

    def _write_context(self) -> WriteContext:
        """Snapshot the state a dialect needs to plan a write (#113)."""
        return WriteContext(
            status=self.data.status if self.data is not None else None,
            last_on_state=self._last_on_state,
            c3_basic_control=self._c3_basic_control,
            kjrh120l_zone1_latched=self.kjrh120l_zone1_capable,
        )

    async def async_send_command(self, command: HeatPumpCommand) -> None:
        """Plan ``command`` in the selected device dialect and execute it (#113).

        Capability checks, ranges, refusal rules and frame encoding live in the
        dialect's pure ``plan_write``; this method only adds the transaction:
        phone-app guard, write lock, fresh read → SET, re-auth and refresh.
        """
        self._require_writable()
        try:
            plan = get_adapter(self.sn8).plan_write(command, self._write_context())
        except WriteRefused as err:
            raise HomeAssistantError(str(err)) from err
        except (ApiError, ValueError) as err:
            raise HomeAssistantError(f"Heat pump command failed: {err}") from err
        await self.async_execute_plan(plan)

    async def async_execute_plan(self, plan: WritePlan) -> dict[str, Any]:
        """Run a dialect ``WritePlan`` (fresh read → encode → SET) in one transaction."""
        appliance_code = self.appliance_code

        def _send(command_hex: str) -> str:
            return self.client.send_hex_command(appliance_code, command_hex)

        def _run() -> dict[str, Any]:
            result = run_write_plan(plan, _send)
            _LOGGER.debug("write %s -> %s", plan.description, result["sent"])
            return result

        result: dict[str, Any] = await self.async_execute_command(_run)
        return result

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
            on_energy=self._push_energy_threadsafe,
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
        return await self.session.async_mint_mqtt_certificate()

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

    def _push_energy_threadsafe(self, energy_body: bytes) -> None:
        """Marshal a subtype-0x04 energy push from paho's thread onto the event loop."""
        self.hass.loop.call_soon_threadsafe(self._apply_push_energy, energy_body)

    def _push_connected_threadsafe(self, connected: bool) -> None:
        self.hass.loop.call_soon_threadsafe(self._apply_push_connected, connected)

    def _apply_push_energy(self, energy_body: bytes) -> None:
        """Handle an unsolicited MQTT 0x04 (C3EnergyBody) push frame (#129).

        Always records push activity and re-arms the status heartbeat where the
        dialect has one (ATW units do not; their availability follows the MQTT
        link). On ATW units with an existing snapshot, also overlays outdoor
        temp and energy.
        """
        self._last_push_status_at = time.monotonic()
        self._schedule_push_status_watchdog()
        if self.data is None:
            return
        adapter = get_adapter(self.sn8)
        if not adapter.accepts_energy_notify:
            return
        raw_frames = dict(self.data.raw_frames)
        raw_frames[0x04] = bytes(energy_body)
        # Keep the last valid value of each 0x04 field separately: the
        # snapshot is re-derived, so a sentinel field must not clobber it.
        self._atw_energy_notify = merge_atw_energy_notify(
            self._atw_energy_notify, bytes(energy_body)
        )
        state = build_snapshot(
            adapter,
            self.data.status,
            self.data.sensors,
            raw_frames,
            retained_subtypes=True,
            energy_notify=self._atw_energy_notify,
        )
        self.async_set_updated_data(state)

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
            # with no sensors (entities read coordinator.data.sensors); the
            # imminent first poll delivers a complete snapshot.
            return
        adapter = get_adapter(self.sn8)
        status = adapter.decode_status(status)
        self._observe_kjrh120l_zone1(status)
        raw_frames = dict(self.data.raw_frames)
        if status.raw_body:
            raw_frames[0x01] = bytes(status.raw_body)
        # Re-apply every retained subtype overlay (0x10/0x0A/0x05/0x07/0x04)
        # exactly as the poll does; the push carries only the 0x01 frame (#225).
        # Optional subtype frames date from the last poll: re-apply counters,
        # run hours and ECO, reconcile Silent with this 0x01 frame.
        state = build_snapshot(
            adapter,
            status,
            self.data.sensors,
            raw_frames,
            retained_subtypes=True,
            energy_notify=self._atw_energy_notify,
        )
        self.async_set_updated_data(state)

    def _cancel_push_status_watchdog(self) -> None:
        if self._push_status_watchdog is not None:
            self._push_status_watchdog()
            self._push_status_watchdog = None

    @callback
    def _on_push_status_watchdog(self, _now: Any) -> None:
        """Run the MQTT status freshness check on the Home Assistant event loop."""
        self._check_push_status_freshness()

    def _push_status_heartbeat_expected(self) -> bool:
        """Return whether silence on a live MQTT link means status is stale.

        Unknown sn8 resolves to STANDARD and keeps the heartbeat (safe default).
        """
        return get_adapter(self.sn8).periodic_status_push

    def _schedule_push_status_watchdog(self) -> None:
        self._cancel_push_status_watchdog()
        if not self._push_status_heartbeat_expected():
            return
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
        if not self._push_status_heartbeat_expected():
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
        if not self.session.policy.allows_background_polling:
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
