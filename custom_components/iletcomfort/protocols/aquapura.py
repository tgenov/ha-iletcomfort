"""AQUAPURA protocol adapter (sn8 171000AU)."""

from __future__ import annotations

from ..api import ITSSensors, ITSStatus
from ..c3_frame import build_c3_query
from ..model_profiles import ModelProfile, apply_profile_to_sensors


class AquapuraAdapter:
    """Adapter for AQS Energie AQUAPURA split HPWH units."""

    profile = ModelProfile.AQUAPURA

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return status

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        return apply_profile_to_sensors(ModelProfile.AQUAPURA, sensors, status)

    def describe_variant(self, status: ITSStatus | None) -> str:
        return "aquapura"
