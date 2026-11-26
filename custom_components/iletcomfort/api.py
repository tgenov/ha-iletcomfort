"""iLetComfort / ITS/BTRI Heat Pump Cloud API Client.

Stripped version of iletcomfort_client.py for use as a Home Assistant integration
library. Contains only the API client classes and protocol functions — no CLI code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

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
    exv_drg: int = 0
    pressure_h: int = 0
    pressure_l: int = 0
    comp_frq: int | None = 0
    total_kwh: int = 0
    comp_total_run_hours: int = 0
    fan_total_run_hours: int = 0
    # KJRH-120L dual variant's separate DHW target (status body[15], issue #5).
    kjrh120l_dhw_setpoint: float | None = None
    # KJRH-120L Zone 1 fields from the subtype 0x01 body, decoded on every
    # KJRH-120L frame regardless of body[8:10] (app capture 2026-11-12/14).
    kjrh120l_zone1_power: bool | None = None  # body[8]
    kjrh120l_dhw_power: bool | None = None  # body[10]
    kjrh120l_run_mode: int | None = None  # body[11], heat=3, cool=2
    kjrh120l_zone1_setpoint: float | None = None  # body[12]
    kjrh120l_zone1_heat_max: int | None = None  # body[37]
    kjrh120l_zone1_heat_min: int | None = None  # body[38]
    kjrh120l_zone1_cool_max: int | None = None  # body[39]
    kjrh120l_zone1_cool_min: int | None = None  # body[40]
    # KJRH-120L outdoor ambient temp (status body[57] - 196; #148/#142/#136/#119).
    kjrh120l_outdoor_temp: float | None = None
    # ATW subtype 0x05 (Silent/Super-Silent) and 0x07/0x05 (ECO) decoded state (#139/#188).
    atw_silent_mode: bool | None = None
    atw_silent_level: int | None = None  # 0=Off, 1=Level 1, 2=Level 2 (Super Silent)
    atw_silent_super_selected: bool | None = None  # raw bit 3 (retained even when OFF)
    atw_eco_mode: bool | None = None
    atw_eco_timer: bool | None = None
    # Standard Midea C3 basic status (``c3_basic.C3BasicStatus``) when an ATW
    # (sn8 171H120F) status frame matches that layout; None otherwise.
    c3_basic: Any = None
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
    tf_temp: float | None = 0
    tp_temp: float | None = 0
    th_temp: float | None = 0
    water_pres: int = 0
    water_flow: int = 0
    capacity_hp: int = 0
    t3_temp: float | None = None
    t4_temp: float | None = None
    t2_temp: float | None = None
    t2b_temp: float | None = None
    twin_temp: float | None = None
    twout_temp: float | None = None
    t1_temp: float | None = None
    odu_current: float | None = 0.0
    odu_voltage: int | None = 0
    dc_current: int = 0
    idu_version: str = ""
    odu_version: str = ""
    hmi_version: str = ""
    ctrl_flag: int = 0  # d+41: 0=normal, 1=mute, 2=boost
    mute_level: int = 0  # d+40: 0=Level 1 (or off), 1=Level 2
    dc_voltage: int = 0
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
    """Client for the iLetComfort / Midea Dollin cloud API (C3 protocol layer)."""

    def query_status(
        self, appliance_code: str, sn8: str | None = None,
    ) -> ITSStatus:
        """Query heat pump status (subtype 0x01).

        ``sn8`` is the appliance's model-code serial prefix. When it maps to a
        model-specific decode profile (see ``model_profiles``) the raw frame is
        re-decoded via that profile's status layout; an unknown/None sn8 uses
        the STANDARD decode unchanged.

        The query command itself is also profile-aware: the KJRH-120L's cloud
        rejects the standard long C3 query frame (code 1214) and needs a short
        ``ffff<ss><ss><ss>ff`` form, so the command is built via
        ``build_query_command``. Other profiles keep the standard frame.
        """
        # Imported lazily to avoid a circular import (protocols imports api).
        from .protocols import get_adapter

        adapter = get_adapter(sn8)
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

        command = get_adapter(sn8).build_query(0x02)
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
        from .model_profiles import ATW_UNIT_PARAMS_MIN_LEN, ATW_UNIT_PARAMS_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_UNIT_PARAMS_SUBTYPE,
            expected_types=(ATW_UNIT_PARAMS_SUBTYPE,),
            min_len=ATW_UNIT_PARAMS_MIN_LEN,
            label="ATW unit_params (0x10)",
        )

    def query_atw_hmi_params(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x0A HMI parameters for compressor run hours (#120)."""
        from .model_profiles import ATW_HMI_PARAMS_MIN_LEN, ATW_HMI_PARAMS_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_HMI_PARAMS_SUBTYPE,
            expected_types=(ATW_HMI_PARAMS_SUBTYPE,),
            min_len=ATW_HMI_PARAMS_MIN_LEN,
            label="ATW hmi_params (0x0A)",
        )

    def query_atw_silent(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x05 Silent / Super-Silent state (#139/#188)."""
        from .model_profiles import ATW_SILENT_MIN_LEN, ATW_SILENT_SUBTYPE

        return self._query_c3_subtype(
            appliance_code,
            ATW_SILENT_SUBTYPE,
            expected_types=(ATW_SILENT_SUBTYPE,),
            min_len=ATW_SILENT_MIN_LEN,
            label="ATW silent (0x05)",
        )

    def query_atw_eco(self, appliance_code: str) -> bytes:
        """Query ATW subtype 0x07 (or shared 0x05) ECO state (#139/#188)."""
        from .model_profiles import (
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

    def set_device(
        self,
        appliance_code: str,
        *,
        sn8: str | None = None,
        mode: int | None = None,
        temperature: int | None = None,
        boost: bool | None = None,
        mute: int | None = None,
        mute_level: int | None = None,
        power_on: bool = False,
        last_on_state: tuple[int, int] | None = None,
        zone1_temperature: int | None = None,
        zone1_run_mode: int | None = None,
        zone1_power: bool | None = None,
    ) -> dict[str, Any]:
        """Send a SET command to the heat pump.

        ``sn8`` selects the write encoding per model. The KJRH-120L (sn8
        17100003) rejects the standard 62-byte C3 SET frame, so for that profile
        a captured short write command (``00 <field> 01 <value> ff``) is sent
        directly. Every other (unknown/None/ATW/AQUAPURA) sn8 keeps the legacy
        build_c3_set path unchanged: query current status for echo bytes, merge
        the requested changes, validate temperature ranges, build and send the
        SET frame.

        ``zone1_temperature``, ``zone1_run_mode`` and ``zone1_power`` exist only
        for the KJRH-120L. Any other sn8 raises ApiError before anything is sent.
        """
        # Imported lazily to avoid a circular import (model_profiles imports api).
        from .model_profiles import ModelProfile, resolve_profile

        if resolve_profile(sn8) is ModelProfile.KJRH120L:
            return self._set_device_kjrh120l(
                appliance_code,
                mode=mode,
                temperature=temperature,
                power_on=power_on,
                zone1_temperature=zone1_temperature,
                zone1_run_mode=zone1_run_mode,
                zone1_power=zone1_power,
            )
        if any(
            value is not None
            for value in (zone1_temperature, zone1_run_mode, zone1_power)
        ):
            raise ApiError(
                "Zone 1 setpoint, run mode and Zone 1 power writes are only "
                "supported on the KJRH-120L (sn8 17100003)"
            )

        # Query current status for echo bytes
        command = build_c3_query(0x01)
        response_hex = self.send_hex_command(appliance_code, command)
        _LOGGER.debug("set_device status raw response: %s", response_hex)
        raw = parse_hex_response(response_hex)
        _body_type, status_body = extract_c3_body(raw)
        status = decode_its_status(status_body)

        # An unknown mode can mean this model uses a different status layout.
        # Even an explicit mode override cannot make its echoed bytes safe.
        if status.mode not in (*QUERY_TO_SET_MODE, 3):
            raise ApiError(
                f"Unrecognized status mode {status.mode}; refusing to send a "
                "control command using an unvalidated status layout"
            )
        current_set_mode = QUERY_TO_SET_MODE.get(status.mode)

        # Handle power_on: restore last on-state
        if power_on and last_on_state is not None:
            if mode is None:
                mode = last_on_state[0]
            if temperature is None:
                temperature = last_on_state[1]

        eff_mode = mode if mode is not None else current_set_mode
        if eff_mode is None:
            raise ApiError(
                "Cannot preserve Auto mode in a control command; "
                "select an explicit operating mode first"
            )
        # Use active mode setpoint (t5s_def, offset-decoded) not DHW target (set_temperature)
        if temperature is not None:
            eff_temp = temperature
        elif status.t5s_def is not None:
            eff_temp = int(status.t5s_def)
        else:
            eff_temp = status.set_temperature
        temp_explicitly_set = temperature is not None

        # Determine ctrl_flag and mute_level
        eff_ctrl_flag = 0x00
        eff_mute_level = 0x00

        if boost is True:
            eff_ctrl_flag = 0x02
        elif mute is not None:
            if mute == 0:
                eff_ctrl_flag = 0x00
            else:
                eff_ctrl_flag = 0x01
                eff_mute_level = 0x00 if mute == 1 else 0x01

        if mute_level is not None:
            eff_mute_level = mute_level

        # Validate temperature
        if temp_explicitly_set and eff_mode in TEMP_RANGES and eff_mode != MODE_OFF:
            temp_min, temp_max = TEMP_RANGES[eff_mode]
            if not (temp_min <= eff_temp <= temp_max):
                mode_name = {v: k for k, v in MODE_MAP.items()}.get(eff_mode, "unknown")
                raise ValueError(
                    f"Temperature {eff_temp}C out of range for {mode_name} mode "
                    f"(allowed: {temp_min}-{temp_max}C)"
                )

        # Build and send
        set_hex = build_c3_set(
            mode=eff_mode,
            temperature=eff_temp,
            status_body=status_body,
            mute_level=eff_mute_level,
            ctrl_flag=eff_ctrl_flag,
        )

        set_response_hex = self.send_hex_command(appliance_code, set_hex)

        return {
            "sent": set_hex,
            "response": set_response_hex,
            "effective_mode": eff_mode,
            "effective_temp": eff_temp,
        }

    # -- Standard Midea C3 SET (ATW profile, sn8 171H120F) --------------------
    # Callers serialise these per appliance (coordinator write lock), so the
    # fresh read and the SET built from it form one transaction.

    @staticmethod
    def _require_c3_basic_profile(sn8: str | None) -> None:
        # Imported lazily to avoid a circular import (model_profiles imports api).
        from .model_profiles import ModelProfile, resolve_profile

        if resolve_profile(sn8) is not ModelProfile.ATW:
            raise ApiError(
                f"C3 basic control is not supported for model {sn8!r}"
            )

    def _query_c3_body(self, appliance_code: str, body_type: int) -> bytearray:
        """Send a C3 query for ``body_type`` and return the response body."""
        response_hex = self.send_hex_command(
            appliance_code, build_c3_query(body_type),
        )
        _body_type, body = extract_c3_body(parse_hex_response(response_hex))
        if not body or body[0] != body_type:
            raise ApiError(
                f"Unexpected response to C3 query 0x{body_type:02x}: "
                f"{response_hex}"
            )
        return body

    def set_c3_basic(
        self, appliance_code: str, *, sn8: str | None, **changes: Any,
    ) -> dict[str, Any]:
        """Change C3 basic fields with a standard Midea C3 SET.

        Reads a fresh status, applies ``changes`` (validated by
        ``c3_basic.apply_basic_changes``) and writes every field back, so only
        the requested ones change. Nothing is sent when the profile, the
        current frame or the change is not supported.
        """
        from .c3_basic import apply_basic_changes, build_basic_set, decode_c3_basic

        self._require_c3_basic_profile(sn8)
        current = decode_c3_basic(self._query_c3_body(appliance_code, 0x01))
        if current is None:
            raise ApiError(
                "Status frame is not a recognised C3 basic layout; refusing "
                "to send a control command"
            )
        command = build_basic_set(apply_basic_changes(current, **changes))
        _LOGGER.debug("set_c3_basic %s -> %s", changes, command)
        response_hex = self.send_hex_command(appliance_code, command)
        return {"sent": command, "response": response_hex}

    def set_c3_feature(
        self, appliance_code: str, *, sn8: str | None, feature: str, on: bool,
    ) -> dict[str, Any]:
        """Switch the C3 silent or ECO mode, keeping the device's schedule."""
        from .c3_basic import FEATURE_BODY_TYPES, build_feature_set

        self._require_c3_basic_profile(sn8)
        body_type = FEATURE_BODY_TYPES.get(feature)
        if body_type is None:
            raise ValueError(f"Unknown C3 feature {feature!r}")
        command = build_feature_set(
            self._query_c3_body(appliance_code, body_type), on,
        )
        _LOGGER.debug("set_c3_feature %s on=%s -> %s", feature, on, command)
        response_hex = self.send_hex_command(appliance_code, command)
        return {"sent": command, "response": response_hex}

    def _set_device_kjrh120l(
        self,
        appliance_code: str,
        *,
        mode: int | None,
        temperature: int | None,
        power_on: bool,
        zone1_temperature: int | None = None,
        zone1_run_mode: int | None = None,
        zone1_power: bool | None = None,
    ) -> dict[str, Any]:
        """Send a KJRH-120L (sn8 17100003) control command (issue #35).

        This controller rejects the standard C3 SET frame, so a single captured
        short command is sent directly (no status query, no checksum). One of:
          - temperature → DHW setpoint write (``0007 01 <temp> ff``)
          - power_on / mode→heat → DHW power ON (``00020101ff``)
          - mode→off → DHW power OFF (``00020100ff``)
          - zone1_temperature → Zone 1 setpoint (``0004 01 <temp> ff``)
          - zone1_run_mode + zone1_temperature → run mode paired with the
            Zone 1 setpoint, as the app sends it
          - zone1_power → Zone 1 power (``0000 01 <0|1> ff``)

        The caller (climate entity) has already clamped ``temperature`` to the
        DHW range. Temperature takes priority over a same-call mode change.
        A Zone 1 write cannot be combined with any other write; the frame
        builders reject values that do not fit the captured shape.
        """
        # Imported lazily to avoid a circular import (model_profiles imports api).
        from .model_profiles import (
            KJRH120L_DHW_OFF,
            KJRH120L_DHW_ON,
            build_kjrh120l_run_mode,
            build_kjrh120l_set_temperature,
            build_kjrh120l_zone1_power,
            build_kjrh120l_zone1_setpoint,
        )

        effective: dict[str, Any]
        if any(v is not None for v in (zone1_temperature, zone1_run_mode, zone1_power)):
            if temperature is not None or mode is not None or power_on:
                raise ValueError(
                    "A KJRH-120L Zone 1 write cannot be combined with a DHW write"
                )
            if zone1_power is not None:
                if zone1_temperature is not None or zone1_run_mode is not None:
                    raise ValueError(
                        "KJRH-120L Zone 1 power is a separate write from the "
                        "setpoint and run mode"
                    )
                command = build_kjrh120l_zone1_power(zone1_power)
                effective = {"effective_zone1_power": zone1_power}
            elif zone1_run_mode is not None:
                if zone1_temperature is None:
                    raise ValueError(
                        "A KJRH-120L run mode write needs the Zone 1 setpoint "
                        "for the destination mode"
                    )
                command = build_kjrh120l_run_mode(zone1_run_mode, zone1_temperature)
                effective = {
                    "effective_run_mode": zone1_run_mode,
                    "effective_zone1_temp": zone1_temperature,
                }
            else:
                assert zone1_temperature is not None
                command = build_kjrh120l_zone1_setpoint(zone1_temperature)
                effective = {"effective_zone1_temp": zone1_temperature}
            response_hex = self.send_hex_command(appliance_code, command)
            return {"sent": command, "response": response_hex, **effective}

        if temperature is not None:
            command = build_kjrh120l_set_temperature(int(temperature))
            effective = {"effective_temp": int(temperature)}
        elif power_on or (mode is not None and mode != MODE_OFF):
            command = KJRH120L_DHW_ON
            effective = {"effective_power": True}
        else:
            # mode is MODE_OFF (turn off / hvac_mode → off).
            command = KJRH120L_DHW_OFF
            effective = {"effective_power": False}

        response_hex = self.send_hex_command(appliance_code, command)
        return {"sent": command, "response": response_hex, **effective}
