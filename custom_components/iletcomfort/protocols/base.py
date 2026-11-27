"""ProtocolAdapter interface and normalized HeatPumpState domain model (#110, #172, #192)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..commands import HeatPumpCommand
from .atw import atw_features
from .kjrh120l import (
    KJRH120L_RUN_MODE_COOL,
    KJRH120L_RUN_MODE_HEAT,
    kjrh120l_status,
    kjrh120l_zone1_range,
)
from .probes import OptionalProbe
from .profile import ModelProfile
from .views import ClimateView, EntityCapabilities, ViewContext, status_active_setpoint
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

    This is ``coordinator.data``. Entities read only the domain-named
    properties below, never the protocol-internal ``status`` / ``sensors``
    (``ITSStatus`` / ``ITSSensors``) wire fields. A property returns ``None``
    when the dialect did not decode the value (unknown), which is distinct
    from a decoded zero/off. Model-specific routing lives here (data-driven)
    or in the selected ``ProtocolAdapter`` (#110, #111).
    """

    profile: ModelProfile
    variant: str
    status: ITSStatus
    sensors: ITSSensors | None = None
    raw_frames: dict[int, bytes] = field(default_factory=dict)

    # -- frame evidence -------------------------------------------------------

    @property
    def has_atw_0x10(self) -> bool:
        """Return True when live ATW subtype 0x10 telemetry populated this state."""
        return 0x10 in self.raw_frames

    @property
    def _has_energy_frame(self) -> bool:
        """ATW 0x10 or MQTT 0x04 energy telemetry populated this state."""
        return self.has_atw_0x10 or 0x04 in self.raw_frames

    # -- temperatures ---------------------------------------------------------

    @property
    def water_inlet_temp(self) -> float | None:
        """Water inlet temperature in °C."""
        return self.sensors.twin_temp if self.sensors is not None else None

    @property
    def water_outlet_temp(self) -> float | None:
        """Water outlet temperature in °C."""
        return self.sensors.twout_temp if self.sensors is not None else None

    @property
    def dhw_tank_temp(self) -> float | None:
        """Domestic hot water tank temperature in °C (dialect-routed to th_temp)."""
        return self.sensors.th_temp if self.sensors is not None else None

    @property
    def outdoor_temp(self) -> float | None:
        """Outdoor ambient temperature in °C."""
        if self.sensors is not None and self.sensors.t4_temp is not None:
            return self.sensors.t4_temp
        return kjrh120l_status(self.status).outdoor_temp

    @property
    def condenser_temp(self) -> float | None:
        """Condenser (T3) temperature in °C."""
        return self.sensors.t3_temp if self.sensors is not None else None

    @property
    def evaporator_temp(self) -> float | None:
        """Evaporator (T2) temperature in °C."""
        return self.sensors.t2_temp if self.sensors is not None else None

    @property
    def refrigerant_temp(self) -> float | None:
        """Refrigerant (TF) temperature in °C."""
        return self.sensors.tf_temp if self.sensors is not None else None

    @property
    def plate_hx_temp(self) -> float | None:
        """Plate heat exchanger (TP) temperature in °C."""
        return self.sensors.tp_temp if self.sensors is not None else None

    @property
    def climate_current_temperature(self) -> float | None:
        """Return dialect-aware climate current temperature (#110, #192)."""
        from . import get_adapter

        return get_adapter(self.profile).climate_current_temperature(self.sensors)

    # -- compressor, energy, refrigerant circuit -------------------------------

    @property
    def compressor_running(self) -> bool | None:
        """Whether the compressor runs; None when the dialect has no signal."""
        return self.status.comp_running

    @property
    def compressor_frequency(self) -> int | None:
        """Compressor frequency in Hz; None when not decoded."""
        return self.status.comp_frq

    @property
    def total_energy_kwh(self) -> int | None:
        """Cumulative electrical energy in kWh; None (never 0) when not decoded."""
        return self.status.total_kwh

    @property
    def total_thermal_energy_kwh(self) -> int | None:
        """Cumulative thermal energy in kWh from ATW 0x10 / MQTT 0x04 only."""
        if not self._has_energy_frame:
            return None
        return self.status.total_thermal_kwh

    @property
    def compressor_run_hours(self) -> int | None:
        """Cumulative compressor run hours; None (never 0) when not decoded."""
        return self.status.comp_total_run_hours

    @property
    def high_pressure(self) -> int | None:
        """Refrigerant high pressure (raw device units); None when not decoded."""
        return self.status.pressure_h

    @property
    def low_pressure(self) -> int | None:
        """Refrigerant low pressure (raw device units); None when not decoded."""
        return self.status.pressure_l

    @property
    def exv_opening(self) -> int | None:
        """Electronic expansion valve opening (steps), from live ATW 0x10 only."""
        if not self.has_atw_0x10:
            return None
        return self.status.exv_drg

    @property
    def water_flow_m3h(self) -> float | None:
        """Water flow in m³/h, from live ATW 0x10 only."""
        if not self.has_atw_0x10 or self.sensors is None:
            return None
        flow = self.sensors.water_flow
        return round(flow / 100.0, 2) if flow is not None else None

    @property
    def dc_bus_voltage(self) -> int | None:
        """Inverter DC bus voltage in V, from live ATW 0x10 only."""
        if not self.has_atw_0x10 or self.sensors is None:
            return None
        return self.sensors.dc_voltage

    @property
    def dc_current(self) -> int | None:
        """Inverter DC current in A, from live ATW 0x10 only."""
        if not self.has_atw_0x10 or self.sensors is None:
            return None
        return self.sensors.dc_current

    @property
    def odu_voltage(self) -> int | None:
        """Outdoor unit supply voltage in V."""
        return self.sensors.odu_voltage if self.sensors is not None else None

    @property
    def odu_current(self) -> float | None:
        """Outdoor unit current in A."""
        return self.sensors.odu_current if self.sensors is not None else None

    @property
    def odu_firmware_version(self) -> str | None:
        """Outdoor unit firmware version string, None when not reported."""
        if self.sensors is None:
            return None
        return self.sensors.odu_version or None

    # -- faults and auxiliary heaters ------------------------------------------

    @property
    def error_code(self) -> int:
        """Vendor numeric error code (0 = no fault)."""
        return self.status.error_code

    @property
    def has_error(self) -> bool:
        """Whether the unit reports a fault."""
        return self.status.error_code != 0

    @property
    def ibh_running(self) -> bool:
        """Whether the internal backup heater runs."""
        return self.status.ibh_running

    # -- operating modes --------------------------------------------------------

    @property
    def operating_mode(self) -> int:
        """Status-frame operating mode (0 off, 1 heat, 2 cool, 3 auto, 4 water pump)."""
        return self.status.mode

    @property
    def active_setpoint(self) -> float:
        """Setpoint of the active mode in °C (falls back to the direct target byte)."""
        return status_active_setpoint(self.status)

    @property
    def boost_enabled(self) -> bool:
        """Whether legacy boost (ctrl_flag 2) is active."""
        return self.sensors is not None and self.sensors.ctrl_flag == 2

    @property
    def reported_silent_level(self) -> int | None:
        """Silent level from a dedicated silent frame (ATW 0x05), else None.

        0 = off, 1 = Level 1, 2 = Level 2 (Super Silent).
        """
        return atw_features(self.status).silent_level

    @property
    def silent_level(self) -> int:
        """Effective silent level: 0 = off, 1 = Level 1, 2 = Level 2.

        ATW 0x05 level first, then the C3 basic binary Silent flag (Level 1),
        then the legacy ctrl_flag/mute_level pair.
        """
        silent_level = atw_features(self.status).silent_level
        if silent_level is not None:
            return silent_level
        basic = self.atw_c3_basic
        if basic is not None:
            return 1 if basic.silent_mode else 0
        if self.sensors is None or self.sensors.ctrl_flag != 1:
            return 0
        # mute_level: 0 = Level 1, 1 = Level 2
        return 2 if self.sensors.mute_level == 1 else 1

    @property
    def silent_enabled(self) -> bool | None:
        """Whether silent mode is on (ATW 0x05, else C3 basic flag, else legacy)."""
        silent_mode = atw_features(self.status).silent_mode
        if silent_mode is not None:
            return silent_mode
        basic = self.atw_c3_basic
        if basic is not None:
            return basic.silent_mode
        if self.sensors is None:
            return None
        return self.sensors.ctrl_flag == 1

    @property
    def eco_enabled(self) -> bool | None:
        """Whether ECO is on (ATW 0x07/0x05, else C3 basic flag); None if unknown."""
        eco_mode = atw_features(self.status).eco_mode
        if eco_mode is not None:
            return eco_mode
        basic = self.atw_c3_basic
        return basic.eco_mode if basic is not None else None

    # -- zones and domestic hot water ---------------------------------------------

    @property
    def zone1_power(self) -> bool | None:
        """Whether Zone 1 is switched on; None when the dialect does not report it."""
        zone1_power = kjrh120l_status(self.status).zone1_power
        if zone1_power is not None:
            return zone1_power
        basic = self.atw_c3_basic
        return basic.zone1_power if basic is not None else None

    @property
    def zone1_run_mode(self) -> str | None:
        """Zone 1 run mode ``"heat"`` / ``"cool"``; None for other or unknown codes."""
        run_mode = kjrh120l_status(self.status).run_mode
        if run_mode == KJRH120L_RUN_MODE_HEAT:
            return "heat"
        if run_mode == KJRH120L_RUN_MODE_COOL:
            return "cool"
        return None

    @property
    def zone1_target_temp(self) -> float | None:
        """Zone 1 target in °C (KJRH-120L body[12], else the C3 basic target)."""
        zone1_setpoint = kjrh120l_status(self.status).zone1_setpoint
        if zone1_setpoint is not None:
            return zone1_setpoint
        basic = self.atw_c3_basic
        if basic is None:
            return None
        return float(basic.zone1_target) if basic.zone1_water_temp else basic.room_target

    @property
    def zone1_water_target(self) -> float | None:
        """C3 basic Zone 1 water target (body[6]) in °C, whatever the control mode."""
        basic = self.atw_c3_basic
        return float(basic.zone1_target) if basic is not None else None

    @property
    def room_target(self) -> float | None:
        """C3 basic Zone 1 room target (body[9] / 2) in °C, whatever the control mode."""
        basic = self.atw_c3_basic
        return basic.room_target if basic is not None else None

    @property
    def zone1_temp_mode(self) -> str | None:
        """C3 basic Zone 1 control field: ``"water"`` or ``"room"``; None if not C3."""
        basic = self.atw_c3_basic
        if basic is None:
            return None
        return "water" if basic.zone1_water_temp else "room"

    @property
    def zone2_water_target(self) -> float | None:
        """C3 basic Zone 2 water target in °C; None when not C3 basic."""
        basic = self.atw_c3_basic
        return float(basic.zone2_target) if basic is not None else None

    @property
    def zone1_target_range(self) -> tuple[int, int] | None:
        """Device-reported Zone 1 ``(min, max)`` for the current run mode, if valid."""
        return kjrh120l_zone1_range(self.status)

    @property
    def zone1_curve_enabled(self) -> bool | None:
        """Whether the Zone 1 weather curve is on (C3 basic)."""
        basic = self.atw_c3_basic
        return basic.zone1_curve if basic is not None else None

    @property
    def zone2_curve_enabled(self) -> bool | None:
        """Whether the Zone 2 weather curve is on (C3 basic)."""
        basic = self.atw_c3_basic
        return basic.zone2_curve if basic is not None else None

    @property
    def dhw_power(self) -> bool | None:
        """Whether DHW heating is switched on; None when not reported."""
        dhw_power = kjrh120l_status(self.status).dhw_power
        if dhw_power is not None:
            return dhw_power
        basic = self.atw_c3_basic
        return basic.dhw_power if basic is not None else None

    @property
    def fast_dhw_enabled(self) -> bool | None:
        """Whether Fast DHW is on (C3 basic)."""
        basic = self.atw_c3_basic
        return basic.fast_dhw if basic is not None else None

    @property
    def dhw_target_temp(self) -> float | None:
        """DHW target in °C (dialect-routed); None when not reported."""
        from . import get_adapter

        return get_adapter(self.profile).dhw_target_temp(self.status)

    @property
    def dhw_target_range(self) -> tuple[float, float] | None:
        """Device-reported DHW ``(min, max)``, ignoring zeroed or inverted limits."""
        from .atw import atw_dhw_target_range

        return atw_dhw_target_range(self.atw_c3_basic)

    # -- dialect helpers (climate regime logic, #192) ---------------------------

    @property
    def atw_c3_basic(self) -> C3BasicStatus | None:
        """Return the C3 basic status when supported by this dialect, else None."""
        from . import get_adapter

        return get_adapter(self.profile).c3_basic(self.status)

    def is_kjrh120l_dual(self, zone1_latched: bool) -> bool:
        """Return whether this unit is the dual Zone-1 + DHW KJRH-120L variant."""
        from . import get_adapter

        return get_adapter(self.profile).is_kjrh120l_dual(self.status, zone1_latched)

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
    # Whether an unsolicited MQTT 0x04 energy notification carries values this
    # dialect overlays (``apply_overlays(energy_notify=...)``).
    accepts_energy_notify: bool
    # Whether the options flow offers the opt-in C3 basic control.
    offers_c3_basic_control: bool
    # Whether a Zone-1 observation is latched in the config entry (once seen,
    # kept across Zone-1-off frames and restarts) and honoured afterwards.
    latches_zone1_capability: bool

    def build_query(self, subtype: int) -> str:
        """Build the wire hex query command for ``subtype``."""

    def read_status(self, send: Callable[[str], str]) -> ITSStatus | None:
        """Run a dialect-owned status read over ``send``; None → subtype 0x01 query.

        For controllers that do not answer the C3 subtype queries at all
        (KJR-86T3 table reads, #126). The returned status is already
        dialect-decoded.
        """

    def read_sensors(self, send: Callable[[str], str]) -> ITSSensors | None:
        """Run a dialect-owned sensors read over ``send``; None → subtype 0x02 query."""

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        """Return the profile-canonical ITSStatus for ``status``."""

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        """Return the profile-canonical ITSSensors for ``sensors``."""

    def describe_variant(self, status: ITSStatus | None) -> str:
        """Return the structural variant label for diagnostics."""

    def sensors_placeholder(self, raw_0x02: bytes) -> bool | None:
        """Whether a 0x02 frame is a non-telemetry placeholder; None if not detected."""

    def observed_zone1(self, status: ITSStatus) -> bool:
        """Whether ``status`` shows a Zone 1 worth latching (see latches_zone1_capability)."""

    def optional_probes(
        self, status: ITSStatus, raw_frames: Mapping[int, bytes]
    ) -> tuple[OptionalProbe, ...]:
        """Optional subtypes to query this poll, in order (pure, #110).

        ``status`` is this poll's dialect-decoded status and ``raw_frames``
        the 0x01/0x02 frames collected so far.
        """

    def apply_overlays(
        self,
        status: ITSStatus,
        sensors: ITSSensors | None,
        raw_frames: Mapping[int, bytes],
        *,
        retained_subtypes: bool,
        energy_notify: bytes | None,
    ) -> tuple[ITSStatus, ITSSensors | None]:
        """Overlay this dialect's optional subtype frames (see ``build_snapshot``)."""

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

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        """Return the DHW target in °C as this dialect reports it, else None."""

    def entity_capabilities(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> EntityCapabilities:
        """Project which optional entities exist / may appear (pure, #192)."""

    def climate_view(self, status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
        """Project the main (Zone 1 / DHW) climate entity (pure, #192)."""

    def zone2_climate_view(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> ClimateView:
        """Project the Zone-2 climate entity (pure, #192)."""

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        """Plan ``command`` as a wire write for this dialect (pure, no I/O, #113).

        Returns a static ``WritePlan`` or a read-modify-write plan naming the
        fresh query to run immediately before encoding. Raises ``WriteRefused``
        (surfaced verbatim) for HA-side gating refusals, and ``ApiError`` /
        ``ValueError`` when the command is outside this dialect's validated
        write envelope.
        """
