"""Protocol adapter registry for iLetComfort heat-pump families (#172)."""

from __future__ import annotations

from ..model_profiles import ModelProfile, resolve_profile
from .aquapura import AquapuraAdapter
from .atw import AtwAdapter
from .base import HeatPumpState, ProtocolAdapter
from .kjrh120l import Kjrh120lAdapter
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


__all__ = [
    "AquapuraAdapter",
    "AtwAdapter",
    "HeatPumpState",
    "Kjrh120lAdapter",
    "ProtocolAdapter",
    "StandardAdapter",
    "get_adapter",
]
