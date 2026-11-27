"""Framework-independent event-sequence tests for acquisition & health (#112)."""

from __future__ import annotations

import logging

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.const import (
    MQTT_STATUS_STALE_AFTER,
    OPERATION_MODE_HA_PRIMARY,
    OPERATION_MODE_PHONE_APP,
)
from custom_components.iletcomfort.health import (
    OFFLINE_REPAIR_THRESHOLD,
    SUSTAINED_FAILURE_THRESHOLD,
    AcquisitionHealthStateMachine,
)
from custom_components.iletcomfort.protocols import build_snapshot, get_adapter


def _status(mode: int = 1, temp: int = 45, raw: bytes = b"\x01\x01\x01\x46\x55\x2d") -> ITSStatus:
    return ITSStatus(mode=mode, set_temperature=temp, raw_body=raw)


def _sensors(t4: float = 12.0, raw: bytes = b"\x02" + bytes(49)) -> ITSSensors:
    return ITSSensors(t4_temp=t4, raw_body=raw)


def test_per_query_cache_fallback_warning_escalation_and_recovery() -> None:
    sm = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_HA_PRIMARY)
    err = RuntimeError("cloud 502")

    # Below threshold: DEBUG only.
    for i in range(1, SUSTAINED_FAILURE_THRESHOLD):
        t = sm.record_query_result("status", succeeded=False, error=err)
        assert t.health.status_degraded is True
        assert t.health.status_fail_streak == i
        assert t.log_events[0][0] == logging.DEBUG

    # Exactly at threshold: single WARNING.
    t_warn = sm.record_query_result("status", succeeded=False, error=err)
    assert t_warn.health.status_fail_streak == SUSTAINED_FAILURE_THRESHOLD
    assert t_warn.log_events[0][0] == logging.WARNING

    # Beyond threshold: back to DEBUG (no log flood).
    t_beyond = sm.record_query_result("status", succeeded=False, error=err)
    assert t_beyond.health.status_fail_streak == SUSTAINED_FAILURE_THRESHOLD + 1
    assert t_beyond.log_events[0][0] == logging.DEBUG

    # Recovery after sustained warning: single INFO breadcrumb.
    t_rec = sm.record_query_result("status", succeeded=True)
    assert t_rec.health.status_degraded is False
    assert t_rec.health.status_fail_streak == 0
    assert t_rec.log_events[0][0] == logging.INFO

    # Transient blip below threshold recovers silently.
    sm.record_query_result("sensors", succeeded=False, error=err)
    t_quiet = sm.record_query_result("sensors", succeeded=True)
    assert t_quiet.health.sensors_degraded is False
    assert t_quiet.log_events == ()


def test_offline_repair_create_and_clear_transitions() -> None:
    sm = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_HA_PRIMARY)
    err = RuntimeError("offline")
    status, sensors = _status(), _sensors()

    for poll in range(1, OFFLINE_REPAIR_THRESHOLD):
        sm.record_query_result("status", succeeded=False, error=err)
        sm.record_query_result("sensors", succeeded=False, error=err)
        t = sm.on_poll_complete(sn8=None, status=status, sensors=sensors, raw_frames={})
        assert t.repair_action is None
        assert t.health.repair_issued is False
        assert t.health.consecutive_both_degraded == poll

    # Reaching threshold creates Repair card once.
    sm.record_query_result("status", succeeded=False, error=err)
    sm.record_query_result("sensors", succeeded=False, error=err)
    t_create = sm.on_poll_complete(sn8=None, status=status, sensors=sensors, raw_frames={})
    assert t_create.repair_action == "create"
    assert t_create.health.repair_issued is True

    # Subsequent degraded poll does not re-create.
    t_stay = sm.on_poll_complete(sn8=None, status=status, sensors=sensors, raw_frames={})
    assert t_stay.repair_action is None
    assert t_stay.health.repair_issued is True

    # First poll where either query succeeds clears Repair card.
    sm.record_query_result("status", succeeded=True)
    t_clear = sm.on_poll_complete(sn8=None, status=status, sensors=sensors, raw_frames={})
    assert t_clear.repair_action == "delete"
    assert t_clear.health.repair_issued is False
    assert t_clear.health.consecutive_both_degraded == 0


def test_profile_change_invalidates_cached_state_and_resets_capabilities() -> None:
    sm = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_HA_PRIMARY)
    sm.on_poll_complete(sn8=None, status=_status(mode=1, temp=48), sensors=_sensors(), raw_frames={})
    assert sm.last_on_state == (1, 48)

    # Same profile -> no reset.
    t_same = sm.on_profile_check(None)
    assert t_same.reset_capabilities is False
    assert sm.last_on_state == (1, 48)

    # Discovered ATW profile -> invalidates last_on_state and requests capability reset.
    t_changed = sm.on_profile_check("171H120F")
    assert t_changed.reset_capabilities is True
    assert sm.last_on_state is None
    assert sm.atw_energy_notify is None


def test_phone_app_coexistence_transport_liveness_and_freshness_are_distinct() -> None:
    sm = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_PHONE_APP)
    assert sm.policy.allows_background_polling is False
    assert sm.policy.allows_writes is False

    # Initial poll snapshot from setup.
    initial = build_snapshot(get_adapter(None), _status(temp=40), _sensors(t4=14.0), {})

    # Broker connects: transport is live, watchdog scheduled, polling stays disabled.
    t_conn = sm.on_push_connected(
        True, sn8=None, cached_state=initial, last_update_success=True
    )
    assert t_conn.disable_polling is True
    assert t_conn.watchdog_action == "schedule"
    assert t_conn.health.mqtt_transport_connected is True

    # Status push arrives: retains cached sensors (t4=14.0) while updating status.
    t_push, _ = sm.on_push_status(
        _status(temp=52), sn8=None, now=100.0, cached_state=initial
    )
    assert t_push.publish_state is True
    assert t_push.state is not None
    assert t_push.state.sensors.t4_temp == 14.0
    assert t_push.state.status.set_temperature == 52
    assert t_push.health.mqtt_transport_connected is True
    assert t_push.health.mqtt_status_fresh is True

    # Freshness check before expiry keeps transport & freshness healthy.
    t_ok = sm.on_freshness_check(
        sn8=None, now=100.0 + MQTT_STATUS_STALE_AFTER - 10, push_enabled=True
    )
    assert t_ok.update_error is None
    assert t_ok.watchdog_action == "schedule"
    assert t_ok.health.mqtt_transport_connected is True
    assert t_ok.health.mqtt_status_fresh is True

    # Freshness expires while MQTT transport is STILL connected (distinct signals!).
    t_stale = sm.on_freshness_check(
        sn8=None, now=100.0 + MQTT_STATUS_STALE_AFTER, push_enabled=True
    )
    assert t_stale.update_error == "MQTT device status heartbeat expired"
    assert t_stale.health.mqtt_transport_connected is True
    assert t_stale.health.mqtt_status_fresh is False

    # Broker disconnect: transport False, polling stays disabled, never falls back.
    t_disc = sm.on_push_connected(
        False, sn8=None, cached_state=t_push.state, last_update_success=False
    )
    assert t_disc.disable_polling is True
    assert t_disc.watchdog_action == "cancel"
    assert t_disc.update_error == "MQTT push disconnected in phone app mode"
    assert t_disc.health.mqtt_transport_connected is False
    assert t_disc.health.mqtt_status_fresh is False
    assert t_disc.health.last_push_status_at is None

    # Reconnect clears unavailable state by republishing cached snapshot.
    t_reconn = sm.on_push_connected(
        True, sn8=None, cached_state=t_push.state, last_update_success=False
    )
    assert t_reconn.publish_state is True
    assert t_reconn.state is t_push.state
    assert t_reconn.disable_polling is True


def test_push_before_initial_snapshot_is_ignored_without_publishing() -> None:
    sm = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_PHONE_APP)
    t_push, _ = sm.on_push_status(_status(), sn8=None, now=50.0, cached_state=None)
    assert t_push.publish_state is False
    assert t_push.state is None
    assert t_push.health.last_push_status_at == 50.0


def test_atw_skips_periodic_status_heartbeat_watchdog() -> None:
    sm = AcquisitionHealthStateMachine(
        operation_mode=OPERATION_MODE_PHONE_APP, initial_sn8="171H120F"
    )
    t_conn = sm.on_push_connected(
        True, sn8="171H120F", cached_state=None, last_update_success=True
    )
    assert t_conn.watchdog_action == "cancel"

    t_check = sm.on_freshness_check(
        sn8="171H120F", now=99999.0, push_enabled=True
    )
    assert t_check.update_error is None
    assert t_check.watchdog_action is None


def test_push_start_failed_stopped_and_ha_primary_mode_gating() -> None:
    # Phone-app coexistence: startup failure disables polling and sets error.
    sm_phone = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_PHONE_APP)
    t_fail = sm_phone.on_push_start_failed()
    assert t_fail.disable_polling is True
    assert t_fail.watchdog_action == "cancel"
    assert t_fail.update_error == "MQTT push failed to start in phone app mode"
    assert t_fail.health.mqtt_transport_connected is False
    assert t_fail.health.mqtt_status_fresh is False

    # Stop resets MQTT link and freshness signals back to None.
    t_stop = sm_phone.on_push_stopped()
    assert t_stop.watchdog_action == "cancel"
    assert t_stop.health.mqtt_transport_connected is None
    assert t_stop.health.mqtt_status_fresh is None
    assert t_stop.health.last_push_status_at is None

    # HA primary mode: push events never disable polling or emit phone-app errors.
    sm_ha = AcquisitionHealthStateMachine(operation_mode=OPERATION_MODE_HA_PRIMARY)
    t_ha_conn = sm_ha.on_push_connected(
        False, sn8=None, cached_state=None, last_update_success=True
    )
    assert t_ha_conn.disable_polling is False
    assert t_ha_conn.update_error is None
    assert t_ha_conn.health.mqtt_transport_connected is False

    t_ha_fail = sm_ha.on_push_start_failed()
    assert t_ha_fail.disable_polling is False
    assert t_ha_fail.update_error is None


def test_push_energy_atw_merge_non_atw_ignore_and_poll_supersede() -> None:
    sm = AcquisitionHealthStateMachine(
        operation_mode=OPERATION_MODE_PHONE_APP, initial_sn8="171H120F"
    )
    atw_initial = build_snapshot(
        get_adapter("171H120F"), _status(temp=35), _sensors(t4=10.0), {}
    )
    # 0x04 energy payload (min 15 bytes): subtype 0x04 + data.
    energy_body = bytes([0x04] + [0] * 14)

    # ATW accepts 0x04, merges energy notify, and publishes updated snapshot.
    t_atw = sm.on_push_energy(
        energy_body, sn8="171H120F", now=200.0, cached_state=atw_initial
    )
    assert t_atw.publish_state is True
    assert t_atw.state is not None
    assert t_atw.health.last_push_status_at == 200.0
    assert sm.atw_energy_notify == energy_body

    # Subsequent poll supersedes pushed 0x04 energy state.
    sm.on_poll_complete(
        sn8="171H120F", status=_status(temp=35), sensors=_sensors(t4=10.0), raw_frames={}
    )
    assert sm.atw_energy_notify is None

    # Non-ATW (STANDARD) ignores 0x04 payload while still updating push activity.
    std_initial = build_snapshot(get_adapter(None), _status(), _sensors(), {})
    t_std = sm.on_push_energy(
        energy_body, sn8=None, now=300.0, cached_state=std_initial
    )
    assert t_std.publish_state is False
    assert t_std.health.last_push_status_at == 300.0
