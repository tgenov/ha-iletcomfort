"""Single pure snapshot builder for poll, MQTT status push and MQTT energy push (#225).

The coordinator assembles a ``HeatPumpState`` from three entry points: the
cloud poll, an MQTT 0x01 status push and an MQTT 0x04 energy push. Each must
apply the *same* profile decode and the *same* optional ATW subtype overlays,
otherwise a push silently drops values only the poll derived (#225: a 0x01
push wiped 0x10/0x0A/0x05/0x07 overlays in phone-app coexistence mode).

``build_snapshot`` is that one assembly step. It is framework-independent and
does no I/O: probing (whether/when to query an optional subtype) stays in the
coordinator, which only records successful replies in ``raw_frames``.
"""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses

from ..api import ITSSensors, ITSStatus
from .atw import (
    ATW_ECO_SUBTYPE,
    ATW_ENERGY_NOTIFY_MIN_LEN,
    ATW_ENERGY_NOTIFY_SUBTYPE,
    ATW_HMI_PARAMS_SUBTYPE,
    ATW_SILENT_SUBTYPE,
    ATW_UNIT_PARAMS_SUBTYPE,
    _atw_unit_s8,
    apply_atw_eco,
    apply_atw_energy_notify,
    apply_atw_hmi_params,
    apply_atw_silent,
    apply_atw_unit_params,
    valid_energy_counter,
)
from .base import HeatPumpState, ProtocolAdapter
from .profile import ModelProfile

_ENERGY_COUNTER_SLICES = (slice(2, 6), slice(6, 10))
_ENERGY_OUTDOOR_INDEX = 10
_ATW_UNIT_COUNTER_FIELDS = ("total_kwh", "total_thermal_kwh")


def build_snapshot(
    adapter: ProtocolAdapter,
    status: ITSStatus,
    sensors: ITSSensors | None,
    raw_frames: Mapping[int, bytes],
    *,
    retained_subtypes: bool = False,
    energy_notify: bytes | None = None,
) -> HeatPumpState:
    """Apply the profile sensors decode and every retained subtype overlay.

    ``status`` is the profile-decoded subtype-0x01 status (``query_status``
    output, ``adapter.decode_status`` of a pushed frame, or the previous
    snapshot's status). ``sensors`` is the subtype-0x02 base (a fresh query
    result or the previous snapshot's sensors). ``raw_frames`` holds the
    optional subtype replies to overlay. The sensors decode and every overlay
    only *set* fields, so building on a previous snapshot is idempotent.

    ATW overlay order:

    1. 0x10 unit parameters (compressor, pressures, energy, live sensors);
    2. 0x0A HMI parameters (compressor run hours);
    3. 0x05 silent and 0x07 ECO, only when the status matches the C3 basic
       layout;
    4. 0x04 MQTT energy notify last, so the newest energy counters win.
       ``energy_notify`` (the coordinator's sentinel-filtered 0x04 state)
       is applied in place of the literal ``raw_frames[0x04]`` when given.

    ``retained_subtypes=True`` (MQTT pushes) means the optional subtype
    frames were *not* re-queried alongside ``status``; they date from the
    last poll. Then only values a push cannot contradict are re-applied:

    - 0x10 cumulative counters (``total_kwh``, ``total_thermal_kwh``) only.
      Instantaneous status values (compressor, pressures, EXV) stay as the
      0x01 decode leaves them instead of freezing at the poll's reading, and
      0x10 sensor values are not re-applied (the retained sensors carry them).
    - 0x0A run hours and 0x07 ECO (no push frame carries ECO; #139).
    - 0x05 reconciled against the current 0x01 Silent bit
      (``_reconcile_retained_silent``).

    Whether an optional subtype is *queried* (e.g. 0x0A only after 0x10
    succeeded) is decided by the coordinator's probing; a frame present in
    ``raw_frames`` is a reply that probing accepted.
    """
    if sensors is not None:
        sensors = adapter.decode_sensors(sensors, status)

    if adapter.profile is ModelProfile.ATW:
        unit_body = raw_frames.get(ATW_UNIT_PARAMS_SUBTYPE)
        if unit_body is not None:
            if retained_subtypes:
                status = _apply_atw_unit_counters(unit_body, status)
            elif sensors is not None:
                status, sensors = apply_atw_unit_params(unit_body, status, sensors)
            else:
                status, _ = apply_atw_unit_params(unit_body, status, ITSSensors())
        hmi_body = raw_frames.get(ATW_HMI_PARAMS_SUBTYPE)
        if hmi_body is not None:
            status = apply_atw_hmi_params(hmi_body, status)
        if status.c3_basic is not None:
            silent_body = raw_frames.get(ATW_SILENT_SUBTYPE)
            if silent_body is not None:
                status = (
                    _reconcile_retained_silent(silent_body, status)
                    if retained_subtypes
                    else apply_atw_silent(silent_body, status)
                )
            eco_body = raw_frames.get(ATW_ECO_SUBTYPE)
            if eco_body is not None:
                status = apply_atw_eco(eco_body, status)
        energy_body = (
            energy_notify
            if energy_notify is not None
            else raw_frames.get(ATW_ENERGY_NOTIFY_SUBTYPE)
        )
        if energy_body is not None:
            status, sensors = apply_atw_energy_notify(energy_body, status, sensors)

    return HeatPumpState(
        profile=adapter.profile,
        variant=adapter.describe_variant(status),
        status=status,
        sensors=sensors,
        raw_frames=dict(raw_frames),
    )


def _apply_atw_unit_counters(unit_body: bytes, status: ITSStatus) -> ITSStatus:
    """Re-apply only the cumulative (TOTAL_INCREASING) counters from a 0x10 frame."""
    full, _ = apply_atw_unit_params(unit_body, status, ITSSensors())
    return dataclasses.replace(
        status,
        **{field: getattr(full, field) for field in _ATW_UNIT_COUNTER_FIELDS},
    )


def _reconcile_retained_silent(silent_body: bytes, status: ITSStatus) -> ITSStatus:
    """Overlay a retained 0x05 frame, letting the current 0x01 Silent bit win.

    0x01 ``body[3] & 0x02`` carries only binary Silent on/off; the level lives
    in 0x05 (#139). With a 0x05 frame from an earlier poll:

    - 0x01 off: Silent off, level 0; keep ``atw_silent_super_selected`` from
      0x05 (bit 3 is retained by the unit even when Silent is off).
    - 0x01 on and 0x05 on: keep the 0x05 level.
    - 0x01 on but 0x05 off: the level is unknown, so the silent fields are
      cleared and entities fall back to ``c3_basic.silent_mode``.
    """
    overlaid = apply_atw_silent(silent_body, status)
    basic = status.c3_basic
    if basic is None or overlaid.atw_silent_mode is None:
        # Short (<= 6-byte) 0x05 ECO fallback or an invalid frame.
        return overlaid
    if not basic.silent_mode:
        return dataclasses.replace(overlaid, atw_silent_mode=False, atw_silent_level=0)
    if overlaid.atw_silent_mode:
        return overlaid
    return dataclasses.replace(
        overlaid,
        atw_silent_mode=None,
        atw_silent_level=None,
        atw_silent_super_selected=None,
    )


def merge_atw_energy_notify(previous: bytes | None, new: bytes) -> bytes:
    """Return ``new`` with invalid fields carried over from ``previous`` (#129, #225).

    A 0x04 push can carry sentinels (0xFFFFFFFF / 0 counters, 0x7F outdoor)
    for individual fields. ``apply_atw_energy_notify`` skips those, so keeping
    only the latest literal frame would lose the last valid value once the
    snapshot is re-derived. The coordinator keeps this merged frame (the
    latest valid value of each field) separately and passes it to
    ``build_snapshot(energy_notify=...)``; ``raw_frames[0x04]`` stays the
    literal last frame for diagnostics.
    """
    if (
        previous is None
        or len(previous) < ATW_ENERGY_NOTIFY_MIN_LEN
        or previous[0] != ATW_ENERGY_NOTIFY_SUBTYPE
        or len(new) < ATW_ENERGY_NOTIFY_MIN_LEN
        or new[0] != ATW_ENERGY_NOTIFY_SUBTYPE
    ):
        return bytes(new)
    merged = bytearray(new)
    for counter in _ENERGY_COUNTER_SLICES:
        if not valid_energy_counter(int.from_bytes(new[counter], "big")):
            merged[counter] = previous[counter]
    if _atw_unit_s8(new[_ENERGY_OUTDOOR_INDEX]) is None:
        merged[_ENERGY_OUTDOOR_INDEX] = previous[_ENERGY_OUTDOOR_INDEX]
    return bytes(merged)
