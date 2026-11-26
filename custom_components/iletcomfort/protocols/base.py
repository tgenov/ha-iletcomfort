"""ProtocolAdapter interface and normalized HeatPumpState domain model (#110, #172, #192)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..commands import HeatPumpCommand
from .profile import ModelProfile
from .write import WriteContext, WritePlan


def profile_climate_temp_bounds(
    profile: ModelProfile, set_mode: int | None
) -> tuple[float, float]:
    """Return ``(min_temp, max_temp)`` for ``profile`` and SET ``set_mode``."""
    from . import get_adapter

    return get_adapter(profile).climate_temp_bounds(None, set_mode)


@dataclass(frozen=True)
class HeatPumpState:
    """Normalized, profile-agnostic snapshot of a heat pump poll cycle.

    Entities and diagnostics read normalized properties without branching on
    ``ModelProfile`` or raw byte layouts; all model-specific behavior is
    delegated to the selected ``ProtocolAdapter`` (#110).
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

    @property
    def has_atw_0x10(self) -> bool:
        """Return True when live ATW subtype 0x10 telemetry populated this state."""
        return 0x10 in self.raw_frames

    @property
    def atw_c3_basic(self) -> C3BasicStatus | None:
        """Return the C3 basic status when supported by this dialect, else None."""
        from . import get_adapter

        return get_adapter(self.profile).c3_basic(self.status)

    def is_kjrh120l_dual(self, zone1_latched: bool) -> bool:
        """Return whether this unit is the dual Zone-1 + DHW KJRH-120L variant."""
        from . import get_adapter

        return get_adapter(self.profile).is_kjrh120l_dual(self.status, zone1_latched)

    @property
    def climate_current_temperature(self) -> float | None:
        """Return dialect-aware climate current temperature (#110, #192)."""
        from . import get_adapter

        return get_adapter(self.profile).climate_current_temperature(self.sensors)

    def climate_temp_bounds(self, set_mode: int | None) -> tuple[float, float]:
        """Return ``(min_temp, max_temp)`` for the climate entity (#110)."""
        from . import get_adapter

        return get_adapter(self.profile).climate_temp_bounds(self.status, set_mode)


class ProtocolAdapter(Protocol):
    """Per-family protocol dialect contract (#110)."""

    profile: ModelProfile
    # Whether the device publishes 0x01 status pushes on a fixed cadence while
    # idle. Only then can silence on a live MQTT link mean the data is stale.
    periodic_status_push: bool

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

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        """Return the dialect-appropriate climate current temperature."""

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        """Return ``(min_temp, max_temp)`` for the climate entity."""

    def is_kjrh120l_dual(
        self, status: ITSStatus | None, zone1_latched: bool
    ) -> bool:
        """Return whether this device is a dual Zone-1 + DHW KJRH-120L unit."""

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        """Return the C3 basic status when supported by this dialect, else None."""

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        """Plan ``command`` as a wire write for this dialect (pure, no I/O, #113).

        Returns a static ``WritePlan`` or a read-modify-write plan naming the
        fresh query to run immediately before encoding. Raises ``WriteRefused``
        (surfaced verbatim) for HA-side gating refusals, and ``ApiError`` /
        ``ValueError`` when the command is outside this dialect's validated
        write envelope.
        """
