"""KJRH-120L protocol adapter (sn8 17100003)."""

from __future__ import annotations

from ..api import ITSSensors, ITSStatus
from ..model_profiles import (
    KJRH120L_SN8,
    ModelProfile,
    apply_profile_to_sensors,
    build_query_command,
    decode_kjrh120l_status,
    describe_profile_variant,
)


class Kjrh120lAdapter:
    """Adapter for KJRH-120L controllers (pure-DHW and dual Zone-1 + DHW)."""

    profile = ModelProfile.KJRH120L

    def build_query(self, subtype: int) -> str:
        return build_query_command(ModelProfile.KJRH120L, subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return decode_kjrh120l_status(status.raw_body)

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        return apply_profile_to_sensors(ModelProfile.KJRH120L, sensors, status)

    def describe_variant(self, status: ITSStatus | None) -> str:
        return describe_profile_variant(KJRH120L_SN8, status)
