"""Protocol dialect adapter registry for iLetComfort heat-pump families (#110, #172, #192).

Model selection happens here, once: ``get_adapter(sn8)`` resolves the sn8 model
code (``profile.resolve_profile``; unknown/missing → STANDARD) to one
``ProtocolAdapter``. Callers use that adapter's methods and capability flags
and never compare ``ModelProfile`` values themselves.

Adding a dialect: create ``protocols/<dialect>.py`` with an adapter class that
implements every ``ProtocolAdapter`` member (``base.py``), add a
``ModelProfile`` member and its sn8 code to ``profile.py``, and register the
adapter in ``_ADAPTERS`` below.
"""

from __future__ import annotations

import dataclasses

from .aquapura import AquapuraAdapter
from .atw import AtwAdapter
from .base import HeatPumpState, ProtocolAdapter, profile_climate_temp_bounds
from .kjr86t3 import Kjr86t3Adapter
from .kjrh120l import Kjrh120lAdapter
from .probes import OptionalProbe
from .profile import ModelProfile, resolve_profile
from .snapshot import build_snapshot, flatten_status, merge_atw_energy_notify
from .standard import StandardAdapter
from .views import (
    REMOVABLE_CAPABILITIES,
    UNSUPPORTED,
    ClimateView,
    EntityCapabilities,
    EntitySupport,
    ViewContext,
)
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
    ModelProfile.KJR86T3: Kjr86t3Adapter(),
}


def get_adapter(sn8_or_profile: str | ModelProfile | None) -> ProtocolAdapter:
    """Return the ProtocolAdapter for a given ``sn8`` model code or ``ModelProfile``."""
    profile = (
        sn8_or_profile
        if isinstance(sn8_or_profile, ModelProfile)
        else resolve_profile(sn8_or_profile)
    )
    return _ADAPTERS[profile]


def project_entity_capabilities(
    sn8: str | None, state: HeatPumpState | None, ctx: ViewContext
) -> EntityCapabilities:
    """Project the optional entities for a device (pure, #192).

    The dialect that decoded ``state`` (else the one ``sn8`` selects) decides
    what exists now. While ``sn8`` is unknown the profile is not settled:
    whatever any dialect could expose stays ``possible``, so entities that can
    only appear once ``sn8`` resolves are still added later.
    """
    adapter = get_adapter(state.profile if state is not None else sn8)
    status = state.status if state is not None else None
    caps = adapter.entity_capabilities(status, ctx)
    if sn8 is not None:
        return caps
    for other in _ADAPTERS.values():
        caps = caps.widen_possible(other.entity_capabilities(None, ctx))
    return dataclasses.replace(caps, profile_settled=False)


def project_climate_view(
    sn8: str | None, state: HeatPumpState | None, ctx: ViewContext
) -> ClimateView:
    """Project the main climate entity for a device (pure, #192)."""
    adapter = get_adapter(state.profile if state is not None else sn8)
    return adapter.climate_view(state.status if state is not None else None, ctx)


def project_zone2_climate_view(
    sn8: str | None, state: HeatPumpState | None, ctx: ViewContext
) -> ClimateView:
    """Project the Zone-2 climate entity for a device (pure, #192)."""
    adapter = get_adapter(state.profile if state is not None else sn8)
    return adapter.zone2_climate_view(state.status if state is not None else None, ctx)


def is_kjrh120l_dual_state(
    sn8: str | None, state: HeatPumpState | None, zone1_latched: bool
) -> bool:
    """Return whether a KJRH-120L unit has the dual Zone-1 capability (#192)."""
    if state is not None:
        return state.is_kjrh120l_dual(zone1_latched)
    return get_adapter(sn8).is_kjrh120l_dual(None, zone1_latched)


__all__ = [
    "AquapuraAdapter",
    "ClimateView",
    "EntityCapabilities",
    "EntitySupport",
    "REMOVABLE_CAPABILITIES",
    "UNSUPPORTED",
    "ViewContext",
    "FreshRead",
    "AtwAdapter",
    "HeatPumpState",
    "Kjr86t3Adapter",
    "Kjrh120lAdapter",
    "OptionalProbe",
    "ProtocolAdapter",
    "StandardAdapter",
    "build_snapshot",
    "flatten_status",
    "WriteContext",
    "WritePlan",
    "WriteRefused",
    "get_adapter",
    "is_kjrh120l_dual_state",
    "merge_atw_energy_notify",
    "profile_climate_temp_bounds",
    "project_climate_view",
    "project_entity_capabilities",
    "project_zone2_climate_view",
    "run_write_plan",
]
