"""sn8-gated model decode profiles facade (#110).

All per-family decode, sensor-routing, query-building, and write-encoding logic
lives in the dedicated dialect modules under ``protocols/``:

- ``protocols/profile.py``: ``ModelProfile`` enum and ``sn8 → profile`` table
- ``protocols/standard.py``: STANDARD default C3 dialect
- ``protocols/aquapura.py``: AQUAPURA (``171000AU``) dialect
- ``protocols/atw.py``: ATW (``171H120F``) dialect (C3Basic, 0x10/0x0A/0x05/0x07/0x04)
- ``protocols/kjrh120l.py``: KJRH-120L (``17100003``) dialect

This module re-exports the public constants and helpers for backwards
compatibility and delegates profile dispatch through ``protocols.get_adapter``.
"""

from __future__ import annotations

from .api import ITSSensors, ITSStatus
from .protocols import get_adapter
from .protocols.atw import (
    ATW_DHW_SETPOINT_INDEX,
    ATW_DHW_TANK_TEMP_INDEX,
    ATW_ECO_MIN_LEN,
    ATW_ECO_SUBTYPE,
    ATW_ENERGY_NOTIFY_MIN_LEN,
    ATW_ENERGY_NOTIFY_SUBTYPE,
    ATW_FLAGS_INDEX,
    ATW_GALMET_ZONE1_SETPOINT_INDEX,
    ATW_HMI_PARAMS_MIN_LEN,
    ATW_HMI_PARAMS_SUBTYPE,
    ATW_SILENT_FULL_LEN,
    ATW_SILENT_MIN_LEN,
    ATW_SILENT_SUBTYPE,
    ATW_SPACE_HEAT_DEMAND_BIT,
    ATW_UNIT_PARAMS_ENERGY_MIN_LEN,
    ATW_UNIT_PARAMS_MIN_LEN,
    ATW_UNIT_PARAMS_SUBTYPE,
    ATW_UNIT_PARAMS_THERMAL_ENERGY_MIN_LEN,
    ATW_ZONE1_SETPOINT_X2_INDEX,
    _ATW_PLACEHOLDER_SENSOR_FIELDS,
    _ATW_PLACEHOLDER_SENSORS_BODY,
    _is_27_byte_atw_status,
    _is_rotenso_atw_status,
    apply_atw_eco,
    apply_atw_energy_notify,
    apply_atw_hmi_params,
    apply_atw_silent,
    apply_atw_unit_params,
    atw_c3_basic,
    atw_sensors_are_placeholder,
    decode_atw_status,
)
from .protocols.kjrh120l import (
    KJRH120L_DHW_OFF,
    KJRH120L_DHW_ON,
    KJRH120L_DHW_SETPOINT_INDEX,
    KJRH120L_DHW_TEMP_MAX,
    KJRH120L_DHW_TEMP_MIN,
    KJRH120L_MODE_OFF,
    KJRH120L_MODE_ON,
    KJRH120L_OUTDOOR_TEMP_INDEX,
    KJRH120L_OUTDOOR_TEMP_MAX,
    KJRH120L_OUTDOOR_TEMP_MIN,
    KJRH120L_OUTDOOR_TEMP_OFFSET,
    KJRH120L_POWER_INDEX,
    KJRH120L_RUN_MODE_COOL,
    KJRH120L_RUN_MODE_HEAT,
    KJRH120L_RUN_MODE_INDEX,
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
    KJRH120L_ZONE1_CAP_INDEX_A,
    KJRH120L_ZONE1_CAP_INDEX_B,
    KJRH120L_ZONE1_COOL_MAX_INDEX,
    KJRH120L_ZONE1_COOL_MIN_INDEX,
    KJRH120L_ZONE1_HEAT_MAX_INDEX,
    KJRH120L_ZONE1_HEAT_MIN_INDEX,
    KJRH120L_ZONE1_POWER_INDEX,
    KJRH120L_ZONE1_POWER_OFF,
    KJRH120L_ZONE1_POWER_ON,
    KJRH120L_ZONE1_SETPOINT_INDEX,
    build_kjrh120l_run_mode,
    build_kjrh120l_set_temperature,
    build_kjrh120l_zone1_power,
    build_kjrh120l_zone1_setpoint,
    decode_kjrh120l_status,
    kjrh120l_has_zone1,
    kjrh120l_mode_change_setpoint,
    kjrh120l_zone1_range,
)
from .protocols.profile import (
    AQUAPURA_SN8,
    ATW_SN8,
    KJRH120L2_SN8,
    KJRH120L_SN8,
    ModelProfile,
    _SN8_PROFILES,
    resolve_profile,
)


def build_query_command(profile: ModelProfile, subtype: int) -> str:
    """Return the cloud query command for a status/sensors subtype."""
    return get_adapter(profile).build_query(subtype)


def apply_profile_to_status(profile: ModelProfile, status: ITSStatus) -> ITSStatus:
    """Return the profile-canonical ITSStatus for a decoded status object."""
    return get_adapter(profile).decode_status(status)


def apply_profile_to_sensors(
    profile: ModelProfile,
    sensors: ITSSensors,
    status: ITSStatus,
) -> ITSSensors:
    """Return the profile-canonical ITSSensors for the decoded sensors object."""
    return get_adapter(profile).decode_sensors(sensors, status)


def describe_profile_variant(
    sn8: str | None,
    status: ITSStatus | None,
) -> str:
    """Return a concise label for the resolved profile and structural variant."""
    return get_adapter(sn8).describe_variant(status)


__all__ = [
    "AQUAPURA_SN8",
    "ATW_DHW_SETPOINT_INDEX",
    "ATW_DHW_TANK_TEMP_INDEX",
    "ATW_ECO_MIN_LEN",
    "ATW_ECO_SUBTYPE",
    "ATW_ENERGY_NOTIFY_MIN_LEN",
    "ATW_ENERGY_NOTIFY_SUBTYPE",
    "ATW_FLAGS_INDEX",
    "ATW_GALMET_ZONE1_SETPOINT_INDEX",
    "ATW_HMI_PARAMS_MIN_LEN",
    "ATW_HMI_PARAMS_SUBTYPE",
    "ATW_SILENT_FULL_LEN",
    "ATW_SILENT_MIN_LEN",
    "ATW_SILENT_SUBTYPE",
    "ATW_SN8",
    "ATW_SPACE_HEAT_DEMAND_BIT",
    "ATW_UNIT_PARAMS_ENERGY_MIN_LEN",
    "ATW_UNIT_PARAMS_MIN_LEN",
    "ATW_UNIT_PARAMS_SUBTYPE",
    "ATW_UNIT_PARAMS_THERMAL_ENERGY_MIN_LEN",
    "ATW_ZONE1_SETPOINT_X2_INDEX",
    "KJRH120L2_SN8",
    "KJRH120L_DHW_OFF",
    "KJRH120L_DHW_ON",
    "KJRH120L_DHW_SETPOINT_INDEX",
    "KJRH120L_DHW_TEMP_MAX",
    "KJRH120L_DHW_TEMP_MIN",
    "KJRH120L_MODE_OFF",
    "KJRH120L_MODE_ON",
    "KJRH120L_OUTDOOR_TEMP_INDEX",
    "KJRH120L_OUTDOOR_TEMP_MAX",
    "KJRH120L_OUTDOOR_TEMP_MIN",
    "KJRH120L_OUTDOOR_TEMP_OFFSET",
    "KJRH120L_POWER_INDEX",
    "KJRH120L_RUN_MODE_COOL",
    "KJRH120L_RUN_MODE_HEAT",
    "KJRH120L_RUN_MODE_INDEX",
    "KJRH120L_SN8",
    "KJRH120L_TEMP_MAX",
    "KJRH120L_TEMP_MIN",
    "KJRH120L_ZONE1_CAP_INDEX_A",
    "KJRH120L_ZONE1_CAP_INDEX_B",
    "KJRH120L_ZONE1_COOL_MAX_INDEX",
    "KJRH120L_ZONE1_COOL_MIN_INDEX",
    "KJRH120L_ZONE1_HEAT_MAX_INDEX",
    "KJRH120L_ZONE1_HEAT_MIN_INDEX",
    "KJRH120L_ZONE1_POWER_INDEX",
    "KJRH120L_ZONE1_POWER_OFF",
    "KJRH120L_ZONE1_POWER_ON",
    "KJRH120L_ZONE1_SETPOINT_INDEX",
    "ModelProfile",
    "_ATW_PLACEHOLDER_SENSORS_BODY",
    "_ATW_PLACEHOLDER_SENSOR_FIELDS",
    "_SN8_PROFILES",
    "_is_27_byte_atw_status",
    "_is_rotenso_atw_status",
    "apply_atw_eco",
    "apply_atw_energy_notify",
    "apply_atw_hmi_params",
    "apply_atw_silent",
    "apply_atw_unit_params",
    "apply_profile_to_sensors",
    "apply_profile_to_status",
    "atw_c3_basic",
    "atw_sensors_are_placeholder",
    "build_kjrh120l_run_mode",
    "build_kjrh120l_set_temperature",
    "build_kjrh120l_zone1_power",
    "build_kjrh120l_zone1_setpoint",
    "build_query_command",
    "decode_atw_status",
    "decode_kjrh120l_status",
    "describe_profile_variant",
    "kjrh120l_has_zone1",
    "kjrh120l_mode_change_setpoint",
    "kjrh120l_zone1_range",
    "resolve_profile",
]
