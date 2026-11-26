"""Protocol dialect adapter registry for iLetComfort heat-pump families (#110, #172, #192)."""

from __future__ import annotations

from typing import Any

from .aquapura import AquapuraAdapter
from .atw import AtwAdapter
from .base import HeatPumpState, ProtocolAdapter, profile_climate_temp_bounds
from .kjrh120l import Kjrh120lAdapter
from .profile import ModelProfile, resolve_profile
from .standard import StandardAdapter

_ADAPTERS: dict[ModelProfile, ProtocolAdapter] = {
    ModelProfile.STANDARD: StandardAdapter(),
    ModelProfile.AQUAPURA: AquapuraAdapter(),
    ModelProfile.ATW: AtwAdapter(),
    ModelProfile.KJRH120L: Kjrh120lAdapter(),
}


def get_adapter(sn8_or_profile: str | ModelProfile | None) -> ProtocolAdapter:
    """Return the ProtocolAdapter for a given ``sn8`` model code or ``ModelProfile``."""
    profile = (
        sn8_or_profile
        if isinstance(sn8_or_profile, ModelProfile)
        else resolve_profile(sn8_or_profile)
    )
    return _ADAPTERS[profile]


def resolve_state(
    sn8: str | None, data: dict[str, Any] | None
) -> HeatPumpState | None:
    """Return the HeatPumpState from ``data`` or synthesize one on the fly.

    Allows HA entities to read normalized ``HeatPumpState`` properties even
    when a test constructs ``coordinator.data`` without ``"state"``.
    """
    if data is None:
        return None
    existing = data.get("state")
    if isinstance(existing, HeatPumpState):
        return existing
    status = data.get("status")
    sensors = data.get("sensors")
    if status is None and sensors is None:
        return None
    adapter = get_adapter(sn8)
    raw = data.get("raw_frames")
    has_authoritative = isinstance(raw, dict)
    raw_frames = dict(raw) if isinstance(raw, dict) else {}
    return HeatPumpState(
        profile=adapter.profile,
        variant=adapter.describe_variant(status),
        status=status,
        sensors=sensors,
        raw_frames=raw_frames,
        has_authoritative_raw_frames=has_authoritative,
    )


def is_kjrh120l_dual_state(
    sn8: str | None, data: dict[str, Any] | None, zone1_latched: bool
) -> bool:
    """Return whether a KJRH-120L unit has the dual Zone-1 capability (#192)."""
    state = resolve_state(sn8, data)
    if state is not None:
        return state.is_kjrh120l_dual(zone1_latched)
    return get_adapter(sn8).is_kjrh120l_dual(None, zone1_latched)


__all__ = [
    "AquapuraAdapter",
    "AtwAdapter",
    "HeatPumpState",
    "Kjrh120lAdapter",
    "ProtocolAdapter",
    "StandardAdapter",
    "get_adapter",
    "is_kjrh120l_dual_state",
    "profile_climate_temp_bounds",
    "resolve_state",
]
