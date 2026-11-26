"""Pure, dialect-owned entity projections (#192).

A dialect projects what Home Assistant should expose for a device from the
decoded status and a small ``ViewContext`` (owner options and latched
observations). Platforms map these plain dataclasses onto HA entities and do
not branch on ``ModelProfile`` / ``sn8`` themselves.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, replace
from typing import Any


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


# Boost and Silent Mode on dialects that write them with the legacy ITS SET.
LEGACY_BOOST_MUTE = EntitySupport(exists=True, possible=True, available=True, writable=True)


__all__ = [
    "EntityCapabilities",
    "EntitySupport",
    "LEGACY_BOOST_MUTE",
    "UNSUPPORTED",
    "ViewContext",
]
