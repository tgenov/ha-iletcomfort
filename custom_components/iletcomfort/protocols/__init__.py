"""Protocol dialect adapter registry for iLetComfort heat-pump families (#110, #172, #192)."""

from __future__ import annotations

from .aquapura import AquapuraAdapter
from .atw import AtwAdapter
from .base import HeatPumpState, ProtocolAdapter, profile_climate_temp_bounds
from .kjrh120l import Kjrh120lAdapter
from .profile import ModelProfile, resolve_profile
from .snapshot import build_snapshot, merge_atw_energy_notify
from .standard import StandardAdapter
from .write import (
    FreshRead,
    WriteContext,
    WritePlan,
    WriteRefused,
    run_write_plan,
)

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


def is_kjrh120l_dual_state(
    sn8: str | None, state: HeatPumpState | None, zone1_latched: bool
) -> bool:
    """Return whether a KJRH-120L unit has the dual Zone-1 capability (#192)."""
    if state is not None:
        return state.is_kjrh120l_dual(zone1_latched)
    return get_adapter(sn8).is_kjrh120l_dual(None, zone1_latched)


__all__ = [
    "AquapuraAdapter",
    "FreshRead",
    "AtwAdapter",
    "HeatPumpState",
    "Kjrh120lAdapter",
    "ProtocolAdapter",
    "StandardAdapter",
    "build_snapshot",
    "WriteContext",
    "WritePlan",
    "WriteRefused",
    "get_adapter",
    "is_kjrh120l_dual_state",
    "merge_atw_energy_notify",
    "profile_climate_temp_bounds",
    "run_write_plan",
]
