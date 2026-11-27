"""KJR-86T3 table dialect (sn8 17186T3A, AQS Energie AQUAPURA variant, #126).

This wired controller ignores every standard C3 query: the cloud echoes the
command back (the 1-byte ``aa,0b,c3,...,01,2e`` stub of #126). It answers
"table" queries instead: message type 0x03 with a 16-byte body
``01 <table u16 BE>`` zero-padded. The official app reads tables
0/100/400/500/600/610/900/1000 through ``luaQuery({"u16_table": N})``.

Reply body (frame[10:-1]): ``[0]=00``, ``[1..2]`` table, ``[5]`` series
(0x83), ``[11]`` period, then the table data. u16 values are big-endian and
signed; temperatures are x10; ``0x7fff``/``0xffff`` mark an unused field. The
offsets below were matched against the app's own decoded ``luaQuery`` JSON
captured at the same moment, on one unit.

Writes (validated on that unit for table 500: DHW setpoint 55 -> 54 -> 55 °C
and silent mode on/off): message type 0x02, body
``00 <table u16 BE> ff ff 83 ff 00`` + data. A field read at reply body[k] is
written at body[k - 4] (the write header has no ``ff ff ff <period>`` block),
0xff leaves a byte unchanged, and the body keeps the table's length (a 35-byte
table-500 write is echoed). The device answers with the updated table; an echo
of the command means it was rejected. Power at body[13] is **not** applied
through table 500, and Smart-Grid-ready accepted 0 but refused 1, so power,
mode and SG stay app-only.
"""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses

from ..api import ITSSensors, ITSStatus
from ..c3_basic import C3BasicStatus
from ..c3_frame import (
    build_c3_frame,
    build_c3_query,
    extract_c3_body,
    parse_hex_response,
)
from ..cloud import ApiError
from ..commands import (
    HeatPumpCommand,
    SetDhwTargetCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
)
from .probes import OptionalProbe
from .profile import ModelProfile
from .read import ReadPlan
from .views import (
    HEAT_OFF_MODES,
    LEGACY_BOOST_MUTE,
    NO_ZONE2_VIEW,
    ClimateView,
    EntityCapabilities,
    ViewContext,
    legacy_climate_view,
)
from .write import WriteContext, WritePlan, WriteRefused

KJR86T3_TABLE_LIMITS = 100
KJR86T3_TABLE_SETTINGS = 500
KJR86T3_TABLE_ICONS = 900
KJR86T3_TABLE_PARAMS = 1000
KJR86T3_QUERY_BODY_LEN = 16

KJR86T3_POWER_INDEX = 13  # table 500, u8s_power_on
KJR86T3_MODE_INDEX = 14  # table 500, u8s_mode (0x40 = Eco)
KJR86T3_SETPOINT_INDEX = 15  # table 500, u16s_t5s (x10)
KJR86T3_SILENCE_INDEX = 17  # table 500, u8s_silence_active
KJR86T3_SMART_GRID_INDEX = 20  # table 500, u8s_smart_grid_active ("SG ready")
KJR86T3_TANK_TEMP_INDEX = 30  # table 900, u16_icons_temp (x10)
KJR86T3_VOLTAGE_INDEX = 31  # table 1000, u16_parm_voltage
KJR86T3_TP_INDEX = 43  # table 1000, u16_parm_tp (x10)
KJR86T3_TH_INDEX = 45  # table 1000, u16_parm_th (x10)
KJR86T3_T3_INDEX = 47  # table 1000, u16_parm_t3 (x10)
KJR86T3_T4_INDEX = 49  # table 1000, u16_parm_t4 (x10)
KJR86T3_ENERGY_INDEX = 85  # table 1000, u32_parm_ap (x100, kWh)
# DHW setpoint range reported by table 100 (u16_t5s_min / u16_t5s_max).
KJR86T3_TEMP_MIN = 38
KJR86T3_TEMP_MAX = 70

KJR86T3_WRITE_SHIFT = 4
KJR86T3_WRITE_HEADER = (0xFF, 0xFF, 0x83, 0xFF, 0x00)
KJR86T3_TABLE_SETTINGS_LEN = 36
KJR86T3_WRITABLE_SETTINGS = frozenset(
    {KJR86T3_SETPOINT_INDEX, KJR86T3_SETPOINT_INDEX + 1, KJR86T3_SILENCE_INDEX}
)

KJR86T3_UNSUPPORTED_WRITE = (
    "Only the DHW setpoint and silent mode can be changed on the KJR-86T3; "
    "power, mode and boost writes have not been validated"
)
KJR86T3_NO_LEVEL_2 = "The KJR-86T3 has a single silent level"

_INVALID_U16 = (0x7FFF, 0xFFFF)


def build_kjr86t3_table_query(table: int) -> str:
    """Return the read-only table query frame for ``table``."""
    body = [0x01, (table >> 8) & 0xFF, table & 0xFF]
    body += [0x00] * (KJR86T3_QUERY_BODY_LEN - len(body))
    return build_c3_frame(0x03, bytes(body))


def build_kjr86t3_write(
    table: int, fields: Mapping[int, int], body_len: int = KJR86T3_TABLE_SETTINGS_LEN
) -> str:
    """Return a sparse write frame setting ``fields`` (reply index -> byte)."""
    body = [0x00, (table >> 8) & 0xFF, table & 0xFF, *KJR86T3_WRITE_HEADER]
    body += [0xFF] * (body_len - len(body))
    for index, value in fields.items():
        pos = index - KJR86T3_WRITE_SHIFT
        if not len(KJR86T3_WRITE_HEADER) + 3 <= pos < body_len:
            raise ValueError(f"table {table} index {index} is not writable")
        body[pos] = value & 0xFF
    return build_c3_frame(0x02, bytes(body))


def build_kjr86t3_settings_write(
    *, setpoint: int | None = None, silence: bool | None = None
) -> str:
    """Return the table-500 write for a DHW setpoint and/or silent mode."""
    fields: dict[int, int] = {}
    if setpoint is not None:
        if not KJR86T3_TEMP_MIN <= setpoint <= KJR86T3_TEMP_MAX:
            raise ValueError(
                f"Temperature {setpoint}C out of range "
                f"(allowed: {KJR86T3_TEMP_MIN}-{KJR86T3_TEMP_MAX}C)"
            )
        raw = setpoint * 10
        fields[KJR86T3_SETPOINT_INDEX] = raw >> 8
        fields[KJR86T3_SETPOINT_INDEX + 1] = raw & 0xFF
    if silence is not None:
        fields[KJR86T3_SILENCE_INDEX] = 1 if silence else 0
    if not fields:
        raise ValueError("nothing to write")
    if not set(fields) <= KJR86T3_WRITABLE_SETTINGS:
        raise ValueError("KJR-86T3 write outside the validated fields")
    return build_kjr86t3_write(KJR86T3_TABLE_SETTINGS, fields)


def kjr86t3_table_matches(body: bytearray | bytes, table: int) -> bool:
    """Return whether ``body`` is a real reply for ``table`` (not an echo).

    An echoed query starts with 0x01 and an echoed write is the command itself;
    a device reply starts with ``00 <table>`` and carries the table data.
    """
    return (
        len(body) > 12
        and body[0] == 0x00
        and body[1] == (table >> 8) & 0xFF
        and body[2] == table & 0xFF
    )


def verify_kjr86t3_write(command_hex: str, response_hex: str) -> None:
    """Raise ``ApiError`` when the device echoed a write or returned no table-500 reply."""
    try:
        raw_reply = parse_hex_response(response_hex)
    except ValueError as err:
        raise ApiError(f"The KJR-86T3 write returned invalid hex: {err}") from err
    if bytes(raw_reply) == bytes.fromhex(command_hex):
        raise ApiError("The KJR-86T3 rejected the write (command echoed back)")
    try:
        _body_type, body = extract_c3_body(raw_reply)
    except ValueError as err:
        raise ApiError(f"The KJR-86T3 write returned an invalid reply: {err}") from err
    if not kjr86t3_table_matches(body, KJR86T3_TABLE_SETTINGS):
        raise ApiError(
            "The KJR-86T3 write did not return an updated table 500 reply"
        )


def _u16(body: bytearray | bytes, index: int) -> int | None:
    if len(body) < index + 2:
        return None
    raw = (body[index] << 8) | body[index + 1]
    if raw in _INVALID_U16:
        return None
    return raw - 0x10000 if raw & 0x8000 else raw


def _temp(body: bytearray | bytes, index: int) -> float | None:
    raw = _u16(body, index)
    return None if raw is None else raw / 10


def decode_kjr86t3_status(
    settings: bytearray | bytes, params: bytearray | bytes
) -> ITSStatus:
    """Decode KJR-86T3 tables 500 (settings) and 1000 (parameters)."""
    status = ITSStatus()
    status.raw_body = bytes(settings)
    status.error_code = 0
    # No compressor signal has been mapped from these tables: unknown, not off.
    status.comp_running = None

    # Status mode 1 ("Heat") projects to HVAC heat, so a powered DHW unit reads
    # on; the controller's own mode byte (0x40 = Eco) is not a C3 mode.
    if len(settings) > KJR86T3_POWER_INDEX:
        power_on = settings[KJR86T3_POWER_INDEX] != 0
        status.mode = 1 if power_on else 0
        status.mode_name = "Heat" if power_on else "Off"

    setpoint = _temp(settings, KJR86T3_SETPOINT_INDEX)
    if setpoint is not None:
        status.t5s_def = setpoint
        status.set_temperature = int(setpoint)

    if len(settings) > KJR86T3_SILENCE_INDEX:
        status.mute_valid = settings[KJR86T3_SILENCE_INDEX] == 1

    if len(params) >= KJR86T3_ENERGY_INDEX + 4:
        raw = int.from_bytes(
            params[KJR86T3_ENERGY_INDEX:KJR86T3_ENERGY_INDEX + 4], "big"
        )
        # 0 and 0xFFFFFFFF are never a real meter reading (a 0 would be a
        # statistics reset in HA); keep the counter unknown instead.
        if raw not in (0, 0xFFFFFFFF):
            status.total_kwh = round(raw / 100, 2)

    return status


def decode_kjr86t3_sensors(
    params: bytearray | bytes, icons: bytearray | bytes
) -> ITSSensors:
    """Decode KJR-86T3 tables 1000 (parameters) and 900 (display icons).

    The DHW tank temperature (the value the app shows) is routed to
    ``th_temp``, the field the "DHW Tank Temperature" sensor reads. The
    controller's own Th probe is a different sensor and is not reused there.
    """
    sensors = ITSSensors()
    sensors.raw_body = bytes(params)
    sensors.th_temp = _temp(icons, KJR86T3_TANK_TEMP_INDEX)
    sensors.tp_temp = _temp(params, KJR86T3_TP_INDEX)
    sensors.t3_temp = _temp(params, KJR86T3_T3_INDEX)
    sensors.t4_temp = _temp(params, KJR86T3_T4_INDEX)
    sensors.odu_voltage = _u16(params, KJR86T3_VOLTAGE_INDEX)
    sensors.tf_temp = None
    sensors.odu_current = None
    return sensors


def parse_kjr86t3_table_reply(response_hex: str, table: int) -> bytes:
    """Parse and validate a single KJR-86T3 table reply hex frame."""
    _body_type, body = extract_c3_body(parse_hex_response(response_hex))
    if not kjr86t3_table_matches(body, table):
        raise ApiError(
            f"Table {table} query returned no device data; device may be "
            f"offline or unsupported. Raw: {response_hex}"
        )
    return bytes(body)


def kjr86t3_climate_view(status: ITSStatus | None) -> ClimateView:
    """DHW climate card: tank target only; power and mode stay app-only."""
    view = legacy_climate_view(
        status,
        lambda _set_mode: (float(KJR86T3_TEMP_MIN), float(KJR86T3_TEMP_MAX)),
        hvac_modes=HEAT_OFF_MODES,
    )
    return dataclasses.replace(view, supports_turn_on_off=False)


def plan_kjr86t3_write(command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
    """Plan a KJR-86T3 table-500 write (DHW setpoint or silent mode)."""
    if isinstance(command, (SetTargetTemperatureCommand, SetDhwTargetCommand)):
        temp = int(round(kjr86t3_climate_view(ctx.status).clamp(command.temperature)))
        return WritePlan.static(
            build_kjr86t3_settings_write(setpoint=temp),
            f"kjr86t3 dhw_target={temp}",
            verify=verify_kjr86t3_write,
        )
    if isinstance(command, SetSilentModeCommand):
        if command.level == 2:
            raise WriteRefused(KJR86T3_NO_LEVEL_2)
        on = bool(command.level) if command.level is not None else bool(command.enabled)
        return WritePlan.static(
            build_kjr86t3_settings_write(silence=on),
            f"kjr86t3 silence={on}",
            verify=verify_kjr86t3_write,
        )
    raise WriteRefused(KJR86T3_UNSUPPORTED_WRITE)


class Kjr86t3Adapter:
    """Dialect adapter for the KJR-86T3 table-based controller (#126)."""

    profile = ModelProfile.KJR86T3
    # No standard 0x01 push has been seen from this controller.
    periodic_status_push = False
    accepts_energy_notify = False
    offers_c3_basic_control = False
    latches_zone1_capability = False

    def build_query(self, subtype: int) -> str:
        return build_c3_query(subtype)

    def plan_status_read(self) -> ReadPlan[ITSStatus]:
        return ReadPlan(
            commands=(
                build_kjr86t3_table_query(KJR86T3_TABLE_SETTINGS),
                build_kjr86t3_table_query(KJR86T3_TABLE_PARAMS),
            ),
            decode=lambda replies: decode_kjr86t3_status(
                parse_kjr86t3_table_reply(replies[0], KJR86T3_TABLE_SETTINGS),
                parse_kjr86t3_table_reply(replies[1], KJR86T3_TABLE_PARAMS),
            ),
            description="kjr86t3_status_tables_500_1000",
        )

    def plan_sensors_read(self) -> ReadPlan[ITSSensors]:
        return ReadPlan(
            commands=(
                build_kjr86t3_table_query(KJR86T3_TABLE_PARAMS),
                build_kjr86t3_table_query(KJR86T3_TABLE_ICONS),
            ),
            decode=lambda replies: decode_kjr86t3_sensors(
                parse_kjr86t3_table_reply(replies[0], KJR86T3_TABLE_PARAMS),
                parse_kjr86t3_table_reply(replies[1], KJR86T3_TABLE_ICONS),
            ),
            description="kjr86t3_sensors_tables_1000_900",
        )

    def decode_status(self, status: ITSStatus) -> ITSStatus:
        # Table reads return an already-decoded status; pushes are ignored
        # upstream because table replies never carry subtype 0x01.
        return status

    def decode_sensors(self, sensors: ITSSensors, status: ITSStatus) -> ITSSensors:
        # Silent mode lives in table 500 (status); expose it through the
        # legacy ctrl_flag pair the Silent Mode select reads (Level 1 only).
        silent = status is not None and status.mute_valid
        return dataclasses.replace(sensors, ctrl_flag=1 if silent else 0, mute_level=0)

    def describe_variant(self, status: ITSStatus | None) -> str:
        return "kjr86t3"

    def sensors_placeholder(self, raw_0x02: bytes) -> bool | None:
        return None

    def observed_zone1(self, status: ITSStatus) -> bool:
        return False

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

    def climate_current_temperature(self, sensors: ITSSensors | None) -> float | None:
        return sensors.th_temp if sensors is not None else None

    def climate_temp_bounds(
        self, status: ITSStatus | None, set_mode: int | None
    ) -> tuple[float, float]:
        return float(KJR86T3_TEMP_MIN), float(KJR86T3_TEMP_MAX)

    def is_kjrh120l_dual(self, status: ITSStatus | None, zone1_latched: bool) -> bool:
        return False

    def c3_basic(self, status: ITSStatus | None) -> C3BasicStatus | None:
        return None

    def dhw_target_temp(self, status: ITSStatus) -> float | None:
        return status.t5s_def

    def entity_capabilities(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> EntityCapabilities:
        # Silent mode is written through table 500; no boost write exists.
        return EntityCapabilities(mute=LEGACY_BOOST_MUTE)

    def climate_view(self, status: ITSStatus | None, ctx: ViewContext) -> ClimateView:
        return kjr86t3_climate_view(status)

    def zone2_climate_view(
        self, status: ITSStatus | None, ctx: ViewContext
    ) -> ClimateView:
        return NO_ZONE2_VIEW

    def plan_write(self, command: HeatPumpCommand, ctx: WriteContext) -> WritePlan:
        return plan_kjr86t3_write(command, ctx)
