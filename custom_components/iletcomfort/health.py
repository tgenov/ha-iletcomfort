"""Framework-independent acquisition and health state machine (#112).

Models polling and MQTT push acquisition events, per-query cache fallback,
sustained-warning escalation, recovery breadcrumbs, offline Repair transitions,
profile-change cache invalidation, MQTT transport liveness vs device-status
freshness, and operation-mode policy (HA primary vs phone-app coexistence).

Zero Home Assistant dependencies: produces immutable :class:`HealthSnapshot`
projections and :class:`AcquisitionTransition` side-effect directives that the
Home Assistant coordinator publishes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal

from .api import QUERY_TO_SET_MODE, ITSSensors, ITSStatus
from .const import DEFAULT_OPERATION_MODE, MQTT_STATUS_STALE_AFTER
from .protocols import (
    HeatPumpState,
    ProtocolAdapter,
    build_snapshot,
    get_adapter,
    merge_atw_energy_notify,
)
from .session import SessionPolicy

# Number of consecutive polls in which both status and sensors fall back to
# cache before surfacing a "device appears offline" Repair card (~5 min at 60s).
OFFLINE_REPAIR_THRESHOLD = 5

# Number of consecutive polls a single query (status or sensors) must fall back
# to cache before escalating from DEBUG to a single WARNING (issue #44).
SUSTAINED_FAILURE_THRESHOLD = 5

QueryKind = Literal["status", "sensors"]
RepairAction = Literal["create", "delete"]
WatchdogAction = Literal["schedule", "cancel"]


@dataclass(frozen=True)
class HealthSnapshot:
    """Public, immutable projection of acquisition and link health (#112).

    Consumed by diagnostics and tests instead of private coordinator fields.
    ``mqtt_transport_connected`` (broker link liveness) and ``mqtt_status_fresh``
    (end-to-end device status heartbeat) are intentionally distinct signals.
    """

    operation_mode: str = DEFAULT_OPERATION_MODE
    status_degraded: bool = False
    sensors_degraded: bool = False
    status_fail_streak: int = 0
    sensors_fail_streak: int = 0
    consecutive_both_degraded: int = 0
    repair_issued: bool = False
    mqtt_transport_connected: bool | None = None
    mqtt_status_fresh: bool | None = None
    last_push_status_at: float | None = None


@dataclass(frozen=True)
class AcquisitionTransition:
    """Immutable transition returned by :class:`AcquisitionHealthStateMachine`.

    Describes the resulting :class:`HealthSnapshot` and any framework side
    effects the coordinator adapter should publish.
    """

    health: HealthSnapshot
    state: HeatPumpState | None = None
    publish_state: bool = False
    update_error: str | None = None
    disable_polling: bool = False
    repair_action: RepairAction | None = None
    watchdog_action: WatchdogAction | None = None
    log_events: tuple[tuple[int, str], ...] = ()
    reset_capabilities: bool = False


_UNSET = object()


class AcquisitionHealthStateMachine:
    """Pure state machine for snapshot acquisition and device/link health (#112)."""

    def __init__(
        self,
        *,
        operation_mode: str = DEFAULT_OPERATION_MODE,
        initial_sn8: str | None = None,
    ) -> None:
        self._policy = SessionPolicy(operation_mode=operation_mode)
        self._health = HealthSnapshot(operation_mode=operation_mode)
        self._last_adapter: ProtocolAdapter = get_adapter(initial_sn8)
        self._last_on_state: tuple[int, int] | None = None
        self._atw_energy_notify: bytes | None = None

    @property
    def policy(self) -> SessionPolicy:
        return self._policy

    def set_operation_mode(self, operation_mode: str) -> None:
        self._policy = SessionPolicy(operation_mode=operation_mode)
        self._health = replace(self._health, operation_mode=operation_mode)

    @property
    def health(self) -> HealthSnapshot:
        """Current immutable health projection."""
        return self._health

    @property
    def last_on_state(self) -> tuple[int, int] | None:
        return self._last_on_state

    @last_on_state.setter
    def last_on_state(self, value: tuple[int, int] | None) -> None:
        self._last_on_state = value

    @property
    def atw_energy_notify(self) -> bytes | None:
        return self._atw_energy_notify

    @atw_energy_notify.setter
    def atw_energy_notify(self, value: bytes | None) -> None:
        self._atw_energy_notify = value

    def on_profile_check(
        self, current_sn8: str | None, *, previous_sn8: str | None | object = _UNSET
    ) -> AcquisitionTransition:
        """Invalidate cached state/probes when the resolved dialect changes."""
        previous_adapter = (
            self._last_adapter
            if previous_sn8 is _UNSET
            else get_adapter(previous_sn8 if isinstance(previous_sn8, str) else None)
        )
        adapter = get_adapter(current_sn8)
        self._last_adapter = adapter
        if adapter is previous_adapter:
            return AcquisitionTransition(health=self._health)
        self._last_on_state = None
        self._atw_energy_notify = None
        return AcquisitionTransition(health=self._health, reset_capabilities=True)

    def record_query_result(
        self,
        query: QueryKind,
        *,
        succeeded: bool,
        error: Exception | None = None,
    ) -> AcquisitionTransition:
        """Record a single status or sensors poll query outcome."""
        label = "Status" if query == "status" else "Sensors"
        streak = (
            self._health.status_fail_streak
            if query == "status"
            else self._health.sensors_fail_streak
        )
        logs: list[tuple[int, str]] = []

        if succeeded:
            if streak >= SUSTAINED_FAILURE_THRESHOLD:
                logs.append(
                    (
                        logging.INFO,
                        f"{label} query recovered after {streak} sustained cache-fallback polls",
                    )
                )
            new_streak = 0
            degraded = False
        else:
            new_streak = streak + 1
            degraded = True
            level = (
                logging.WARNING
                if new_streak == SUSTAINED_FAILURE_THRESHOLD
                else logging.DEBUG
            )
            logs.append((level, f"{label} query failed, using cache: {error}"))

        if query == "status":
            self._health = replace(
                self._health,
                status_degraded=degraded,
                status_fail_streak=new_streak,
            )
        else:
            self._health = replace(
                self._health,
                sensors_degraded=degraded,
                sensors_fail_streak=new_streak,
            )
        return AcquisitionTransition(health=self._health, log_events=tuple(logs))

    def on_poll_complete(
        self,
        *,
        sn8: str | None,
        status: ITSStatus,
        sensors: ITSSensors,
        raw_frames: dict[int, bytes],
    ) -> AcquisitionTransition:
        """Complete a poll cycle: update Repair state and build the snapshot."""
        # Track last on-state for power restore.
        if status.mode != 0:
            set_mode = QUERY_TO_SET_MODE.get(status.mode)
            if set_mode is not None:
                temp = (
                    int(status.t5s_def)
                    if status.t5s_def is not None
                    else status.set_temperature
                )
                self._last_on_state = (set_mode, temp)

        repair_action = self._advance_offline_repair()

        # A poll's freshly queried 0x10 counters supersede any pushed 0x04
        # energy state (raw_frames is rebuilt without 0x04 too).
        self._atw_energy_notify = None
        adapter = get_adapter(sn8)
        self._last_adapter = adapter
        state = build_snapshot(adapter, status, sensors, raw_frames)
        return AcquisitionTransition(
            health=self._health,
            state=state,
            publish_state=True,
            repair_action=repair_action,
        )

    def _advance_offline_repair(self) -> RepairAction | None:
        both_degraded = self._health.status_degraded and self._health.sensors_degraded
        if both_degraded:
            consecutive = self._health.consecutive_both_degraded + 1
            if consecutive >= OFFLINE_REPAIR_THRESHOLD and not self._health.repair_issued:
                self._health = replace(
                    self._health,
                    consecutive_both_degraded=consecutive,
                    repair_issued=True,
                )
                return "create"
            self._health = replace(
                self._health,
                consecutive_both_degraded=consecutive,
            )
            return None

        if self._health.repair_issued:
            self._health = replace(
                self._health,
                consecutive_both_degraded=0,
                repair_issued=False,
            )
            return "delete"

        if self._health.consecutive_both_degraded != 0:
            self._health = replace(self._health, consecutive_both_degraded=0)
        return None

    def _watchdog_action(self, adapter: ProtocolAdapter) -> WatchdogAction:
        """Schedule freshness watchdog only in phone-app mode for heartbeat dialects."""
        if not self._policy.allows_background_polling and adapter.periodic_status_push:
            return "schedule"
        return "cancel"

    def on_push_start_failed(self) -> AcquisitionTransition:
        """Record MQTT push startup failure without re-enabling account polling."""
        phone_app = not self._policy.allows_background_polling
        self._health = replace(
            self._health,
            mqtt_transport_connected=False,
            mqtt_status_fresh=False,
            last_push_status_at=None,
        )
        return AcquisitionTransition(
            health=self._health,
            disable_polling=phone_app,
            watchdog_action="cancel",
            update_error=(
                "MQTT push failed to start in phone app mode" if phone_app else None
            ),
        )

    def on_push_stopped(self) -> AcquisitionTransition:
        """Reset MQTT link and freshness tracking on shutdown/unload."""
        self._health = replace(
            self._health,
            mqtt_transport_connected=None,
            mqtt_status_fresh=None,
            last_push_status_at=None,
        )
        return AcquisitionTransition(
            health=self._health,
            watchdog_action="cancel",
        )

    def on_push_connected(
        self,
        connected: bool,
        *,
        sn8: str | None,
        cached_state: HeatPumpState | None,
        last_update_success: bool,
    ) -> AcquisitionTransition:
        """Handle MQTT broker connect/disconnect transitions.

        In phone-app coexistence mode, polling is never re-enabled on
        disconnect; entities are marked unavailable instead.
        """
        if self._policy.allows_background_polling:
            self._health = replace(self._health, mqtt_transport_connected=connected)
            return AcquisitionTransition(health=self._health)

        adapter = get_adapter(sn8)
        if connected:
            self._health = replace(self._health, mqtt_transport_connected=True)
            should_republish = not last_update_success and cached_state is not None
            return AcquisitionTransition(
                health=self._health,
                state=cached_state if should_republish else None,
                publish_state=should_republish,
                disable_polling=True,
                watchdog_action=self._watchdog_action(adapter),
            )

        self._health = replace(
            self._health,
            mqtt_transport_connected=False,
            mqtt_status_fresh=False,
            last_push_status_at=None,
        )
        return AcquisitionTransition(
            health=self._health,
            disable_polling=True,
            watchdog_action="cancel",
            update_error="MQTT push disconnected in phone app mode",
        )

    def on_push_status(
        self,
        status: ITSStatus,
        *,
        sn8: str | None,
        now: float,
        cached_state: HeatPumpState | None,
    ) -> tuple[AcquisitionTransition, ITSStatus]:
        """Apply a pushed 0x01 status frame while retaining cached sensors/subtypes."""
        adapter = get_adapter(sn8)
        self._health = replace(
            self._health,
            last_push_status_at=now,
            mqtt_status_fresh=True,
        )
        watchdog: WatchdogAction = self._watchdog_action(adapter)
        if cached_state is None:
            return (
                AcquisitionTransition(health=self._health, watchdog_action=watchdog),
                status,
            )

        decoded_status = adapter.decode_status(status)
        raw_frames = dict(cached_state.raw_frames)
        if decoded_status.raw_body:
            raw_frames[0x01] = bytes(decoded_status.raw_body)
        state = build_snapshot(
            adapter,
            decoded_status,
            cached_state.sensors,
            raw_frames,
            retained_subtypes=True,
            energy_notify=self._atw_energy_notify,
        )
        return (
            AcquisitionTransition(
                health=self._health,
                state=state,
                publish_state=True,
                watchdog_action=watchdog,
            ),
            decoded_status,
        )

    def on_push_energy(
        self,
        energy_body: bytes,
        *,
        sn8: str | None,
        now: float,
        cached_state: HeatPumpState | None,
    ) -> AcquisitionTransition:
        """Apply an unsolicited MQTT 0x04 (C3EnergyBody) push frame (#129)."""
        adapter = get_adapter(sn8)
        self._health = replace(
            self._health,
            last_push_status_at=now,
            mqtt_status_fresh=True,
        )
        watchdog: WatchdogAction = self._watchdog_action(adapter)
        if cached_state is None or not adapter.accepts_energy_notify:
            return AcquisitionTransition(health=self._health, watchdog_action=watchdog)

        raw_frames = dict(cached_state.raw_frames)
        raw_frames[0x04] = bytes(energy_body)
        self._atw_energy_notify = merge_atw_energy_notify(
            self._atw_energy_notify, bytes(energy_body)
        )
        state = build_snapshot(
            adapter,
            cached_state.status,
            cached_state.sensors,
            raw_frames,
            retained_subtypes=True,
            energy_notify=self._atw_energy_notify,
        )
        return AcquisitionTransition(
            health=self._health,
            state=state,
            publish_state=True,
            watchdog_action=watchdog,
        )

    def on_freshness_check(
        self,
        *,
        sn8: str | None,
        now: float,
        push_enabled: bool,
        stale_after: float = MQTT_STATUS_STALE_AFTER,
    ) -> AcquisitionTransition:
        """Evaluate device-status freshness separately from broker transport liveness."""
        if not push_enabled or self._policy.allows_background_polling:
            return AcquisitionTransition(health=self._health)
        adapter = get_adapter(sn8)
        if not adapter.periodic_status_push:
            return AcquisitionTransition(health=self._health)
        last_at = self._health.last_push_status_at
        if last_at is None or (now - last_at >= stale_after):
            self._health = replace(self._health, mqtt_status_fresh=False)
            return AcquisitionTransition(
                health=self._health,
                update_error="MQTT device status heartbeat expired",
            )
        self._health = replace(self._health, mqtt_status_fresh=True)
        return AcquisitionTransition(
            health=self._health,
            watchdog_action="schedule",
        )


__all__ = [
    "AcquisitionHealthStateMachine",
    "AcquisitionTransition",
    "HealthSnapshot",
    "OFFLINE_REPAIR_THRESHOLD",
    "QueryKind",
    "RepairAction",
    "SUSTAINED_FAILURE_THRESHOLD",
    "WatchdogAction",
]
