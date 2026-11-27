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
from .health import (
    OFFLINE_REPAIR_THRESHOLD as OFFLINE_REPAIR_THRESHOLD,
    SUSTAINED_FAILURE_THRESHOLD as SUSTAINED_FAILURE_THRESHOLD,
    AcquisitionHealthStateMachine,
    AcquisitionTransition,
    HealthSnapshot,
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
    get_adapter,
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

OFFLINE_REPAIR_ID = "device_offline_{entry_id}"


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
        # Framework-independent acquisition & health state machine (#112).
        self.health_machine = AcquisitionHealthStateMachine(
            operation_mode=operation_mode,
        )
        # Serialises writes for this appliance: a C3 basic SET is built from a
        # fresh status read, so overlapping read/modify/write transactions
        # would otherwise undo each other's changes.
        self._write_lock = asyncio.Lock()
        self._c3_basic_control: bool = bool(
            entry.options.get(CONF_C3_BASIC_CONTROL, DEFAULT_C3_BASIC_CONTROL)
        )
        self._push_status_watchdog: Callable[[], None] | None = None
        # Centralized runtime probe / capability state (#171).
        self.capabilities = DeviceCapabilities()
        # Last valid optional ATW subtype bodies (0x10, 0x0A, 0x05, 0x07),
        # retained as transient-error fallback so a single cloud blip never
        # drops telemetry to unknown or resets TOTAL_INCREASING energy (#118).
        self._last_atw_subtypes: dict[int, bytes] = {}

    @property
    def health_snapshot(self) -> HealthSnapshot:
        """Public immutable health projection consumed by diagnostics (#112)."""
        return self.health_machine.health

    @property
    def atw_energy_notify(self) -> bytes | None:
        """Public accessor for the merged sentinel-filtered ATW 0x04 energy state."""
        return self.health_machine.atw_energy_notify

    @property
    def _atw_energy_notify(self) -> bytes | None:
        return self.health_machine.atw_energy_notify

    @_atw_energy_notify.setter
    def _atw_energy_notify(self, value: bytes | None) -> None:
        self.health_machine.atw_energy_notify = value

    @property
    def _last_on_state(self) -> tuple[int, int] | None:
        return self.health_machine.last_on_state

    @_last_on_state.setter
    def _last_on_state(self, value: tuple[int, int] | None) -> None:
        self.health_machine.last_on_state = value

    @property
    def _last_push_status_at(self) -> float | None:
        return self.health_machine.health.last_push_status_at

    @property
    def _status_degraded(self) -> bool:
        return self.health_machine.health.status_degraded

    @property
    def _sensors_degraded(self) -> bool:
        return self.health_machine.health.sensors_degraded

    @property
    def _repair_issued(self) -> bool:
        return self.health_machine.health.repair_issued

    @property
    def _operation_mode(self) -> str:
        return self.session.policy.operation_mode

    @_operation_mode.setter
    def _operation_mode(self, value: str) -> None:
        self.session.set_operation_mode(value)
        self.health_machine.set_operation_mode(value)

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

    def _apply_transition(
        self,
        transition: AcquisitionTransition,
        *,
        publish: bool = True,
    ) -> None:
        """Execute HA side effects described by a state-machine transition (#112)."""
        for level, message in transition.log_events:
            _LOGGER.log(level, "%s", message)

        if transition.reset_capabilities:
            self.data = None  # type: ignore[assignment]
            self._last_atw_subtypes.clear()
            self.capabilities.reset_probes()

        if transition.disable_polling:
            self.update_interval = None

        if transition.repair_action == "create":
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
        elif transition.repair_action == "delete":
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                OFFLINE_REPAIR_ID.format(entry_id=self.entry.entry_id),
            )

        if transition.watchdog_action == "schedule":
            self._schedule_push_status_watchdog()
        elif transition.watchdog_action == "cancel":
            self._cancel_push_status_watchdog()

        if publish:
            if transition.update_error is not None:
                self.async_set_update_error(UpdateFailed(transition.update_error))
            elif transition.publish_state and transition.state is not None:
                self.async_set_updated_data(transition.state)

    async def _poll(self) -> HeatPumpState:
        """Run the actual polling calls in the executor."""
        previous_sn8 = self.sn8
        await self._ensure_appliance_meta()
        self._apply_transition(
            self.health_machine.on_profile_check(
                self.sn8, previous_sn8=previous_sn8
            ),
            publish=False,
        )
        cached: HeatPumpState | None = self.data
        sn8 = self.sn8

        try:
            status: ITSStatus = await self.hass.async_add_executor_job(
                self.client.query_status, self.appliance_code, sn8,
            )
            self._apply_transition(
                self.health_machine.record_query_result("status", succeeded=True),
                publish=False,
            )
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            if cached is None:
                raise
            status = cached.status
            self._apply_transition(
                self.health_machine.record_query_result(
                    "status", succeeded=False, error=err
                ),
                publish=False,
            )

        await asyncio.sleep(2)

        try:
            sensors: ITSSensors = await self.hass.async_add_executor_job(
                self.client.query_sensors, self.appliance_code, sn8,
            )
            self._apply_transition(
                self.health_machine.record_query_result("sensors", succeeded=True),
                publish=False,
            )
        except AuthError:
            raise  # bubble up for re-auth
        except Exception as err:
            cached_sensors = cached.sensors if cached is not None else None
            if cached_sensors is None:
                raise
            sensors = cached_sensors
            self._apply_transition(
                self.health_machine.record_query_result(
                    "sensors", succeeded=False, error=err
                ),
                publish=False,
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

        adapter = get_adapter(sn8)

        raw_frames: dict[int, bytes] = {}
        if status.raw_body:
            raw_frames[0x01] = bytes(status.raw_body)
        if self.health_snapshot.sensors_degraded:
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

        self._observe_kjrh120l_zone1(status)

        transition = self.health_machine.on_poll_complete(
            sn8=sn8,
            status=status,
            sensors=sensors,
            raw_frames=raw_frames,
        )
        self._apply_transition(transition, publish=False)
        assert transition.state is not None
        return transition.state

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
            self._apply_transition(self.health_machine.on_push_start_failed())

    async def _async_get_push_certificate(self) -> AppCert:
        """Mint a push certificate, re-authenticating only when required."""
        return await self.session.async_mint_mqtt_certificate()

    async def async_stop_push(self) -> None:
        """Stop the MQTT push listener, if running."""
        self._apply_transition(self.health_machine.on_push_stopped(), publish=False)
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
        """Handle an unsolicited MQTT 0x04 (C3EnergyBody) push frame (#129)."""
        transition = self.health_machine.on_push_energy(
            energy_body,
            sn8=self.sn8,
            now=time.monotonic(),
            cached_state=self.data,
        )
        self._apply_transition(transition)

    def _apply_push_status(self, status: ITSStatus) -> None:
        """Publish a pushed status to entities, reusing the last polled sensors."""
        transition, decoded_status = self.health_machine.on_push_status(
            status,
            sn8=self.sn8,
            now=time.monotonic(),
            cached_state=self.data,
        )
        if transition.publish_state:
            self._observe_kjrh120l_zone1(decoded_status)
        self._apply_transition(transition)

    def _cancel_push_status_watchdog(self) -> None:
        if self._push_status_watchdog is not None:
            self._push_status_watchdog()
            self._push_status_watchdog = None

    @callback
    def _on_push_status_watchdog(self, _now: Any) -> None:
        """Run the MQTT status freshness check on the Home Assistant event loop."""
        self._check_push_status_freshness()

    def _push_status_heartbeat_expected(self) -> bool:
        """Return whether silence on a live MQTT link means status is stale."""
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
        transition = self.health_machine.on_freshness_check(
            sn8=self.sn8,
            now=time.monotonic(),
            push_enabled=self._push_enabled,
        )
        self._apply_transition(transition)

    def _apply_push_connected(self, connected: bool) -> None:
        """Expose transport and device-status freshness without account polling."""
        transition = self.health_machine.on_push_connected(
            connected,
            sn8=self.sn8,
            cached_state=self.data,
            last_update_success=self.last_update_success,
        )
        self._apply_transition(transition)
