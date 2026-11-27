"""KJRH-120L protocol dialect (sn8 17100003, pure-DHW and dual Zone-1 + DHW, #110)."""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
import math
from typing import Any

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..commands import (
    HeatPumpCommand,
    SetBoostCommand,
    SetDhwPowerCommand,
    SetDhwTargetCommand,
    SetEcoModeCommand,
    SetFastDhwCommand,
    SetHvacModeCommand,
    SetPowerCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
    SetZoneCurveCommand,
    SetZonePowerCommand,
    SetZoneRunModeCommand,
    SetZoneTargetCommand,
    ZoneRunModeIntent,
)
from .probes import OptionalProbe
from .profile import ModelProfile
from .read import ReadPlan, c3_sensors_read_plan, c3_status_read_plan
from .views import (
    KJRH120L_ZONE1_READ_ONLY,
    NO_ZONE2_VIEW,
    ClimateView,
    EntityCapabilities,
    EntitySupport,
    ViewContext,
    legacy_climate_view,
)
from .write import (
    C3_CONTROL_DISABLED,
    STATUS_NOT_REPORTED,
    WriteContext,
    WritePlan,
    WriteRefused,
)

KJRH120L_POWER_INDEX = 10
KJRH120L_MODE_OFF = 0
KJRH120L_MODE_ON = 1
KJRH120L_DHW_SETPOINT_INDEX = 15
KJRH120L_ZONE1_CAP_INDEX_A = 8
KJRH120L_ZONE1_CAP_INDEX_B = 9
KJRH120L_ZONE1_SETPOINT_INDEX = 12
KJRH120L_DHW_TEMP_MIN = 35
KJRH120L_DHW_TEMP_MAX = 60

# Zone 1 fields in the subtype 0x01 body (write field N lands at body[8 + N]).
KJRH120L_ZONE1_POWER_INDEX = 8  # field 0x00, 1 = on, 0 = off
KJRH120L_RUN_MODE_INDEX = 11  # field 0x03
KJRH120L_RUN_MODE_HEAT = 3
KJRH120L_RUN_MODE_COOL = 2
KJRH120L_ZONE1_HEAT_MAX_INDEX = 37
KJRH120L_ZONE1_HEAT_MIN_INDEX = 38
KJRH120L_ZONE1_COOL_MAX_INDEX = 39
KJRH120L_ZONE1_COOL_MIN_INDEX = 40

# Outdoor ambient temperature (T4) in status subtype 0x01: raw = T4 + 196.
KJRH120L_OUTDOOR_TEMP_INDEX = 57
KJRH120L_OUTDOOR_TEMP_OFFSET = 196
KJRH120L_OUTDOOR_TEMP_MIN = -50
KJRH120L_OUTDOOR_TEMP_MAX = 58

_KJRH120L_MODES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", 4: "Water Pump"}

# The KJRH-120L 0x02 sensors frame is mostly zero and no field in it has ever
# matched the official app (#131/#136/#142/#119), so nothing from it is
# published; one capture carried bytes that decoded as refrigerant 100 °C /
# plate HX 8 °C. decode_sensors is therefore an allowlist: it keeps only the
# raw body (for diagnostics) and fields validated from another frame. Today
# that is t4, taken from status body[57]; nothing from 0x02 is published.

# Confirmed short write commands (``00 <field> 01 <value> ff``).
KJRH120L_DHW_ON = "00020101ff"
KJRH120L_DHW_OFF = "00020100ff"
KJRH120L_TEMP_MIN = 20
KJRH120L_TEMP_MAX = 70
KJRH120L_ZONE1_POWER_ON = "00000101ff"
KJRH120L_ZONE1_POWER_OFF = "00000100ff"
_KJRH120L_CAPTURED_RUN_MODES = (KJRH120L_RUN_MODE_HEAT, KJRH120L_RUN_MODE_COOL)


@dataclasses.dataclass(frozen=True)
class Kjrh120lStatus:
    """KJRH-120L fields of the subtype 0x01 status body (``ITSStatus.kjrh120l``).

    Zone 1 fields are decoded on every KJRH-120L frame regardless of
    body[8:10] (app capture 2026-11-12/14). ``None`` means not decoded.
    """

    # Dual variant's separate DHW target (body[15], issue #5).
    dhw_setpoint: float | None = None
    zone1_power: bool | None = None  # body[8]
    dhw_power: bool | None = None  # body[10]
    run_mode: int | None = None  # body[11], heat=3, cool=2
    zone1_setpoint: float | None = None  # body[12]
    zone1_heat_max: int | None = None  # body[37]
    zone1_heat_min: int | None = None  # body[38]
    zone1_cool_max: int | None = None  # body[39]
    zone1_cool_min: int | None = None  # body[40]
    # Outdoor ambient temp (body[57] - 196; #148/#142/#136/#119).
    outdoor_temp: float | None = None


_NO_KJRH120L_STATUS = Kjrh120lStatus()


def kjrh120l_status(status: ITSStatus | None) -> Kjrh120lStatus:
    """Return the KJRH-120L fields of ``status`` (all ``None`` when absent)."""
    if status is None or status.kjrh120l is None:
        return _NO_KJRH120L_STATUS
    return status.kjrh120l


def kjrh120l_has_zone1(body: bytearray | bytes) -> bool:
    """Return whether a KJRH-120L frame carries the validated Zone-1 variant."""
    return (
        len(body) > KJRH120L_ZONE1_CAP_INDEX_B
        and body[KJRH120L_ZONE1_CAP_INDEX_A] == 0x01
        and body[KJRH120L_ZONE1_CAP_INDEX_B] == 0x01
    )


def decode_kjrh120l_status(body: bytearray | bytes) -> ITSStatus:
    """Decode a KJRH-120L (sn8 17100003) status frame into a clean ITSStatus."""
    status = ITSStatus()
    status.raw_body = bytes(body)
    body_len = len(body)

    status.error_code = 0
    # No compressor-running signal has been confirmed in this frame, so
    # comp_running stays None (unknown, not False; #231). Compressor
    # frequency, energy, run hours, EXV and pressures are not in this frame
    # either and keep the ITSStatus default None (unknown, not 0).
    status.comp_running = None

    fields: dict[str, Any] = {}
    if body_len > KJRH120L_POWER_INDEX:
        dhw_on = body[KJRH120L_POWER_INDEX] != 0
        fields["dhw_power"] = dhw_on
        status.mode = KJRH120L_MODE_ON if dhw_on else KJRH120L_MODE_OFF
        status.mode_name = _KJRH120L_MODES.get(
            status.mode, f"Unknown({status.mode})"
        )

    if body_len > KJRH120L_DHW_SETPOINT_INDEX:
        dhw_setpoint = body[KJRH120L_DHW_SETPOINT_INDEX]
        if kjrh120l_has_zone1(body):
            zone1_setpoint = body[KJRH120L_ZONE1_SETPOINT_INDEX]
            status.t5s_def = float(zone1_setpoint)
            status.set_temperature = zone1_setpoint
            fields["dhw_setpoint"] = float(dhw_setpoint)
        else:
            status.t5s_def = float(dhw_setpoint)
            status.set_temperature = dhw_setpoint

    fields.update(_decode_kjrh120l_zone1(body))
    decoded = Kjrh120lStatus(**fields)
    # None when nothing was decoded, so an empty sub-struct never differs
    # from an absent one.
    status.kjrh120l = None if decoded == _NO_KJRH120L_STATUS else decoded
    return status


def _decode_kjrh120l_zone1(body: bytearray | bytes) -> dict[str, Any]:
    """Return the Zone 1 ``Kjrh120lStatus`` fields of a KJRH-120L status body."""
    fields: dict[str, Any] = {}
    body_len = len(body)
    if body_len > KJRH120L_ZONE1_POWER_INDEX:
        fields["zone1_power"] = {0: False, 1: True}.get(
            body[KJRH120L_ZONE1_POWER_INDEX]
        )
    if body_len > KJRH120L_RUN_MODE_INDEX:
        fields["run_mode"] = body[KJRH120L_RUN_MODE_INDEX]
    if body_len > KJRH120L_ZONE1_SETPOINT_INDEX:
        fields["zone1_setpoint"] = float(body[KJRH120L_ZONE1_SETPOINT_INDEX])
    if body_len > KJRH120L_ZONE1_COOL_MIN_INDEX:
        fields["zone1_heat_max"] = body[KJRH120L_ZONE1_HEAT_MAX_INDEX]
        fields["zone1_heat_min"] = body[KJRH120L_ZONE1_HEAT_MIN_INDEX]
        fields["zone1_cool_max"] = body[KJRH120L_ZONE1_COOL_MAX_INDEX]
        fields["zone1_cool_min"] = body[KJRH120L_ZONE1_COOL_MIN_INDEX]
    if body_len > KJRH120L_OUTDOOR_TEMP_INDEX:
        raw_t4 = body[KJRH120L_OUTDOOR_TEMP_INDEX]
        if raw_t4 != 0xFF:
            t4 = raw_t4 - KJRH120L_OUTDOOR_TEMP_OFFSET
            if KJRH120L_OUTDOOR_TEMP_MIN <= t4 <= KJRH120L_OUTDOOR_TEMP_MAX:
                fields["outdoor_temp"] = float(t4)
    return fields


def build_kjrh120l_set_temperature(temp: int) -> str:
    """Return the KJRH-120L short DHW-setpoint write command for ``temp`` °C."""
    return "000701%02xff" % temp


def _kjrh120l_byte(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 0xFF:
        raise ValueError(f"KJRH-120L {what} must be an integer 0-255, got {value!r}")
    return value


def build_kjrh120l_zone1_setpoint(temp: int) -> str:
    """Return the Zone 1 setpoint write ``0004 01 <temp> ff``."""
    return "000401%02xff" % _kjrh120l_byte(temp, "Zone 1 setpoint")


def build_kjrh120l_run_mode(run_mode: int, zone1_setpoint: int) -> str:
    """Return the run mode write paired with the Zone 1 setpoint."""
    if isinstance(run_mode, bool) or run_mode not in _KJRH120L_CAPTURED_RUN_MODES:
        raise ValueError(f"KJRH-120L run mode must be 2 or 3, got {run_mode!r}")
    return ("000301%02xff" % run_mode) + build_kjrh120l_zone1_setpoint(zone1_setpoint)


def build_kjrh120l_zone1_power(on: bool) -> str:
    """Return the Zone 1 power write for ``on``."""
    if not isinstance(on, bool):
        raise ValueError(f"KJRH-120L Zone 1 power must be a bool, got {on!r}")
    return KJRH120L_ZONE1_POWER_ON if on else KJRH120L_ZONE1_POWER_OFF


def _kjrh120l_valid_range(low: object, high: object) -> tuple[int, int] | None:
    limits = []
    for value in (low, high):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or value != int(value):
            return None
        if not 0 <= value <= 0xFF:
            return None
        limits.append(int(value))
    if limits[0] >= limits[1]:
        return None
    return limits[0], limits[1]


def kjrh120l_zone1_range(
    status: ITSStatus, run_mode: int | None = None
) -> tuple[int, int] | None:
    """Return the device-reported Zone 1 ``(min, max)`` for ``run_mode``."""
    kjrh = kjrh120l_status(status)
    if run_mode is None:
        run_mode = kjrh.run_mode
    if run_mode == KJRH120L_RUN_MODE_HEAT:
        return _kjrh120l_valid_range(kjrh.zone1_heat_min, kjrh.zone1_heat_max)
    if run_mode == KJRH120L_RUN_MODE_COOL:
        return _kjrh120l_valid_range(kjrh.zone1_cool_min, kjrh.zone1_cool_max)
    return None


def kjrh120l_mode_change_setpoint(status: ITSStatus, run_mode: int) -> int:
    """Return the Zone 1 setpoint to pair with a switch to ``run_mode``."""
    if run_mode not in _KJRH120L_CAPTURED_RUN_MODES:
        raise ValueError(f"KJRH-120L run mode must be 2 or 3, got {run_mode!r}")
    limits = kjrh120l_zone1_range(status, run_mode)
    if limits is None:
        raise ValueError("The device has not reported a valid Zone 1 range for that mode")
    current = kjrh120l_status(status).zone1_setpoint
    if current is None or not math.isfinite(current):
        raise ValueError("The device has not reported a Zone 1 setpoint")
    low, high = limits
    return max(low, min(int(current), high))


_RUN_MODE_INTENTS: dict[str, int] = {
    "heat": KJRH120L_RUN_MODE_HEAT,
    "cool": KJRH120L_RUN_MODE_COOL,
}


def zone1_run_mode_setpoint(
    ctx: WriteContext, mode: ZoneRunModeIntent
) -> tuple[int, int]:
    """Return ``(run_mode, clamped Zone 1 setpoint)`` for a run mode switch.

    The app always pairs field 0x03 with the Zone 1 setpoint clamped into the
    destination mode's device-reported range. Raises ``WriteRefused`` when the
    status needed to clamp is missing or invalid.
    """
    if ctx.status is None:
        raise WriteRefused(STATUS_NOT_REPORTED)
    run_mode = _RUN_MODE_INTENTS[mode]
    try:
        setpoint = kjrh120l_mode_change_setpoint(ctx.status, run_mode)
    except ValueError as err:
        raise WriteRefused(f"Cannot switch to {mode}: {err}") from err
    return run_mode, setpoint


def kjrh120l_is_dual(status: ITSStatus | None, zone1_latched: bool) -> bool:
    """Return whether this is the dual Zone-1 + DHW variant (latched or seen now)."""
    if zone1_latched is True:
        return True
    return bool(status is not None and kjrh120l_has_zone1(status.raw_body))


def kjrh120l_climate_view(status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
    """Climate view: DHW control on pure-DHW units, a read-only Zone-1 card on dual.

    A latched dual unit keeps showing the Zone-1 setpoint (body[12]) even when
    Zone 1 is switched off, rather than flipping to the DHW setpoint (body[15]).
    """
    dual = kjrh120l_is_dual(status, ctx.kjrh120l_zone1_latched)
    zone1 = kjrh120l_status(status).zone1_setpoint if dual else None
    return legacy_climate_view(
        status,
        lambda _set_mode: (float(KJRH120L_TEMP_MIN), float(KJRH120L_TEMP_MAX)),
        read_only_reason=KJRH120L_ZONE1_READ_ONLY if dual else None,
        controls_hidden=dual,
        zone1_target=zone1,
    )


def kjrh120l_zone1_target(status: ITSStatus | None, value: float) -> int:
    """Validate a Zone 1 target against the device-reported range (whole °C).

    Raises ``WriteRefused`` when the unit has not reported a valid range for
    its current run mode, the value is not a whole degree, or it is outside
    that range. Nothing is clamped: the app never sends an out-of-range value.
    """
    limits = kjrh120l_zone1_range(status) if status is not None else None
    if limits is None:
        raise WriteRefused(
            "The heat pump has not reported a valid Zone 1 range for its "
            "current mode"
        )
    if not math.isfinite(value) or value != int(value):
        raise WriteRefused(f"Zone 1 target must be a whole degree, got {value}")
    low, high = limits
    if not low <= value <= high:
        raise WriteRefused(
            f"Zone 1 target {value:g} is outside the reported range {low}-{high}"
        )
    return int(value)


def plan_kjrh120l_write(command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
    """Plan a KJRH-120L write as one captured short command (no status read).

    Fields (``00 <field> 01 <value> ff``): 0x00 Zone 1 power, 0x02 DHW power,
    0x03 run mode (paired with the clamped 0x04 setpoint), 0x04 Zone 1
    setpoint (never 0x08, which always forced 23 °C), 0x07 DHW setpoint.
    Climate targets are clamped to the climate view's 20–70 °C, DHW targets to
    35–60 °C, and Zone 1 targets are validated against the reported range.
    """
    if isinstance(command, (SetPowerCommand, SetDhwPowerCommand)):
        on = command.on
        return WritePlan.static(
            KJRH120L_DHW_ON if on else KJRH120L_DHW_OFF, f"kjrh120l dhw_power={on}"
        )
    if isinstance(command, SetHvacModeCommand):
        on = command.mode != "off"
        return WritePlan.static(
            KJRH120L_DHW_ON if on else KJRH120L_DHW_OFF, f"kjrh120l dhw_power={on}"
        )
    if isinstance(command, (SetTargetTemperatureCommand, SetDhwTargetCommand)):
        if isinstance(command, SetTargetTemperatureCommand):
            clamped = kjrh120l_climate_view(ctx.status, ctx.view_context).clamp(
                command.temperature
            )
        else:
            clamped = max(
                float(KJRH120L_DHW_TEMP_MIN),
                min(command.temperature, float(KJRH120L_DHW_TEMP_MAX)),
            )
        temp = int(clamped)
        return WritePlan.static(
            build_kjrh120l_set_temperature(temp), f"kjrh120l dhw_target={temp}"
        )
    if isinstance(command, SetZonePowerCommand) and command.zone == 1:
        return WritePlan.static(
            build_kjrh120l_zone1_power(command.on), f"kjrh120l zone1_power={command.on}"
        )
    if isinstance(command, SetZoneTargetCommand) and command.zone == 1:
        temp = kjrh120l_zone1_target(ctx.status, command.temperature)
        return WritePlan.static(
            build_kjrh120l_zone1_setpoint(temp), f"kjrh120l zone1_target={temp}"
        )
    if isinstance(command, SetZoneRunModeCommand):
        run_mode, setpoint = zone1_run_mode_setpoint(ctx, command.mode)
        return WritePlan.static(
            build_kjrh120l_run_mode(run_mode, setpoint),
            f"kjrh120l run_mode={run_mode} zone1_target={setpoint}",
        )
    if isinstance(command, SetBoostCommand) or (
        isinstance(command, SetSilentModeCommand) and command.level is not None
    ):
        # No boost/silent write has been captured for this controller. Before
        # #113 these fell through to the DHW-OFF frame; refuse them instead.
        raise WriteRefused("Boost and silent mode are not supported on the KJRH-120L")
    if isinstance(
        command,
        (
            SetSilentModeCommand,
            SetEcoModeCommand,
            SetFastDhwCommand,
            SetZoneCurveCommand,
            SetZonePowerCommand,
            SetZoneTargetCommand,
        ),
    ):
        raise WriteRefused(C3_CONTROL_DISABLED)
    raise WriteRefused(f"Unsupported command intent: {command!r}")


class Kjrh120lAdapter:
    """Dialect adapter for KJRH-120L controllers (pure-DHW and dual Zone-1 + DHW)."""

    profile = ModelProfile.KJRH120L
    periodic_status_push = True
    accepts_energy_notify = False
    offers_c3_basic_control = False
    latches_zone1_capability = True

    def build_query(self, subtype: int) -> str:
        return "ffff%02x%02x%02xff" % (subtype, subtype, subtype)

    def plan_status_read(self) -> ReadPlan[ITSStatus]:
        return c3_status_read_plan(self)

    def plan_sensors_read(self) -> ReadPlan[ITSSensors]:
        return c3_sensors_read_plan(self)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return decode_kjrh120l_status(status.raw_body)

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        # Allowlist: start from "nothing reported" and keep only the raw
        # body plus the status-frame outdoor temperature (see module note).
        return ITSSensors(
            raw_body=sensors.raw_body,
            t4_temp=kjrh120l_status(status).outdoor_temp,
        )

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
        # body[8:10] follow the Zone 1 / Zone 2 power state (00 00 while
        # Zone 1 is off), so the coordinator latches one positive frame.
        return kjrh120l_has_zone1(status.raw_body)

    def describe_variant(self, status: ITSStatus | None) -> str:
        body = bytes(status.raw_body) if status is not None and status.raw_body else b""
        if not body:
            return "kjrh120l_unclassified"
        return "kjrh120l_dual" if kjrh120l_has_zone1(body) else "kjrh120l_pure_dhw"

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        return sensors.twin_temp if sensors is not None else None

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        return float(KJRH120L_TEMP_MIN), float(KJRH120L_TEMP_MAX)

    def is_kjrh120l_dual(
        self, status: ITSStatus | None, zone1_latched: bool
    ) -> bool:
        return kjrh120l_is_dual(status, zone1_latched)

    def climate_view(self, status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
        return kjrh120l_climate_view(status, ctx)

    def zone2_climate_view(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> ClimateView:
        return NO_ZONE2_VIEW

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return None

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        dhw_setpoint = kjrh120l_status(status).dhw_setpoint
        if dhw_setpoint is not None:
            return dhw_setpoint
        # When Zone 1 is off (body[8:10] == 00 00), decode_kjrh120l_status
        # leaves body[15] (the DHW setpoint) in t5s_def so pure-DHW units
        # keep their existing single-setpoint behavior.
        return status.t5s_def

    def entity_capabilities(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> EntityCapabilities:
        # No Boost or Silent write has been captured for this controller, so
        # those entities are not exposed (#192; the writes refuse since #229).
        latched = ctx.kjrh120l_zone1_latched
        return EntityCapabilities(
            kjrh120l_zone1=EntitySupport(
                exists=latched, possible=True, available=latched, writable=True
            ),
        )

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_kjrh120l_write(command, ctx)
