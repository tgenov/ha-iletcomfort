"""Pure, dialect-owned entity projections (#192).

A dialect projects what Home Assistant should expose for a device from the
decoded status and a small ``ViewContext`` (owner options and latched
observations). Platforms map these plain dataclasses onto HA entities and do
not branch on ``ModelProfile`` / ``sn8`` themselves.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, fields, replace
from typing import Any

from ..api import MODE_COOL, MODE_HEAT, MODE_OFF, MODE_WATERPUMP, ITSStatus


@dataclass(frozen=True)
class ViewContext:
    """Owner options and latched observations a projection may depend on.

    ``c3_basic_control`` is the options-flow opt-in for ATW C3 basic writes.
    ``kjrh120l_zone1_latched`` is the coordinator's latched (and
    profile-gated) observation that a KJRH-120L unit has a Zone 1.
    """

    c3_basic_control: bool = False
    kjrh120l_zone1_latched: bool = False


@dataclass(frozen=True)
class EntitySupport:
    """Whether one optional entity (or entity group) is supported.

    ``exists``    create it now.
    ``possible``  it exists now or may still appear later (keep listening);
                  when False the dialect can never expose it, so a stale
                  registry entry may be removed.
    ``available`` the data backing an already created entity is present.
    ``writable``  its writes are within the dialect's enabled write envelope.
    """

    exists: bool = False
    possible: bool = False
    available: bool = False
    writable: bool = False


UNSUPPORTED = EntitySupport()


@dataclass(frozen=True)
class EntityCapabilities:
    """Which optional entities the selected dialect exposes for this device.

    The always-present entities (main climate, sensors, binary sensors) are
    not listed. ``profile_settled`` is False while the ``sn8`` model code is
    still unknown: the default (STANDARD) dialect then decides ``exists``, but
    anything another dialect could expose stays ``possible``.
    """

    boost: EntitySupport = UNSUPPORTED
    mute: EntitySupport = UNSUPPORTED
    zone2_climate: EntitySupport = UNSUPPORTED
    dhw_water_heater: EntitySupport = UNSUPPORTED
    # ATW opt-in C3 switches: Weather Curve, Silent Mode and ECO Mode.
    c3_controls: EntitySupport = UNSUPPORTED
    zone2_curve: EntitySupport = UNSUPPORTED
    fast_dhw: EntitySupport = UNSUPPORTED
    # KJRH-120L Zone 1 entities: Zone 1 Power, DHW Power, Mode, DHW Setpoint
    # and Zone 1 Target.
    kjrh120l_zone1: EntitySupport = UNSUPPORTED
    profile_settled: bool = True

    def widen_possible(self, other: EntityCapabilities) -> EntityCapabilities:
        """Return a copy that is also ``possible`` wherever ``other`` is."""
        changes: dict[str, Any] = {}
        for f in fields(self):
            mine = getattr(self, f.name)
            if not isinstance(mine, EntitySupport):
                continue
            theirs: EntitySupport = getattr(other, f.name)
            if theirs.possible and not mine.possible:
                changes[f.name] = replace(mine, possible=True)
        return replace(self, **changes)


# Capabilities whose ``possible`` depends only on the dialect (never on an owner
# option such as the C3 opt-in), so a stale registry entry can safely be
# removed when they are not possible. Do not add option-dependent entries:
# toggling the option would delete the entities and the user's customisations.
REMOVABLE_CAPABILITIES = frozenset({"boost", "mute"})

# Boost and Silent Mode on dialects that write them with the legacy ITS SET.
LEGACY_BOOST_MUTE = EntitySupport(exists=True, possible=True, available=True, writable=True)


# -- climate ----------------------------------------------------------------------

# HVAC modes as plain strings; HA's ``HVACMode`` values are identical.
HVAC_OFF = "off"
HVAC_HEAT = "heat"
HVAC_COOL = "cool"
HVAC_FAN_ONLY = "fan_only"
LEGACY_HVAC_MODES = (HVAC_OFF, HVAC_HEAT, HVAC_COOL, HVAC_FAN_ONLY)
HEAT_OFF_MODES = (HVAC_OFF, HVAC_HEAT)

# Status-frame operating mode → HVAC mode (3 = Auto has no HA mode and reads Off).
_STATUS_MODE_TO_HVAC = {0: HVAC_OFF, 1: HVAC_HEAT, 2: HVAC_COOL, 4: HVAC_FAN_ONLY}
# HVAC mode → legacy SET mode byte, which selects the mode's temperature bounds.
_HVAC_TO_SET_MODE = {
    HVAC_OFF: MODE_OFF,
    HVAC_HEAT: MODE_HEAT,
    HVAC_COOL: MODE_COOL,
    HVAC_FAN_ONLY: MODE_WATERPUMP,
}

# User-facing refusals; kept identical to the pre-#192 climate entity messages.
FAN_ONLY_TEMPERATURE_REFUSED = "Temperature control is unavailable in Fan Only mode"
ATW_CONTROL_DISABLED = (
    "ATW (171H120F) control is disabled by default; enable "
    "experimental ATW control in the integration options"
)
KJRH120L_ZONE1_READ_ONLY = (
    "Zone-1 control is unavailable because its write command has not "
    "been validated; use the DHW setpoint entity for hot water"
)
# Fallback range when a C3 basic zone reports zeroed or inverted limits.
C3_FALLBACK_RANGE = (10.0, 40.0)


@dataclass(frozen=True)
class ClimateView:
    """What a climate entity shows and accepts, projected by the dialect (#192).

    ``read_only_reason`` refuses every write (raised before anything is sent);
    ``temperature_refusal`` additionally refuses target-temperature writes.
    ``settable_hvac_modes`` lists the modes a write may select, or None when
    the dialect's ``plan_write`` decides. Target writes are clamped to
    ``[min_temp, max_temp]`` by the dialect's ``plan_write`` (``clamp``).
    """

    hvac_modes: tuple[str, ...]
    hvac_mode: str
    target_temperature: float | None
    target_temperature_step: float
    min_temp: float
    max_temp: float
    supports_target_temperature: bool
    supports_turn_on_off: bool
    available: bool = True
    read_only_reason: str | None = None
    temperature_refusal: str | None = None
    settable_hvac_modes: tuple[str, ...] | None = None

    def clamp(self, temperature: float) -> float:
        """Clamp a requested target into this view's bounds."""
        return max(self.min_temp, min(float(temperature), self.max_temp))


def status_hvac_mode(status: ITSStatus | None) -> str:
    """HVAC mode of a status frame's operating mode (unknown → off)."""
    if status is None:
        return HVAC_OFF
    return _STATUS_MODE_TO_HVAC.get(status.mode, HVAC_OFF)


def status_active_setpoint(status: ITSStatus) -> float:
    """Setpoint of the active mode in °C (falls back to the direct target byte)."""
    if status.t5s_def is not None:
        return status.t5s_def
    return float(status.set_temperature)


def legacy_climate_view(
    status: ITSStatus | None,
    bounds: Callable[[int | None], tuple[float, float]],
    *,
    hvac_modes: tuple[str, ...] = LEGACY_HVAC_MODES,
    step: float = 1.0,
    read_only_reason: str | None = None,
    controls_hidden: bool = False,
    zone1_target: float | None = None,
) -> ClimateView:
    """Climate view driven by the status operating mode (legacy ITS semantics).

    ``bounds(set_mode)`` returns the dialect's ``(min, max)`` for the SET mode
    of the current HVAC mode. ``controls_hidden`` drops every feature (a
    read-only card); ``zone1_target`` replaces the active setpoint when given.
    """
    mode = status_hvac_mode(status)
    low, high = bounds(_HVAC_TO_SET_MODE.get(mode))
    fan_only = mode == HVAC_FAN_ONLY
    target: float | None
    if status is None or fan_only:
        target = None
    elif zone1_target is not None:
        target = zone1_target
    else:
        target = status_active_setpoint(status)
    return ClimateView(
        hvac_modes=hvac_modes,
        hvac_mode=mode,
        target_temperature=target,
        target_temperature_step=step,
        min_temp=low,
        max_temp=high,
        supports_target_temperature=not controls_hidden and not fan_only,
        supports_turn_on_off=not controls_hidden,
        read_only_reason=read_only_reason,
        temperature_refusal=FAN_ONLY_TEMPERATURE_REFUSED if fan_only else None,
    )


def read_only_c3_hvac_modes(
    *, cap_heat: bool, cap_cool: bool, zone_power: bool, cooling: bool
) -> tuple[str, ...]:
    """HVAC modes a read-only C3 zone reports from its capability bits."""
    modes = [HVAC_OFF]
    if cap_heat or not cap_cool or (zone_power and not cooling):
        modes.append(HVAC_HEAT)
    if cap_cool or cooling:
        modes.append(HVAC_COOL)
    return tuple(modes)


# A unit without a C3 basic Zone 2: the Zone-2 entity stays unavailable.
NO_ZONE2_VIEW = ClimateView(
    hvac_modes=(HVAC_OFF,),
    hvac_mode=HVAC_OFF,
    target_temperature=None,
    target_temperature_step=1.0,
    min_temp=C3_FALLBACK_RANGE[0],
    max_temp=C3_FALLBACK_RANGE[1],
    supports_target_temperature=False,
    supports_turn_on_off=False,
    available=False,
    read_only_reason=ATW_CONTROL_DISABLED,
    settable_hvac_modes=HEAT_OFF_MODES,
)


__all__ = [
    "ATW_CONTROL_DISABLED",
    "C3_FALLBACK_RANGE",
    "ClimateView",
    "EntityCapabilities",
    "EntitySupport",
    "FAN_ONLY_TEMPERATURE_REFUSED",
    "HEAT_OFF_MODES",
    "KJRH120L_ZONE1_READ_ONLY",
    "LEGACY_BOOST_MUTE",
    "LEGACY_HVAC_MODES",
    "NO_ZONE2_VIEW",
    "REMOVABLE_CAPABILITIES",
    "UNSUPPORTED",
    "ViewContext",
    "legacy_climate_view",
    "read_only_c3_hvac_modes",
    "status_active_setpoint",
    "status_hvac_mode",
]
