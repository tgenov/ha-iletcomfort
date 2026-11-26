"""ATW protocol dialect (sn8 171H120F multi-OEM air-to-water family, #110).

All hardware-captured 171H120F status frames (25B & 27B) follow Midea's
C3BasicBody layout (see ``c3_basic.py``):
  byte[1]  flags: bit 0 (0x01) is Zone-1 power (Heat/Cool when set, Off when clear).
  byte[2]  capabilities: bit 4 (0x10) = zone1_water_temp, selecting byte[6]
           (direct water °C) vs byte[9]/2 (0.5 °C room setpoint).
  byte[4]  mode: 2 = Cool, 3 = Heat (1 = Auto fallback).
  byte[8]  DHW setpoint in °C (direct value).
  byte[22] DHW tank current temp in °C (direct value, routed to th_temp).
  byte[23] error_code (byte[24..] are flags/MSB bytes, not an error code).

``comp_running`` / ``comp_frq`` / cumulative counters / pressures are None in
0x01 and overlaid from subtype 0x10 / 0x0A / MQTT 0x04 when supported.
"""

from __future__ import annotations

import dataclasses
from typing import Any

from ..api import (
    MODE_HEAT,
    MODE_OFF,
    STATUS_MODE_COOL,
    STATUS_MODE_HEAT,
    STATUS_MODE_OFF,
    ITSSensors,
    ITSStatus,
)
from ..c3_basic import (
    BODY_BASIC,
    BODY_ECO,
    BODY_SILENCE,
    C3BasicStatus,
    apply_basic_changes,
    build_basic_set,
    build_feature_set,
    decode_c3_basic,
)
from ..c3_frame import build_c3_query
from ..cloud import ApiError
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
)
from .kjrh120l import zone1_run_mode_setpoint
from .profile import ModelProfile, resolve_profile
from .standard import default_climate_temp_bounds
from .write import (
    C3_CONTROL_DISABLED,
    KJRH120L_ONLY_ZONE1,
    FreshRead,
    WriteContext,
    WritePlan,
    WriteRefused,
)

ATW_LEGACY_WRITE_REFUSED = (
    "Legacy ITS commands are not supported on ATW (171H120F) units; "
    "use the C3 basic entities (with C3 basic control enabled in options)"
)

ATW_DHW_SETPOINT_INDEX = 8
ATW_ZONE1_SETPOINT_X2_INDEX = 9
ATW_DHW_TANK_TEMP_INDEX = 22
ATW_FLAGS_INDEX = 1
ATW_SPACE_HEAT_DEMAND_BIT = 0x01
ATW_GALMET_ZONE1_SETPOINT_INDEX = 6

# Galmet Prima 8GT (issue #38) returned this exact 75-byte subtype-0x02
# template while the official app displayed live Zone-1 water and outdoor
# values. It has no telemetry: after the subtype, it consists solely of 50
# zeroes and twelve ``03,1e`` pairs.
_ATW_PLACEHOLDER_SENSORS_BODY = (
    b"\x02\x00"
    + b"\x03\x1e\x00\x00\x00\x00" * 6
    + b"\x00"
    + b"\x03\x1e\x00\x00\x00\x00" * 6
)
_ATW_PLACEHOLDER_SENSOR_FIELDS = {
    "tf_temp": None,
    "tp_temp": None,
    "t3_temp": None,
    "t4_temp": None,
    "t2_temp": None,
    "t2b_temp": None,
    "twin_temp": None,
    "twout_temp": None,
    "t1_temp": None,
    "odu_current": None,
    "odu_voltage": None,
}
_ATW_TEMPLATE_GROUP_LEN = 6
_ATW_TEMPLATE_MIN_GROUPS = 6
_ATW_ACTIVE_SCHEDULE_LEN = 2 + _ATW_TEMPLATE_MIN_GROUPS * _ATW_TEMPLATE_GROUP_LEN
_ATW_TEMPLATE_MODE_TAGS = (0x02, 0x03)
_ATW_ACTIVE_SCHEDULE_TAGS = (0x02, 0x03, 0x05)

# ATW subtype 0x10 ("unit parameters") live telemetry (issues #120, #139, #118)
ATW_UNIT_PARAMS_SUBTYPE = 0x10
ATW_UNIT_PARAMS_MIN_LEN = 65
ATW_UNIT_PARAMS_ENERGY_MIN_LEN = 71
ATW_UNIT_PARAMS_THERMAL_ENERGY_MIN_LEN = 75
_ATW_UNIT_SENSOR_DISCONNECTED = 0x7F

# ATW subtype 0x0A ("HMI parameters") compressor run hours (issues #120 / #139)
ATW_HMI_PARAMS_SUBTYPE = 0x0A
ATW_HMI_PARAMS_MIN_LEN = 6

# ATW subtypes 0x05 (Silent/Super-Silent) & 0x07 (ECO) (issues #139 / #188)
ATW_SILENT_SUBTYPE = 0x05
ATW_ECO_SUBTYPE = 0x07
ATW_SILENT_MIN_LEN = 2
ATW_ECO_MIN_LEN = 2
ATW_SILENT_FULL_LEN = 7

# ATW MQTT unsolicited subtype 0x04 ("C3EnergyBody", issue #129)
ATW_ENERGY_NOTIFY_SUBTYPE = 0x04
ATW_ENERGY_NOTIFY_MIN_LEN = 11


def _is_27_byte_atw_status(body: bytearray | bytes) -> bool:
    """Return whether ``body`` has the hardware-validated 27-byte ATW layout."""
    return len(body) == 27 and body[0] == 0x01 and body[3:6] == b"\xa0\x03\x03"


def _is_rotenso_atw_status(body: bytearray | bytes) -> bool:
    """Return whether ``body`` has the 25-byte Rotenso [2]=0x15 signature (#124)."""
    return len(body) == 25 and body[0] == 0x01 and body[2] == 0x15


def decode_atw_status(body: bytearray | bytes) -> ITSStatus:
    """Decode an ATW (sn8 171H120F) status frame into an ITSStatus."""
    status = ITSStatus()
    status.raw_body = bytes(body)
    # No 0x01 layout carries a confirmed compressor running signal; 0x10
    # overlays it when supported. Unknown (None), never False.
    status.comp_running = None
    # ATW subtype 0x01 never carries cumulative energy, compressor run hours,
    # compressor frequency, EXV or refrigerant pressures: they keep the
    # ITSStatus default None until 0x10 / 0x0A / MQTT 0x04 overlay them.
    body_len = len(body)

    if body_len <= ATW_DHW_TANK_TEMP_INDEX:
        return status

    flags = body[ATW_FLAGS_INDEX]
    status.status_flags_raw = flags
    status.pump_system = bool(flags & ATW_SPACE_HEAT_DEMAND_BIT)

    status.set_temperature = body[ATW_DHW_SETPOINT_INDEX]
    status.box_bottom_temp = float(body[ATW_DHW_TANK_TEMP_INDEX])
    status.error_code = 0

    basic = decode_c3_basic(body)
    status.c3_basic = basic
    if basic is not None:
        if basic.zone1_power:
            cooling = basic.is_cooling_mode
            status.mode = STATUS_MODE_COOL if cooling else STATUS_MODE_HEAT
            status.mode_name = "Cool" if cooling else "Heat"
        else:
            status.mode = STATUS_MODE_OFF
            status.mode_name = "Off"
        status.t5s_def = (
            float(basic.zone1_target)
            if basic.zone1_water_temp
            else basic.room_target
        )
        status.comp_running = None
        status.comp_frq = None
        return status

    is_27_byte = _is_27_byte_atw_status(body)
    if is_27_byte:
        status.mode = MODE_HEAT if status.pump_system else MODE_OFF
        status.mode_name = "Heat" if status.pump_system else "Off"
        status.t5s_def = float(body[ATW_GALMET_ZONE1_SETPOINT_INDEX])
        status.comp_running = None
        status.comp_frq = None
    else:
        status.t5s_def = body[ATW_ZONE1_SETPOINT_X2_INDEX] / 2
        # No confirmed running signal in this layout: unknown, never False.
        status.comp_running = None

    return status


def _atw_sensors_match_template_shape(raw_body: bytes) -> bool:
    if len(raw_body) < 4 or raw_body[:2] != b"\x02\x00":
        return False
    mode_tag = raw_body[2]
    if mode_tag not in _ATW_TEMPLATE_MODE_TAGS:
        return False
    constant = raw_body[3]
    groups = 0
    after_separator = False
    i = 2
    while i < len(raw_body):
        if raw_body[i] == 0x00 and groups and not after_separator:
            after_separator = True
            i += 1
            continue
        group = raw_body[i : i + _ATW_TEMPLATE_GROUP_LEN]
        if (
            len(group) != _ATW_TEMPLATE_GROUP_LEN
            or group[0] != mode_tag
            or group[1] != constant
        ):
            return False
        groups += 1
        after_separator = False
        i += _ATW_TEMPLATE_GROUP_LEN
    return groups >= _ATW_TEMPLATE_MIN_GROUPS and not after_separator


def _atw_sensors_match_active_schedule_shape(raw_body: bytes) -> bool:
    """Match 38-byte ATW 0x02 schedule frames with active 02/03/05 timer slots."""
    if (
        len(raw_body) != _ATW_ACTIVE_SCHEDULE_LEN
        or raw_body[0] != 0x02
        or raw_body[1] > 0x3F
    ):
        return False
    idle_headers: set[tuple[int, int]] = set()
    has_active_slot = False
    for offset in range(2, len(raw_body), _ATW_TEMPLATE_GROUP_LEN):
        tag, val, start_h, start_m, end_h, end_m = raw_body[
            offset : offset + _ATW_TEMPLATE_GROUP_LEN
        ]
        if tag not in _ATW_ACTIVE_SCHEDULE_TAGS or not (0 < val <= 0x50):
            return False
        if not (
            start_h <= 23 and start_m <= 59 and end_h <= 23 and end_m <= 59
        ):
            return False
        if (start_h, start_m, end_h, end_m) == (0, 0, 0, 0):
            if tag not in _ATW_TEMPLATE_MODE_TAGS:
                return False
            idle_headers.add((tag, val))
            if len(idle_headers) > 2:
                return False
        else:
            has_active_slot = True
    return has_active_slot


def atw_sensors_are_placeholder(raw_body: bytes) -> bool:
    """Return whether an ATW sensor frame is a known non-telemetry template."""
    raw = bytes(raw_body)
    return (
        raw == _ATW_PLACEHOLDER_SENSORS_BODY
        or _atw_sensors_match_template_shape(raw)
        or _atw_sensors_match_active_schedule_shape(raw)
    )


_ENERGY_COUNTER_SENTINEL = 0xFFFFFFFF


def valid_energy_counter(value: int) -> bool:
    """Return whether a 32-bit cumulative energy counter is a real reading.

    Shared by ATW 0x10 and MQTT 0x04 (#129, #232): ``0`` (uninitialised) and
    ``0xFFFFFFFF`` (absent) are sentinels. Publishing either to a
    TOTAL_INCREASING sensor would record a meter reset or a ~4.29e9 kWh spike,
    so callers skip the field: it keeps whatever the base status carries
    (``None``/unknown after a fresh poll or 0x01 push decode, the last valid
    value when overlaying an existing snapshot). Unknown is safe: HA
    statistics ignore it and the next valid reading is not a reset.
    """
    return 0 < value < _ENERGY_COUNTER_SENTINEL


def _atw_unit_s8(raw_byte: int) -> float | None:
    """Decode a signed 8-bit °C byte from ATW subtype 0x10/0x04 (0x7F = absent)."""
    if raw_byte == _ATW_UNIT_SENSOR_DISCONNECTED:
        return None
    return float(raw_byte - 256 if raw_byte > 127 else raw_byte)


def apply_atw_unit_params(
    unit_body: bytes | bytearray,
    status: ITSStatus,
    sensors: ITSSensors,
) -> tuple[ITSStatus, ITSSensors]:
    """Overlay hardware-validated ATW subtype 0x10 telemetry onto status/sensors."""
    body = bytes(unit_body)
    if len(body) < ATW_UNIT_PARAMS_MIN_LEN or body[0] != ATW_UNIT_PARAMS_SUBTYPE:
        return status, sensors

    comp_frq = body[1]
    status_updates: dict[str, Any] = {
        "comp_frq": comp_frq,
        "comp_running": comp_frq > 0,
        "exv_drg": (body[20] << 8) | body[21],
        "pressure_h": (body[43] << 8) | body[44],
    }
    # Sentinel counters (0 / 0xFFFFFFFF) keep the previous value (#232).
    if len(body) >= ATW_UNIT_PARAMS_ENERGY_MIN_LEN:
        total_kwh = int.from_bytes(body[67:71], "big")
        if valid_energy_counter(total_kwh):
            status_updates["total_kwh"] = total_kwh
    if len(body) >= ATW_UNIT_PARAMS_THERMAL_ENERGY_MIN_LEN:
        total_thermal = int.from_bytes(body[71:75], "big")
        if valid_energy_counter(total_thermal):
            status_updates["total_thermal_kwh"] = total_thermal

    sensor_updates: dict[str, Any] = {
        "t3_temp": _atw_unit_s8(body[7]),
        "t4_temp": _atw_unit_s8(body[8]),
        "tp_temp": _atw_unit_s8(body[9]),
        "twin_temp": _atw_unit_s8(body[10]),
        "twout_temp": _atw_unit_s8(body[11]),
        "odu_voltage": (body[18] << 8) | body[19],
        "t1_temp": _atw_unit_s8(body[34]),
        "t2_temp": _atw_unit_s8(body[36]),
        "t2b_temp": _atw_unit_s8(body[37]),
        "dc_current": body[50],
        "dc_voltage": body[51] * 10,
        "tf_temp": _atw_unit_s8(body[52]),
        "water_flow": (body[55] << 8) | body[56],
        "raw_body": body,
    }
    if sensors.th_temp is None:
        tank = _atw_unit_s8(body[38])
        if tank is not None:
            sensor_updates["th_temp"] = tank

    return (
        dataclasses.replace(status, **status_updates),
        dataclasses.replace(sensors, **sensor_updates),
    )


def apply_atw_hmi_params(
    hmi_body: bytes | bytearray,
    status: ITSStatus,
) -> ITSStatus:
    """Overlay verified ATW subtype 0x0A compressor run hours onto status."""
    body = bytes(hmi_body)
    if len(body) < ATW_HMI_PARAMS_MIN_LEN or body[0] != ATW_HMI_PARAMS_SUBTYPE:
        return status
    comp_total_run_hours = (body[4] << 8) | body[5]
    return dataclasses.replace(status, comp_total_run_hours=comp_total_run_hours)


def apply_atw_silent(
    silent_body: bytes | bytearray,
    status: ITSStatus,
) -> ITSStatus:
    """Overlay ATW subtype 0x05 Silent/Super-Silent (or short ECO fallback) onto status."""
    body = bytes(silent_body)
    if len(body) < ATW_SILENT_MIN_LEN or body[0] != ATW_SILENT_SUBTYPE:
        return status
    flags = body[1]
    if len(body) < ATW_SILENT_FULL_LEN:
        return dataclasses.replace(
            status,
            atw_eco_mode=bool(flags & 0x01),
            atw_eco_timer=bool(flags & 0x02),
        )
    silent_on = bool(flags & 0x01)
    super_selected = bool(flags & 0x08)
    silent_level = 0 if not silent_on else (2 if super_selected else 1)
    return dataclasses.replace(
        status,
        atw_silent_mode=silent_on,
        atw_silent_level=silent_level,
        atw_silent_super_selected=super_selected,
    )


def apply_atw_eco(
    eco_body: bytes | bytearray,
    status: ITSStatus,
) -> ITSStatus:
    """Overlay ATW subtype 0x07 (or <=6-byte 0x05) ECO state onto status."""
    body = bytes(eco_body)
    if len(body) < ATW_ECO_MIN_LEN or body[0] not in (
        ATW_ECO_SUBTYPE,
        ATW_SILENT_SUBTYPE,
    ):
        return status
    if body[0] == ATW_SILENT_SUBTYPE and len(body) >= ATW_SILENT_FULL_LEN:
        return status
    flags = body[1]
    return dataclasses.replace(
        status,
        atw_eco_mode=bool(flags & 0x01),
        atw_eco_timer=bool(flags & 0x02),
    )


def apply_atw_energy_notify(
    energy_body: bytes | bytearray,
    status: ITSStatus,
    sensors: ITSSensors | None,
) -> tuple[ITSStatus, ITSSensors | None]:
    """Overlay verified ATW MQTT 0x04 (C3EnergyBody) fields onto status/sensors."""
    body = bytes(energy_body)
    if len(body) < ATW_ENERGY_NOTIFY_MIN_LEN or body[0] != ATW_ENERGY_NOTIFY_SUBTYPE:
        return status, sensors

    status_updates: dict[str, Any] = {}
    total_kwh = (body[2] << 24) | (body[3] << 16) | (body[4] << 8) | body[5]
    if valid_energy_counter(total_kwh):
        status_updates["total_kwh"] = total_kwh
    total_thermal = (body[6] << 24) | (body[7] << 16) | (body[8] << 8) | body[9]
    if valid_energy_counter(total_thermal):
        status_updates["total_thermal_kwh"] = total_thermal
    if status_updates:
        status = dataclasses.replace(status, **status_updates)

    outdoor = _atw_unit_s8(body[10])
    if sensors is not None and outdoor is not None:
        sensors = dataclasses.replace(sensors, t4_temp=outdoor)

    return status, sensors


def atw_c3_basic(sn8: str | None, status: ITSStatus | None) -> C3BasicStatus | None:
    """Return the decoded C3 basic status for an ATW unit, else None."""
    if status is None or resolve_profile(sn8) is not ModelProfile.ATW:
        return None
    return status.c3_basic


def _c3_basic_plan(**changes: Any) -> WritePlan:
    """Plan a C3 basic SET: fresh 0x01 read → validated changes → full write-back."""

    def _encode(body: bytes) -> str:
        current = decode_c3_basic(body)
        if current is None:
            raise ApiError(
                "Status frame is not a recognised C3 basic layout; refusing "
                "to send a control command"
            )
        return build_basic_set(apply_basic_changes(current, **changes))

    return WritePlan.read_modify_write(
        FreshRead(BODY_BASIC, build_c3_query(BODY_BASIC)),
        _encode,
        description=f"c3_basic {changes}",
    )


def _c3_feature_plan(
    body_type: int, on: bool, silent_level: int | None = None
) -> WritePlan:
    """Plan a Silent (0x05) / ECO (0x07) SET that copies the fresh timer schedule.

    ``build_feature_set`` refuses a reply too short to carry the schedule, so
    a short reply aborts the write before any SET is sent.
    """

    def _encode(body: bytes) -> str:
        return build_feature_set(body, on, silent_level=silent_level)

    return WritePlan.read_modify_write(
        FreshRead(body_type, build_c3_query(body_type)),
        _encode,
        description=f"c3_feature 0x{body_type:02x} on={on} level={silent_level}",
    )


def plan_atw_write(command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
    """Plan an ATW (171H120F) write via the standard Midea C3 SET.

    Control is opt-in (``CONF_C3_BASIC_CONTROL``) and needs a status frame that
    matched the C3 basic layout; otherwise C3 intents are refused. ATW units
    never receive the legacy 62-byte ITS frame. Field-level envelope checks
    (Heat-only zone writes, water vs room control, ``cap_dhw``, ranges) run in
    ``apply_basic_changes`` against the fresh status read inside the write
    transaction.
    """
    decoded = ctx.status.c3_basic if ctx.status is not None else None
    basic = (
        decoded
        if ctx.c3_basic_control and isinstance(decoded, C3BasicStatus)
        else None
    )

    def _require_control() -> C3BasicStatus:
        if basic is None:
            raise WriteRefused(C3_CONTROL_DISABLED)
        return basic

    if isinstance(command, SetPowerCommand):
        if basic is None:
            raise ApiError(ATW_LEGACY_WRITE_REFUSED)
        return _c3_basic_plan(zone1_power=command.on)
    if isinstance(command, SetHvacModeCommand):
        if basic is None:
            raise ApiError(ATW_LEGACY_WRITE_REFUSED)
        if command.mode not in ("off", "heat"):
            raise WriteRefused(
                f"{command.mode} is not supported; only Heat and Off can be set"
            )
        return _c3_basic_plan(zone1_power=command.mode == "heat")
    if isinstance(command, SetTargetTemperatureCommand):
        if basic is None:
            raise ApiError(ATW_LEGACY_WRITE_REFUSED)
        if basic.zone1_water_temp:
            return _c3_basic_plan(zone1_water_target=command.temperature)
        return _c3_basic_plan(room_target=command.temperature)
    if isinstance(command, SetBoostCommand):
        raise ApiError(ATW_LEGACY_WRITE_REFUSED)
    if isinstance(command, SetSilentModeCommand):
        if basic is None and command.enabled is None:
            raise ApiError(ATW_LEGACY_WRITE_REFUSED)
        _require_control()
        if command.level is not None:
            return _c3_feature_plan(
                BODY_SILENCE, command.level > 0, silent_level=command.level
            )
        return _c3_feature_plan(BODY_SILENCE, bool(command.enabled))
    if isinstance(command, SetEcoModeCommand):
        _require_control()
        return _c3_feature_plan(BODY_ECO, command.enabled)
    if isinstance(command, SetZoneRunModeCommand):
        zone1_run_mode_setpoint(ctx, command.mode)
        raise ApiError(KJRH120L_ONLY_ZONE1)

    _require_control()
    if isinstance(command, SetDhwPowerCommand):
        return _c3_basic_plan(dhw_power=command.on)
    if isinstance(command, SetDhwTargetCommand):
        return _c3_basic_plan(dhw_target=command.temperature)
    if isinstance(command, SetFastDhwCommand):
        return _c3_basic_plan(fast_dhw=command.enabled)
    if isinstance(command, SetZonePowerCommand):
        if command.zone == 2:
            return _c3_basic_plan(zone2_power=command.on)
        return _c3_basic_plan(zone1_power=command.on)
    if isinstance(command, SetZoneTargetCommand):
        if command.zone == 2:
            return _c3_basic_plan(zone2_water_target=command.temperature)
        return _c3_basic_plan(zone1_water_target=command.temperature)
    if isinstance(command, SetZoneCurveCommand):
        if command.zone == 2:
            return _c3_basic_plan(zone2_curve=command.enabled)
        return _c3_basic_plan(zone1_curve=command.enabled)
    raise WriteRefused(f"Unsupported command intent: {command!r}")


class AtwAdapter:
    """Dialect adapter for 171H120F air-to-water heat pumps."""

    profile = ModelProfile.ATW
    # 171H120F sends no periodic 0x01 status, only sporadic 0x04 change
    # notifications (#129), so availability follows the MQTT link alone.
    periodic_status_push = False

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        return decode_atw_status(status.raw_body)

    def decode_sensors(
        self, sensors: ITSSensors, status: ITSStatus
    ) -> ITSSensors:
        if atw_sensors_are_placeholder(sensors.raw_body):
            placeholder_fields: dict[str, Any] = dict(_ATW_PLACEHOLDER_SENSOR_FIELDS)
            sensors = dataclasses.replace(sensors, **placeholder_fields)
        if status is not None and status.box_bottom_temp is not None:
            return dataclasses.replace(sensors, th_temp=status.box_bottom_temp)
        return sensors

    def describe_variant(self, status: ITSStatus | None) -> str:
        body = bytes(status.raw_body) if status is not None and status.raw_body else b""
        if not body:
            return "atw_unclassified"
        if _is_27_byte_atw_status(body):
            return "atw_27_byte"
        if _is_rotenso_atw_status(body):
            return "atw_rotenso_25_byte"
        return f"atw_{len(body)}_byte"

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        if sensors is None:
            return None
        if sensors.raw_body[:1] == b"\x10" and sensors.twout_temp is not None:
            return sensors.twout_temp
        if atw_sensors_are_placeholder(sensors.raw_body):
            return None
        return sensors.th_temp

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        basic = status.c3_basic if status is not None else None
        if isinstance(basic, C3BasicStatus):
            low, high = (
                basic.zone1_water_range
                if basic.zone1_water_temp
                else (basic.room_min, basic.room_max)
            )
            if 0 < low < high:
                return float(low), float(high)
        return default_climate_temp_bounds(set_mode)

    def is_kjrh120l_dual(
        self, status: ITSStatus | None, zone1_latched: bool
    ) -> bool:
        return False

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return status.c3_basic if status is not None else None

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        basic = status.c3_basic
        return float(basic.dhw_target) if isinstance(basic, C3BasicStatus) else None

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_atw_write(command, ctx)
