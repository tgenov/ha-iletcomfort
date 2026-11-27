"""AQUAPURA protocol dialect (sn8 171000AU, AQS Energie split HPWH, #12, #110)."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..c3_frame import build_c3_query
from .probes import OptionalProbe
from .profile import ModelProfile
from .read import ReadPlan, c3_sensors_read_plan, c3_status_read_plan
from ..commands import HeatPumpCommand
from .standard import default_climate_temp_bounds, plan_legacy_its_write
from .views import (
    LEGACY_BOOST_MUTE,
    NO_ZONE2_VIEW,
    ClimateView,
    EntityCapabilities,
    ViewContext,
    legacy_climate_view,
)
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
    accepts_energy_notify = False
    offers_c3_basic_control = False
    latches_zone1_capability = False

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def plan_status_read(self) -> ReadPlan[ITSStatus]:
        return c3_status_read_plan(self)

    def plan_sensors_read(self) -> ReadPlan[ITSSensors]:
        return c3_sensors_read_plan(self)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return status

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        if status is not None and status.box_bottom_temp is not None:
            return dataclasses.replace(sensors, th_temp=status.box_bottom_temp)
        return sensors

    def sensors_placeholder(self, raw_0x02: bytes) -> bool | None:
        return None

    def optional_probes(
        self, status: ITSStatus, raw_frames: Mapping[int, bytes]
    ) -> tuple[OptionalProbe, ...]:
        return ()

    def apply_overlays(
        self,
        status: ITSStatus,
        sensors: ITSSensors | None,
        raw_frames: Mapping[int, bytes],
        *,
        retained_subtypes: bool,
        energy_notify: bytes | None,
    ) -> tuple[ITSStatus, ITSSensors | None]:
        return status, sensors

    def observed_zone1(self, status: ITSStatus) -> bool:
        return False

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

    def entity_capabilities(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> EntityCapabilities:
        return EntityCapabilities(boost=LEGACY_BOOST_MUTE, mute=LEGACY_BOOST_MUTE)

    def climate_view(self, status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
        return legacy_climate_view(status, default_climate_temp_bounds)

    def zone2_climate_view(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> ClimateView:
        return NO_ZONE2_VIEW

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_legacy_its_write(command, ctx)
