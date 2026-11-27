"""Single pure snapshot builder for poll, MQTT status push and MQTT energy push (#225).

The coordinator assembles a ``HeatPumpState`` from three entry points: the
cloud poll, an MQTT 0x01 status push and an MQTT 0x04 energy push. Each must
apply the *same* profile decode and the *same* optional ATW subtype overlays,
otherwise a push silently drops values only the poll derived (#225: a 0x01
push wiped 0x10/0x0A/0x05/0x07 overlays in phone-app coexistence mode).

``build_snapshot`` is that one assembly step. It is framework-independent and
does no I/O: probing (the dialect's ``optional_probes``, run by the
coordinator) only records successful replies in ``raw_frames``.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..api import ITSSensors, ITSStatus
from .atw import (
    ATW_ENERGY_NOTIFY_MIN_LEN,
    ATW_ENERGY_NOTIFY_SUBTYPE,
    _atw_unit_s8,
    valid_energy_counter,
)
from .base import HeatPumpState, ProtocolAdapter

_ENERGY_COUNTER_SLICES = (slice(2, 6), slice(6, 10))
_ENERGY_OUTDOOR_INDEX = 10


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

    The selected dialect applies its overlays (``adapter.apply_overlays``;
    a no-op for dialects without optional subtypes). ATW overlay order:

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
      (``atw._reconcile_retained_silent``).

    Whether an optional subtype is *queried* (e.g. 0x0A only after 0x10
    succeeded) is decided by the dialect's ``optional_probes`` and run by the
    coordinator's probing; a frame present in ``raw_frames`` is a reply that
    probing accepted.
    """
    if sensors is not None:
        sensors = adapter.decode_sensors(sensors, status)

    status, sensors = adapter.apply_overlays(
        status,
        sensors,
        raw_frames,
        retained_subtypes=retained_subtypes,
        energy_notify=energy_notify,
    )

    return HeatPumpState(
        profile=adapter.profile,
        variant=adapter.describe_variant(status),
        status=status,
        sensors=sensors,
        raw_frames=dict(raw_frames),
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
