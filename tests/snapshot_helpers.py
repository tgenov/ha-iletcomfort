"""Shared helper for building a typed ``coordinator.data`` snapshot in tests (#111).

``coordinator.data`` is a ``HeatPumpState`` built by the production
``build_snapshot`` (the same step the poll and MQTT push paths use), so a test
snapshot goes through the same profile sensors decode and ATW overlays as a
real one.
"""

from __future__ import annotations

from collections.abc import Mapping

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.protocols import (
    HeatPumpState,
    build_snapshot,
    get_adapter,
)


def make_state(
    status: ITSStatus | None = None,
    sensors: ITSSensors | None = None,
    raw_frames: Mapping[int, bytes] | None = None,
    *,
    sn8: str | None = None,
) -> HeatPumpState:
    """Return the ``HeatPumpState`` the coordinator would publish.

    ``sn8`` selects the dialect (``None`` is STANDARD) and must match the
    coordinator's ``sn8`` for profile-specific reads. When ``raw_frames`` is
    omitted it is derived from ``status.raw_body`` (subtype 0x01) and
    ``sensors.raw_body`` (keyed by its first byte), mirroring what the poll
    records.
    """
    if status is None:
        status = ITSStatus()
    if raw_frames is None:
        derived: dict[int, bytes] = {}
        if status.raw_body:
            derived[0x01] = bytes(status.raw_body)
        if sensors is not None and sensors.raw_body:
            derived[sensors.raw_body[0]] = bytes(sensors.raw_body)
        raw_frames = derived
    return build_snapshot(get_adapter(sn8), status, sensors, raw_frames)
