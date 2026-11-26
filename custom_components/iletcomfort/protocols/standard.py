"""STANDARD protocol dialect (default C3 layout, #110)."""

from __future__ import annotations

from ..api import TEMP_RANGES, ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..c3_frame import build_c3_query
from .profile import ModelProfile


def default_climate_temp_bounds(set_mode: int | None) -> tuple[float, float]:
    """Return default ``(min_temp, max_temp)`` for a SET ``set_mode``."""
    if set_mode is not None and set_mode in TEMP_RANGES:
        return float(TEMP_RANGES[set_mode][0]), float(TEMP_RANGES[set_mode][1])
    return 10.0, 40.0


class StandardAdapter:
    """Default dialect adapter for unknown/standard C3 heat pumps."""

    profile = ModelProfile.STANDARD

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return status

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        return sensors

    def describe_variant(self, status: ITSStatus | None) -> str:
        return "standard"

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        return sensors.twin_temp if sensors is not None else None

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        return default_climate_temp_bounds(set_mode)

    def is_kjrh120l_dual(
        self, status: ITSStatus | None, zone1_latched: bool
    ) -> bool:
        return False

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return None
