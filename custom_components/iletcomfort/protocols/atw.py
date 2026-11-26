"""ATW protocol adapter (sn8 171H120F multi-OEM air-to-water family)."""

from __future__ import annotations

from ..api import ITSSensors, ITSStatus
from ..c3_frame import build_c3_query
from ..model_profiles import (
    ATW_SN8,
    ModelProfile,
    apply_profile_to_sensors,
    decode_atw_status,
    describe_profile_variant,
)


class AtwAdapter:
    """Adapter for 171H120F air-to-water heat pumps."""

    profile = ModelProfile.ATW

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return decode_atw_status(status.raw_body)

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        return apply_profile_to_sensors(ModelProfile.ATW, sensors, status)

    def describe_variant(self, status: ITSStatus | None) -> str:
        return describe_profile_variant(ATW_SN8, status)
