"""ProtocolAdapter interface and normalized HeatPumpState domain model (#172, #192)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..api import ITSSensors, ITSStatus, TEMP_RANGES
from ..c3_basic import C3BasicStatus
from ..model_profiles import (
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
    ModelProfile,
    atw_sensors_are_placeholder,
    kjrh120l_has_zone1,
)


def profile_climate_temp_bounds(
    profile: ModelProfile, set_mode: int | None
) -> tuple[float, float]:
    """Return ``(min_temp, max_temp)`` for ``profile`` and SET ``set_mode``."""
    if profile is ModelProfile.KJRH120L:
        return float(KJRH120L_TEMP_MIN), float(KJRH120L_TEMP_MAX)
    if set_mode is not None and set_mode in TEMP_RANGES:
        return float(TEMP_RANGES[set_mode][0]), float(TEMP_RANGES[set_mode][1])
    return 10.0, 40.0


@dataclass(frozen=True)
class HeatPumpState:
    """Normalized, profile-agnostic snapshot of a heat pump poll cycle.

    Entities and diagnostics can read normalized properties without branching
    on ``ModelProfile`` or raw byte layouts, while ``status`` and ``sensors``
    remain accessible for full backwards compatibility.
    """

    profile: ModelProfile
    variant: str
    status: ITSStatus | None = None
    sensors: ITSSensors | None = None
    raw_frames: dict[int, bytes] = field(default_factory=dict)
    has_authoritative_raw_frames: bool = False

    @property
    def outdoor_temp(self) -> float | None:
        if self.sensors is not None and self.sensors.t4_temp is not None:
            return self.sensors.t4_temp
        if self.status is not None:
            return self.status.kjrh120l_outdoor_temp
        return None

    @property
    def dhw_tank_temp(self) -> float | None:
        if self.sensors is not None:
            return self.sensors.th_temp
        return None

    @property
    def compressor_running(self) -> bool | None:
        return self.status.comp_running if self.status is not None else None

    @property
    def compressor_freq(self) -> int | None:
        return self.status.comp_frq if self.status is not None else None

    @property
    def error_code(self) -> int:
        return self.status.error_code if self.status is not None else 0

    @property
    def has_atw_0x10(self) -> bool:
        """Return True when live ATW subtype 0x10 telemetry populated this state."""
        if self.has_authoritative_raw_frames:
            return 0x10 in self.raw_frames
        raw_body = getattr(self.sensors, "raw_body", b"") if self.sensors is not None else b""
        return bytes(raw_body)[:1] == b"\x10"

    @property
    def atw_c3_basic(self) -> C3BasicStatus | None:
        """Return the C3 basic status for ATW units, else None."""
        if self.status is None or self.profile is not ModelProfile.ATW:
            return None
        return self.status.c3_basic

    def is_kjrh120l_dual(self, zone1_latched: bool) -> bool:
        """Return whether this KJRH-120L unit is the dual Zone-1 + DHW variant."""
        if self.profile is not ModelProfile.KJRH120L:
            return False
        if zone1_latched is True:
            return True
        return bool(self.status is not None and kjrh120l_has_zone1(self.status.raw_body))

    @property
    def climate_current_temperature(self) -> float | None:
        """Return profile-aware climate current temperature (#192)."""
        if self.sensors is None:
            return None
        if self.profile is ModelProfile.ATW:
            if (
                self.sensors.raw_body[:1] == b"\x10"
                and self.sensors.twout_temp is not None
            ):
                return self.sensors.twout_temp
            if atw_sensors_are_placeholder(self.sensors.raw_body):
                return None
            return self.sensors.th_temp
        if self.profile is ModelProfile.AQUAPURA:
            if self.sensors.th_temp not in (None, 0.0):
                return self.sensors.th_temp
            if self.sensors.twout_temp is not None:
                return self.sensors.twout_temp
            return self.sensors.twin_temp
        return self.sensors.twin_temp

    def climate_temp_bounds(self, set_mode: int | None) -> tuple[float, float]:
        """Return ``(min_temp, max_temp)`` for the climate entity."""
        basic = self.atw_c3_basic
        if isinstance(basic, C3BasicStatus):
            low, high = (
                basic.zone1_water_range
                if basic.zone1_water_temp
                else (basic.room_min, basic.room_max)
            )
            if 0 < low < high:
                return float(low), float(high)
        return profile_climate_temp_bounds(self.profile, set_mode)


class ProtocolAdapter(Protocol):
    """Per-family protocol adapter contract."""

    profile: ModelProfile

    def build_query(self, subtype: int) -> str:
        """Build the wire hex query command for ``subtype``."""

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        """Return the profile-canonical ITSStatus for ``status``."""

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        """Return the profile-canonical ITSSensors for ``sensors``."""

    def describe_variant(self, status: ITSStatus | None) -> str:
        """Return the structural variant label for diagnostics."""
