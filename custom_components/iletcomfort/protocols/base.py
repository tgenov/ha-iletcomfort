"""ProtocolAdapter interface and normalized HeatPumpState domain model (#172)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..api import ITSSensors, ITSStatus
from ..model_profiles import ModelProfile


@dataclass(frozen=True)
class HeatPumpState:
    """Normalized, profile-agnostic snapshot of a heat pump poll cycle.

    Entities and diagnostics can read normalized properties without branching
    on ``ModelProfile`` or raw byte layouts, while ``status`` and ``sensors``
    remain accessible for full backwards compatibility.
    """

    profile: ModelProfile
    variant: str
    status: ITSStatus
    sensors: ITSSensors | None = None
    raw_frames: dict[int, bytes] = field(default_factory=dict)

    @property
    def outdoor_temp(self) -> float | None:
        if self.sensors is not None and self.sensors.t4_temp is not None:
            return self.sensors.t4_temp
        return self.status.kjrh120l_outdoor_temp

    @property
    def dhw_tank_temp(self) -> float | None:
        if self.sensors is not None:
            return self.sensors.th_temp
        return None

    @property
    def compressor_running(self) -> bool | None:
        return self.status.comp_running

    @property
    def compressor_freq(self) -> int | None:
        return self.status.comp_frq

    @property
    def error_code(self) -> int:
        return self.status.error_code


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
