"""iLetComfort / ITS/BTRI Heat Pump Cloud API Client.

Stripped version of iletcomfort_client.py for use as a Home Assistant integration
library. Contains only the API client classes and protocol functions — no CLI code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .c3_basic import C3BasicStatus
from .c3_frame import (
    DEVICE_TYPE_C3,
    SENSOR_DISCONNECTED,
    TEMP_OFFSET,
    _temp_offset,
    build_c3_frame,
    build_c3_query,
    build_c3_set,
    extract_c3_body,
    parse_hex_response,
)
from .cloud import (
    API_BASE,
    APP_ID,
    APP_KEY,
    APP_SECRET,
    CLIENT_ID,
    CLIENT_SECRET,
    ENCRYPT_KEY,
    IOT_KEY,
    ApiError,
    AppCert,
    AuthError,
    DollinCloudClient,
    encrypt_password,
    mask_identifier,
    sign_v1,
    sign_v2,
)

if TYPE_CHECKING:
    # The dialect modules import this one, so their sub-structs are only
    # imported for type checking (annotations are lazy). Consequence:
    # ``typing.get_type_hints(ITSStatus)`` raises NameError at runtime; use
    # ``dataclasses.fields`` / ``protocols.flatten_status`` for introspection.
    from .protocols.atw import AtwFeatureStatus
    from .protocols.kjrh120l import Kjrh120lStatus

_LOGGER = logging.getLogger(__name__)

__all__ = [
    "API_BASE",
    "APP_ID",
    "APP_KEY",
    "APP_SECRET",
    "CLIENT_ID",
    "CLIENT_SECRET",
    "DEVICE_TYPE_C3",
    "ENCRYPT_KEY",
    "IOT_KEY",
    "MODE_COOL",
    "MODE_HEAT",
    "MODE_MAP",
    "MODE_OFF",
    "MODE_WATERPUMP",
    "QUERY_TO_SET_MODE",
    "SENSORS_MIN_BODY_LEN",
    "SENSOR_DISCONNECTED",
    "STATUS_MIN_BODY_LEN",
    "STATUS_MODE_COOL",
    "STATUS_MODE_HEAT",
    "STATUS_MODE_OFF",
    "TEMP_OFFSET",
    "TEMP_RANGES",
    "ApiError",
    "AppCert",
    "AuthError",
    "DollinCloudClient",
    "ILetComfortClient",
    "ITSSensors",
    "ITSStatus",
    "_temp_offset",
    "build_c3_frame",
    "build_c3_query",
    "build_c3_set",
    "decode_its_sensors",
    "decode_its_status",
    "encrypt_password",
    "extract_c3_body",
    "mask_identifier",
    "parse_hex_response",
    "sign_v1",
    "sign_v2",
]

# A substantive C3 query response body must be long enough for the decoder to
# populate every field surfaced as an entity. Shorter bodies are echo-only /
# partial frames (see issue #5) and are treated as a transient device/cloud
# failure rather than real data, so we don't overwrite good entity state with
# all-defaults.
#
# Status keeps a low threshold on purpose: its primary role is the climate
# entity (mode/setpoint, decoded at body_len >= d+5), so a partial status frame
# is still usable and must not be rejected. Sensors are monitoring-only, and the
# highest offset among the entities sensor.py exposes from ITSSensors is
# odu_voltage, decoded only when body_len > d+30 (>= 32); below that, temps
# and/or odu_voltage would be blanked, so require the full sensor data block.
STATUS_MIN_BODY_LEN = 6
SENSORS_MIN_BODY_LEN = 32

# SET command operating modes
MODE_OFF = 0x00
MODE_HEAT = 0x01
MODE_COOL = 0x03
MODE_WATERPUMP = 0x04

# Status-frame (query response) mode values. Note these differ from the SET
# modes above: Cool is 3 in a SET frame but 2 in a status frame.
STATUS_MODE_OFF = 0
STATUS_MODE_HEAT = 1
STATUS_MODE_COOL = 2
MODE_MAP: dict[str, int] = {
    "off": MODE_OFF,
    "heat": MODE_HEAT,
    "cool": MODE_COOL,
    "waterpump": MODE_WATERPUMP,
}

# Temperature validation ranges per mode (Celsius)
TEMP_RANGES: dict[int, tuple[int, int]] = {
    MODE_HEAT: (10, 40),
    MODE_COOL: (12, 40),
    MODE_WATERPUMP: (15, 40),
}

# Query response mode → SET mode mapping
QUERY_TO_SET_MODE: dict[int, int] = {
    0: MODE_OFF,
    1: MODE_HEAT,
    2: MODE_COOL,
    4: MODE_WATERPUMP,
}


# ---------------------------------------------------------------------------
# ITS protocol response decoding -- subtype 0x01 (Status & Control)
# ---------------------------------------------------------------------------

@dataclass
class ITSStatus:
    """Decoded ITS subtype 0x01 -- status, control settings, and runtime data."""

    pump_outdoor: bool = False
    pump_system: bool = False
    mode: int = 0
    mode_name: str = "Off"
    t5s_def: float | None = None
    t5s_max: float | None = None
    set_temperature: int = 0
    config_status: int = 0
    td_max: float | None = None
    td_min: float | None = None
    ptc_temperature_1: float | None = None
    trdh_max: float | None = None
    trdh_min: float | None = None
    trdh_def: float | None = None
    mute_valid: bool = False
    force_heat_valid: bool = False
    sterilize_valid: bool = False
    comp_running: bool | None = False
    ibh_running: bool = False
    sterilize_running: bool = False
    status_flags_raw: int = 0
    enable_flags_1: int = 0
    enable_flags_2: int = 0
    box_bottom_temp: float | None = None
    ptc_temperature: float | None = None
    tr_temperature: float | None = None
    version_or_sterilize_hour: int = 0
    sterilize_min: int = 0
    sterilize_temperature: float | None = None
    sterilize_cycle_days: int = 0
    error_code: int = 0
    heat_pump_work_temp_limit: float | None = None
    vacation_start_year: int = 0
    vacation_start_month: int = 0
    vacation_start_day: int = 0
    vacation_end_month: int = 0
    vacation_end_day: int = 0
    # Telemetry below defaults to None (not decoded); each decoder or subtype
    # overlay sets it only when the frame actually carries it, so an entity
    # can tell unknown from a real zero (#111). Cumulative counters must never
    # read 0 in place of unknown (that is a meter reset in HA statistics).
    exv_drg: int | None = None
    pressure_h: int | None = None
    pressure_l: int | None = None
    comp_frq: int | None = None
    total_kwh: int | None = None
    total_thermal_kwh: int | None = None
    comp_total_run_hours: int | None = None
    fan_total_run_hours: int = 0
    # Per-dialect sub-structs (#236). Diagnostics flattens them back to the
    # flat ``kjrh120l_*`` / ``atw_*`` keys (``protocols.flatten_status``).
    # KJRH-120L (sn8 17100003) subtype 0x01 fields; None on other dialects.
    kjrh120l: Kjrh120lStatus | None = None
    # ATW subtype 0x05 (Silent/Super-Silent) and 0x07/0x05 (ECO) state
    # (#139/#188); None until one of those frames is overlaid.
    atw: AtwFeatureStatus | None = None
    # Standard Midea C3 basic status when an ATW (sn8 171H120F) status frame
    # matches that layout; None otherwise.
    c3_basic: C3BasicStatus | None = None
    raw_body: bytes = field(default_factory=bytes, repr=False)


def decode_its_status(body: bytearray) -> ITSStatus:
    """Decode ITS subtype 0x01 body into an ITSStatus object."""
    status = ITSStatus()
    status.raw_body = bytes(body)
    d = 1
    body_len = len(body)

    if body_len < d + 5:
        return status

    b0 = body[d + 0]
    status.pump_outdoor = bool(b0 & 0x01)
    status.pump_system = bool(b0 & 0x02)

    modes = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", 4: "Water Pump"}
    status.mode = body[d + 1]
    status.mode_name = modes.get(status.mode, f"Unknown({status.mode})")

    status.t5s_def = _temp_offset(body[d + 2])
    status.t5s_max = _temp_offset(body[d + 3])
    status.set_temperature = body[d + 4]

    if body_len > d + 5:
        status.config_status = body[d + 5]
    if body_len > d + 7:
        status.td_max = _temp_offset(body[d + 6])
        status.td_min = _temp_offset(body[d + 7])
    if body_len > d + 11:
        status.ptc_temperature_1 = _temp_offset(body[d + 8])
        status.trdh_max = _temp_offset(body[d + 9])
        status.trdh_min = _temp_offset(body[d + 10])
        status.trdh_def = _temp_offset(body[d + 11])
    if body_len > d + 12:
        b12 = body[d + 12]
        status.mute_valid = bool(b12 & 0x80)
        status.force_heat_valid = bool(b12 & 0x40)
        status.sterilize_valid = bool(b12 & 0x20)
    if body_len > d + 13:
        b13 = body[d + 13]
        status.status_flags_raw = b13
        status.comp_running = bool(b13 & 0x01)
        status.ibh_running = bool(b13 & 0x02)
        status.sterilize_running = bool(b13 & 0x04)
    if body_len > d + 15:
        status.enable_flags_1 = body[d + 14]
        status.enable_flags_2 = body[d + 15]
    if body_len > d + 18:
        status.box_bottom_temp = _temp_offset(body[d + 16])
        status.ptc_temperature = _temp_offset(body[d + 17])
        status.tr_temperature = _temp_offset(body[d + 18])
    if body_len > d + 22:
        status.version_or_sterilize_hour = body[d + 19]
        status.sterilize_min = body[d + 20]
        status.sterilize_temperature = _temp_offset(body[d + 21])
        status.sterilize_cycle_days = body[d + 22]
    if body_len > d + 23:
        status.error_code = body[d + 23]
    if body_len > d + 24:
        status.heat_pump_work_temp_limit = _temp_offset(body[d + 24])
    if body_len > d + 28:
        status.vacation_start_year = body[d + 25]
        status.vacation_start_month = body[d + 26]
        status.vacation_start_day = body[d + 27]
        b28 = body[d + 28]
        status.vacation_end_month = (b28 >> 5) & 0x07
        status.vacation_end_day = b28 & 0x1F
    if body_len > d + 48:
        status.exv_drg = (body[d + 35] << 8) | body[d + 36]
        status.pressure_h = (body[d + 37] << 8) | body[d + 38]
        status.pressure_l = (body[d + 39] << 8) | body[d + 40]
        status.comp_frq = (body[d + 41] << 8) | body[d + 42]
        status.total_kwh = (body[d + 43] << 8) | body[d + 44]
        status.comp_total_run_hours = (body[d + 45] << 8) | body[d + 46]
        status.fan_total_run_hours = (body[d + 47] << 8) | body[d + 48]

    # Some models (e.g. MSC-70D2N8-A, issue #11) leave the status-flag byte at 0
    # even while running; a non-zero compressor frequency is an authoritative
    # "running" signal, so honor it regardless of the flag bit.
    status.comp_running = status.comp_running or (status.comp_frq or 0) > 0

    return status


# ---------------------------------------------------------------------------
# ITS protocol response decoding -- subtype 0x02 (Sensors & Extended)
# ---------------------------------------------------------------------------

@dataclass
class ITSSensors:
    """Decoded ITS subtype 0x02 -- sensor temperatures and extended data."""

    status_byte: int = 0
    online_num: int = 0
    odu_mac_type: int = 0
    limit_frq_code: int = 0
    # Telemetry defaults to None (not decoded) rather than 0 (#111).
    tf_temp: float | None = None
    tp_temp: float | None = None
    th_temp: float | None = None
    water_pres: int = 0
    water_flow: int | None = None
    capacity_hp: int = 0
    t3_temp: float | None = None
    t4_temp: float | None = None
    t2_temp: float | None = None
    t2b_temp: float | None = None
    twin_temp: float | None = None
    twout_temp: float | None = None
    t1_temp: float | None = None
    odu_current: float | None = None
    odu_voltage: int | None = None
    dc_current: int | None = None
    idu_version: str = ""
    odu_version: str = ""
    hmi_version: str = ""
    ctrl_flag: int = 0  # d+41: 0=normal, 1=mute, 2=boost
    mute_level: int = 0  # d+40: 0=Level 1 (or off), 1=Level 2
    dc_voltage: int | None = None
    ibh1_total_run_hours: int = 0
    ibh2_total_run_hours: int = 0
    tbh_total_run_hours: int = 0
    ahs_total_run_hours: int = 0
    hpc_value: int = 0
    raw_body: bytes = field(default_factory=bytes, repr=False)


def _decode_its_version(b0: int, b1: int, b2: int) -> str:
    """Decode a 3-byte ITS version field."""
    year = 2000 + (b0 >> 1)
    month = ((b0 & 1) << 3) | (b1 >> 5)
    day = b1 & 0x1F
    version = b2
    return f"{year:04d}-{month:02d}-{day:02d} v{version}"


def decode_its_sensors(body: bytearray) -> ITSSensors:
    """Decode ITS subtype 0x02 body into an ITSSensors object."""
    sensors = ITSSensors()
    sensors.raw_body = bytes(body)
    d = 1
    body_len = len(body)

    if body_len < d + 1:
        return sensors

    sensors.status_byte = body[d + 0]

    if body_len > d + 13:
        sensors.online_num = body[d + 11]
        sensors.odu_mac_type = body[d + 12]
        sensors.limit_frq_code = body[d + 13]
    if body_len > d + 16:
        sensors.tf_temp = body[d + 14]
        sensors.tp_temp = body[d + 15]
        sensors.th_temp = body[d + 16]
    if body_len > d + 18:
        sensors.water_pres = body[d + 17]
        sensors.water_flow = body[d + 18]
    if body_len > d + 19:
        sensors.capacity_hp = body[d + 19]
    if body_len > d + 26:
        sensors.t3_temp = _temp_offset(body[d + 20])
        sensors.t4_temp = _temp_offset(body[d + 21])
        sensors.t2_temp = _temp_offset(body[d + 22])
        sensors.t2b_temp = _temp_offset(body[d + 23])
        sensors.twin_temp = _temp_offset(body[d + 24])
        sensors.twout_temp = _temp_offset(body[d + 25])
        sensors.t1_temp = _temp_offset(body[d + 26])
    if body_len > d + 30:
        # Fixed-point current: high byte = integer amps, low byte = 1/256
        # fraction. Raw 0x0400 (1024) -> 4.0 A, confirmed against the official
        # app for the MSC-70D2N8-A (issue #11) and consistent with issue #10.
        sensors.odu_current = round((((body[d + 27] << 8) | body[d + 28]) / 256), 2)
        sensors.odu_voltage = body[d + 29]
        sensors.dc_current = body[d + 30]
    if body_len > d + 39:
        sensors.idu_version = _decode_its_version(
            body[d + 31], body[d + 32], body[d + 33],
        )
        sensors.odu_version = _decode_its_version(
            body[d + 34], body[d + 35], body[d + 36],
        )
        sensors.hmi_version = _decode_its_version(
            body[d + 37], body[d + 38], body[d + 39],
        )
    if body_len > d + 48:
        sensors.mute_level = body[d + 40]  # 0=Level 1 (or off), 1=Level 2
        sensors.ctrl_flag = body[d + 41]  # 0=normal, 1=mute, 2=boost
        sensors.dc_voltage = (body[d + 42] << 8) | body[d + 43]
        sensors.ibh1_total_run_hours = body[d + 44]
        sensors.ibh2_total_run_hours = body[d + 45]
        sensors.tbh_total_run_hours = body[d + 46]
        sensors.ahs_total_run_hours = body[d + 47]
        sensors.hpc_value = body[d + 48]

    return sensors


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------


class ILetComfortClient(DollinCloudClient):
    """Client for the iLetComfort / Midea Dollin cloud API (C3 protocol layer).

    For writes the client is transport only: the selected protocol dialect
    plans the command (``protocols.*.plan_write``) and the coordinator runs the
    plan's fresh read and SET over ``send_hex_command`` (#113).
    """

    def query_status(
        self, appliance_code: str, sn8: str | None = None,
    ) -> ITSStatus:
        """Query heat pump status (subtype 0x01).

        ``sn8`` is the appliance's model-code serial prefix. When it maps to a
        model-specific decode profile (see ``protocols``) the raw frame is
        re-decoded via that profile's status layout; an unknown/None sn8 uses
        the STANDARD decode unchanged.

        The query command itself is also profile-aware: the KJRH-120L's cloud
        rejects the standard long C3 query frame (code 1214) and needs a short
        ``ffff<ss><ss><ss>ff`` form, so the command is built via
        the dialect's ``build_query``. Other profiles keep the standard frame.
        """
        # Imported lazily to avoid a circular import (protocols imports api).
        from .protocols import get_adapter

        adapter = get_adapter(sn8)
        dialect_status = adapter.read_status(
            lambda command: self.send_hex_command(appliance_code, command)
        )
        if dialect_status is not None:
            return dialect_status
        command = adapter.build_query(0x01)
        response_hex = self.send_hex_command(appliance_code, command)
        _LOGGER.debug("query_status raw response: %s", response_hex)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        if len(body) < STATUS_MIN_BODY_LEN:
            raise ApiError(
                f"Status query returned a truncated frame "
                f"({len(body)} body bytes); device may be offline or "
                f"unsupported. Raw: {response_hex}"
            )
        status = decode_its_status(body)
        return adapter.decode_status(status)

    def query_sensors(
        self, appliance_code: str, sn8: str | None = None,
    ) -> ITSSensors:
        """Query heat pump sensors (subtype 0x02).

        ``sn8`` selects the query-command encoding: the KJRH-120L's cloud
        rejects the standard long C3 query frame (code 1214) and needs a short
        ``ffff<ss><ss><ss>ff`` form. Unknown/None sn8 uses the standard frame.
        """
        # Imported lazily to avoid a circular import (protocols imports api).
        from .protocols import get_adapter

        adapter = get_adapter(sn8)
        dialect_sensors = adapter.read_sensors(
            lambda command: self.send_hex_command(appliance_code, command)
        )
        if dialect_sensors is not None:
            return dialect_sensors
        command = adapter.build_query(0x02)
        response_hex = self.send_hex_command(appliance_code, command)
        _LOGGER.debug("query_sensors raw response: %s", response_hex)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        if len(body) < SENSORS_MIN_BODY_LEN:
            raise ApiError(
                f"Sensors query returned a truncated frame "
                f"({len(body)} body bytes); device may be offline or "
                f"unsupported. Raw: {response_hex}"
            )
        return decode_its_sensors(body)

    def _query_c3_subtype(
        self,
        appliance_code: str,
        subtype: int,
        *,
        expected_types: tuple[int, ...],
        min_len: int,
        label: str,
    ) -> bytes:
        """Query a C3 extended subtype and validate its response header and length."""
        command = build_c3_query(subtype)
        response_hex = self.send_hex_command(appliance_code, command)
        _LOGGER.debug("%s raw response: %s", label, response_hex)
        raw = parse_hex_response(response_hex)
        body_type, body = extract_c3_body(raw)
        if body_type not in expected_types or len(body) < min_len:
            raise ApiError(
                f"{label} returned unexpected or truncated frame "
                f"(type=0x{body_type:02x}, {len(body)} body bytes)"
            )
        return bytes(body)

    def query_atw_unit_params(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x10 unit parameters for live telemetry (#120)."""
        from .protocols.atw import ATW_UNIT_PARAMS_MIN_LEN, ATW_UNIT_PARAMS_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_UNIT_PARAMS_SUBTYPE,
            expected_types=(ATW_UNIT_PARAMS_SUBTYPE,),
            min_len=ATW_UNIT_PARAMS_MIN_LEN,
            label="ATW unit_params (0x10)",
        )

    def query_atw_hmi_params(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x0A HMI parameters for compressor run hours (#120)."""
        from .protocols.atw import ATW_HMI_PARAMS_MIN_LEN, ATW_HMI_PARAMS_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_HMI_PARAMS_SUBTYPE,
            expected_types=(ATW_HMI_PARAMS_SUBTYPE,),
            min_len=ATW_HMI_PARAMS_MIN_LEN,
            label="ATW hmi_params (0x0A)",
        )

    def query_atw_silent(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x05 Silent / Super-Silent state (#139/#188)."""
        from .protocols.atw import ATW_SILENT_MIN_LEN, ATW_SILENT_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_SILENT_SUBTYPE,
            expected_types=(ATW_SILENT_SUBTYPE,),
            min_len=ATW_SILENT_MIN_LEN,
            label="ATW silent (0x05)",
        )

    def query_atw_eco(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x07 (or shared 0x05) ECO state (#139/#188)."""
        from .protocols.atw import (
            ATW_ECO_MIN_LEN,
            ATW_ECO_SUBTYPE,
            ATW_SILENT_SUBTYPE,
        )

        return self._query_c3_subtype(
            appliance_code,
            ATW_ECO_SUBTYPE,
            expected_types=(ATW_ECO_SUBTYPE, ATW_SILENT_SUBTYPE),
            min_len=ATW_ECO_MIN_LEN,
            label="ATW eco (0x07)",
        )
