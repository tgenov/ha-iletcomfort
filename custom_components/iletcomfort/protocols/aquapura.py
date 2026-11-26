"""AQUAPURA protocol dialect (sn8 171000AU, AQS Energie split HPWH, #12, #110)."""

from __future__ import annotations

import dataclasses

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..c3_frame import build_c3_query
from .profile import ModelProfile
from ..commands import HeatPumpCommand
from .standard import default_climate_temp_bounds, plan_legacy_its_write
from .write import WriteContext, WritePlan


class AquapuraAdapter:
    """Dialect adapter for AQS Energie AQUAPURA split HPWH units.

    Standard status/sensor decode, except the tank temperature reads
    ``status.box_bottom_temp`` (status byte[17], offset-decoded) and is routed
    to ``th_temp`` ("DHW Tank Temperature") instead of ``twin_temp`` (which is
    null-filled to 0 on the #12 hardware variant). A second 171000AU hardware
    variant publishes live inlet/outlet values while ``th_temp`` is a zero
    placeholder, so ``climate_current_temperature`` keeps a nonzero ``th_temp``
    and otherwise falls back to ``twout_temp``, then ``twin_temp``.
    """

    profile = ModelProfile.AQUAPURA
    periodic_status_push = True

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return status

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        if status is not None and status.box_bottom_temp is not None:
            return dataclasses.replace(sensors, th_temp=status.box_bottom_temp)
        return sensors

    def describe_variant(self, status: ITSStatus | None) -> str:
        return "aquapura"

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        if sensors is None:
            return None
        if sensors.th_temp not in (None, 0.0):
            return sensors.th_temp
        if sensors.twout_temp is not None:
            return sensors.twout_temp
        return sensors.twin_temp

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

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        return None

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_legacy_its_write(command, ctx)
