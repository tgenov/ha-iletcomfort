"""KJRH-120L protocol dialect (sn8 17100003, pure-DHW and dual Zone-1 + DHW, #110)."""

from __future__ import annotations

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
from .profile import ModelProfile
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

_KJRH120L_SUPPRESSED_TEMPS = {
    "t3_temp": None,
    "t4_temp": None,
    "t2_temp": None,
    "t2b_temp": None,
    "twin_temp": None,
    "twout_temp": None,
    "t1_temp": None,
}

# Confirmed short write commands (``00 <field> 01 <value> ff``).
KJRH120L_DHW_ON = "00020101ff"
KJRH120L_DHW_OFF = "00020100ff"
KJRH120L_TEMP_MIN = 20
KJRH120L_TEMP_MAX = 70
KJRH120L_ZONE1_POWER_ON = "00000101ff"
KJRH120L_ZONE1_POWER_OFF = "00000100ff"
_KJRH120L_CAPTURED_RUN_MODES = (KJRH120L_RUN_MODE_HEAT, KJRH120L_RUN_MODE_COOL)


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
    status.comp_running = False
    # Compressor frequency, energy, run hours, EXV and pressures are not in
    # this frame and keep the ITSStatus default None (unknown, not 0).

    if body_len > KJRH120L_POWER_INDEX:
        dhw_on = body[KJRH120L_POWER_INDEX] != 0
        status.kjrh120l_dhw_power = dhw_on
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
            status.kjrh120l_dhw_setpoint = float(dhw_setpoint)
        else:
            status.t5s_def = float(dhw_setpoint)
            status.set_temperature = dhw_setpoint

    _decode_kjrh120l_zone1(status, body)
    return status


def _decode_kjrh120l_zone1(status: ITSStatus, body: bytearray | bytes) -> None:
    """Fill the Zone 1 fields of ``status`` from a KJRH-120L status body."""
    body_len = len(body)
    if body_len > KJRH120L_ZONE1_POWER_INDEX:
        status.kjrh120l_zone1_power = {0: False, 1: True}.get(
            body[KJRH120L_ZONE1_POWER_INDEX]
        )
    if body_len > KJRH120L_RUN_MODE_INDEX:
        status.kjrh120l_run_mode = body[KJRH120L_RUN_MODE_INDEX]
    if body_len > KJRH120L_ZONE1_SETPOINT_INDEX:
        status.kjrh120l_zone1_setpoint = float(body[KJRH120L_ZONE1_SETPOINT_INDEX])
    if body_len > KJRH120L_ZONE1_COOL_MIN_INDEX:
        status.kjrh120l_zone1_heat_max = body[KJRH120L_ZONE1_HEAT_MAX_INDEX]
        status.kjrh120l_zone1_heat_min = body[KJRH120L_ZONE1_HEAT_MIN_INDEX]
        status.kjrh120l_zone1_cool_max = body[KJRH120L_ZONE1_COOL_MAX_INDEX]
        status.kjrh120l_zone1_cool_min = body[KJRH120L_ZONE1_COOL_MIN_INDEX]
    if body_len > KJRH120L_OUTDOOR_TEMP_INDEX:
        raw_t4 = body[KJRH120L_OUTDOOR_TEMP_INDEX]
        if raw_t4 != 0xFF:
            t4 = raw_t4 - KJRH120L_OUTDOOR_TEMP_OFFSET
            if KJRH120L_OUTDOOR_TEMP_MIN <= t4 <= KJRH120L_OUTDOOR_TEMP_MAX:
                status.kjrh120l_outdoor_temp = float(t4)


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
    if run_mode is None:
        run_mode = status.kjrh120l_run_mode
    if run_mode == KJRH120L_RUN_MODE_HEAT:
        return _kjrh120l_valid_range(
            status.kjrh120l_zone1_heat_min, status.kjrh120l_zone1_heat_max
        )
    if run_mode == KJRH120L_RUN_MODE_COOL:
        return _kjrh120l_valid_range(
            status.kjrh120l_zone1_cool_min, status.kjrh120l_zone1_cool_max
        )
    return None


def kjrh120l_mode_change_setpoint(status: ITSStatus, run_mode: int) -> int:
    """Return the Zone 1 setpoint to pair with a switch to ``run_mode``."""
    if run_mode not in _KJRH120L_CAPTURED_RUN_MODES:
        raise ValueError(f"KJRH-120L run mode must be 2 or 3, got {run_mode!r}")
    limits = kjrh120l_zone1_range(status, run_mode)
    if limits is None:
        raise ValueError("The device has not reported a valid Zone 1 range for that mode")
    current = status.kjrh120l_zone1_setpoint
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


def plan_kjrh120l_write(command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
    """Plan a KJRH-120L write as one captured short command (no status read).

    Fields (``00 <field> 01 <value> ff``): 0x00 Zone 1 power, 0x02 DHW power,
    0x03 run mode (paired with the clamped 0x04 setpoint), 0x04 Zone 1
    setpoint (never 0x08, which always forced 23 °C), 0x07 DHW setpoint. The
    climate/number entities clamp DHW temperatures before sending.
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
        temp = int(command.temperature)
        return WritePlan.static(
            build_kjrh120l_set_temperature(temp), f"kjrh120l dhw_target={temp}"
        )
    if isinstance(command, SetZonePowerCommand) and command.zone == 1:
        return WritePlan.static(
            build_kjrh120l_zone1_power(command.on), f"kjrh120l zone1_power={command.on}"
        )
    if isinstance(command, SetZoneTargetCommand) and command.zone == 1:
        temp = int(command.temperature)
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

    def build_query(self, subtype: int) -> str:
        return "ffff%02x%02x%02xff" % (subtype, subtype, subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return decode_kjrh120l_status(status.raw_body)

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        overrides: dict[str, Any] = dict(_KJRH120L_SUPPRESSED_TEMPS)
        if status is not None and status.kjrh120l_outdoor_temp is not None:
            overrides["t4_temp"] = status.kjrh120l_outdoor_temp
        return dataclasses.replace(sensors, **overrides)

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
        if zone1_latched is True:
            return True
        return bool(status is not None and kjrh120l_has_zone1(status.raw_body))

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return None

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        if status.kjrh120l_dhw_setpoint is not None:
            return status.kjrh120l_dhw_setpoint
        # When Zone 1 is off (body[8:10] == 00 00), decode_kjrh120l_status
        # leaves body[15] (the DHW setpoint) in t5s_def so pure-DHW units
        # keep their existing single-setpoint behavior.
        return status.t5s_def

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_kjrh120l_write(command, ctx)
