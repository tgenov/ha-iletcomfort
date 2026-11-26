"""Typed, single-intent semantic write commands for iLetComfort heat pumps (#113).

Each command dataclass represents exactly one user action. Validation happens
at construction time so invalid or contradictory command combinations cannot be
represented or passed across the entity → coordinator boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

HvacModeIntent = Literal["off", "heat", "cool", "fan_only"]
ZoneRunModeIntent = Literal["heat", "cool"]
ZoneNumber = Literal[1, 2]

_VALID_HVAC_MODES = frozenset(("off", "heat", "cool", "fan_only"))
_VALID_ZONE_RUN_MODES = frozenset(("heat", "cool"))
_VALID_ZONES = frozenset((1, 2))


def _require_finite_temp(value: float, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, got {value!r}")
    return float(value)


def _require_zone(zone: int) -> ZoneNumber:
    if isinstance(zone, bool) or not isinstance(zone, int) or zone not in _VALID_ZONES:
        raise ValueError(f"Zone must be 1 or 2, got {zone!r}")
    return zone  # type: ignore[return-value]


@dataclass(frozen=True)
class SetPowerCommand:
    """Power the primary climate zone on (restoring last state) or off."""

    on: bool


@dataclass(frozen=True)
class SetHvacModeCommand:
    """Set the primary climate entity's HVAC mode."""

    mode: HvacModeIntent

    def __post_init__(self) -> None:
        if self.mode not in _VALID_HVAC_MODES:
            raise ValueError(f"Unsupported HVAC mode intent {self.mode!r}")


@dataclass(frozen=True)
class SetTargetTemperatureCommand:
    """Set the primary climate entity's target temperature (°C)."""

    temperature: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "temperature", _require_finite_temp(self.temperature, "Target temperature")
        )


@dataclass(frozen=True)
class SetBoostCommand:
    """Toggle legacy ITS boost mode."""

    enabled: bool


@dataclass(frozen=True)
class SetSilentModeCommand:
    """Set silent/mute mode level (0=Off, 1=Level 1, 2=Level 2 / Super Silent)."""

    level: int | None = None
    enabled: bool | None = None

    def __post_init__(self) -> None:
        if self.level is None and self.enabled is None:
            raise ValueError("SetSilentModeCommand requires either level or enabled")
        if self.level is not None and self.enabled is not None:
            raise ValueError("SetSilentModeCommand cannot specify both level and enabled")
        if self.level is not None and (
            isinstance(self.level, bool)
            or not isinstance(self.level, int)
            or self.level not in (0, 1, 2)
        ):
            raise ValueError(f"Silent level must be 0, 1, or 2, got {self.level!r}")
        if self.enabled is not None and not isinstance(self.enabled, bool):
            raise ValueError(f"Silent enabled must be a bool, got {self.enabled!r}")


@dataclass(frozen=True)
class SetEcoModeCommand:
    """Toggle ATW ECO mode (subtype 0x07)."""

    enabled: bool


@dataclass(frozen=True)
class SetDhwPowerCommand:
    """Power domestic hot water (DHW) on or off."""

    on: bool


@dataclass(frozen=True)
class SetDhwTargetCommand:
    """Set domestic hot water (DHW) target temperature (°C)."""

    temperature: float

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "temperature", _require_finite_temp(self.temperature, "DHW target")
        )


@dataclass(frozen=True)
class SetFastDhwCommand:
    """Toggle ATW Fast DHW boost."""

    enabled: bool


@dataclass(frozen=True)
class SetZonePowerCommand:
    """Power Zone 1 or Zone 2 on or off."""

    zone: ZoneNumber
    on: bool

    def __post_init__(self) -> None:
        _require_zone(self.zone)


@dataclass(frozen=True)
class SetZoneTargetCommand:
    """Set Zone 1 or Zone 2 water target temperature (°C)."""

    zone: ZoneNumber
    temperature: float

    def __post_init__(self) -> None:
        _require_zone(self.zone)
        object.__setattr__(
            self, "temperature", _require_finite_temp(self.temperature, "Zone target")
        )


@dataclass(frozen=True)
class SetZoneRunModeCommand:
    """Switch Zone 1 run mode (KJRH-120L Heat/Cool paired with clamped setpoint)."""

    zone: Literal[1]
    mode: ZoneRunModeIntent

    def __post_init__(self) -> None:
        if isinstance(self.zone, bool) or not isinstance(self.zone, int) or self.zone != 1:
            raise ValueError(f"Run mode switching is only supported for Zone 1, got {self.zone!r}")
        if self.mode not in _VALID_ZONE_RUN_MODES:
            raise ValueError(f"Unsupported Zone run mode {self.mode!r}")


@dataclass(frozen=True)
class SetZoneCurveCommand:
    """Toggle weather-compensation curve for Zone 1 or Zone 2."""

    zone: ZoneNumber
    enabled: bool

    def __post_init__(self) -> None:
        _require_zone(self.zone)


HeatPumpCommand = (
    SetPowerCommand
    | SetHvacModeCommand
    | SetTargetTemperatureCommand
    | SetBoostCommand
    | SetSilentModeCommand
    | SetEcoModeCommand
    | SetDhwPowerCommand
    | SetDhwTargetCommand
    | SetFastDhwCommand
    | SetZonePowerCommand
    | SetZoneTargetCommand
    | SetZoneRunModeCommand
    | SetZoneCurveCommand
)

__all__ = [
    "HeatPumpCommand",
    "HvacModeIntent",
    "SetBoostCommand",
    "SetDhwPowerCommand",
    "SetDhwTargetCommand",
    "SetEcoModeCommand",
    "SetFastDhwCommand",
    "SetHvacModeCommand",
    "SetPowerCommand",
    "SetSilentModeCommand",
    "SetTargetTemperatureCommand",
    "SetZoneCurveCommand",
    "SetZonePowerCommand",
    "SetZoneRunModeCommand",
    "SetZoneTargetCommand",
    "ZoneNumber",
    "ZoneRunModeIntent",
]
