"""STANDARD protocol adapter (default C3 layout)."""

from __future__ import annotations

from ..api import ITSSensors, ITSStatus
from ..c3_frame import build_c3_query
from ..model_profiles import ModelProfile


class StandardAdapter:
    """Default adapter for unknown/standard C3 heat pumps."""

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
