"""Domain-named HeatPumpState properties and unknown-vs-zero semantics (#111).

Entities read only these properties. A value the dialect did not decode is
``None`` (unknown), never a placeholder ``0``; cumulative TOTAL_INCREASING
counters in particular must never read 0 in place of unknown.
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorStateClass
import pytest

from custom_components.iletcomfort.api import (
    ITSSensors,
    ITSStatus,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.iletcomfort.model_profiles import (
    ATW_SN8,
    KJRH120L_SN8,
    decode_atw_status,
    decode_kjrh120l_status,
)
from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B
from tests.snapshot_helpers import make_state
from tests.test_atw_unit_params import NORDIS_UNIT_PARAMS_0X10
from tests.test_c3_basic import ROTENSO_BASELINE, _body
from tests.test_model_profiles import (
    ATW_PLACEHOLDER_SENSORS_BODY,
    ISSUE_11_SENSORS_BODY,
    ISSUE_11_STATUS_BODY,
    NORDIS_ATW_STATUS_FRAMES,
)

_BY_KEY = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}
_TOTAL_INCREASING_KEYS = [
    desc.key
    for desc in SENSOR_DESCRIPTIONS
    if desc.state_class is SensorStateClass.TOTAL_INCREASING
]


def test_standard_issue11_domain_properties_match_the_standard_decode() -> None:
    """STANDARD values are unchanged: each property is the decoded wire field."""
    status = decode_its_status(ISSUE_11_STATUS_BODY)
    sensors = decode_its_sensors(ISSUE_11_SENSORS_BODY)
    state = make_state(status, sensors)

    assert state.water_inlet_temp == sensors.twin_temp
    assert state.water_outlet_temp == sensors.twout_temp
    assert state.dhw_tank_temp == sensors.th_temp
    assert state.outdoor_temp == sensors.t4_temp
    assert state.condenser_temp == sensors.t3_temp
    assert state.evaporator_temp == sensors.t2_temp
    assert state.refrigerant_temp == sensors.tf_temp
    assert state.plate_hx_temp == sensors.tp_temp
    assert state.odu_voltage == sensors.odu_voltage
    assert state.odu_current == 4.0
    assert state.compressor_frequency == 51
    assert state.compressor_running is True
    assert state.total_energy_kwh == status.total_kwh is not None
    assert state.compressor_run_hours == status.comp_total_run_hours is not None
    assert state.high_pressure == status.pressure_h is not None
    assert state.low_pressure == status.pressure_l is not None
    assert state.error_code == 0
    assert state.has_error is False
    assert state.operating_mode == status.mode
    # STANDARD has no 0x10 frame: the ATW-only telemetry stays unknown.
    assert state.exv_opening is None
    assert state.water_flow_m3h is None
    assert state.total_thermal_energy_kwh is None
    # STANDARD reports no DHW-target entity value.
    assert state.dhw_target_temp is None


def test_short_standard_status_reports_unknown_not_zero() -> None:
    """A status frame too short to carry telemetry no longer reads as 0."""
    state = make_state(decode_its_status(bytearray([0x01] + [0] * 30)), ITSSensors())

    assert state.compressor_frequency is None
    assert state.total_energy_kwh is None
    assert state.compressor_run_hours is None
    assert state.high_pressure is None
    assert state.low_pressure is None
    for key in ("compressor_freq", "total_energy", "comp_run_hours", "pressure_high", "pressure_low"):
        assert _BY_KEY[key].value_fn(state) is None, key


def test_long_standard_status_keeps_decoded_zero() -> None:
    """A frame that carries the fields still reports a genuine 0."""
    state = make_state(decode_its_status(bytearray([0x01] + [0] * 49)), ITSSensors())

    assert state.compressor_frequency == 0
    assert state.total_energy_kwh == 0
    assert state.compressor_run_hours == 0
    assert state.high_pressure == 0
    assert state.low_pressure == 0


def test_its_sensors_defaults_are_unknown_until_decoded() -> None:
    blank = ITSSensors()
    for field in (
        "tf_temp",
        "tp_temp",
        "th_temp",
        "odu_current",
        "odu_voltage",
        "water_flow",
        "dc_current",
        "dc_voltage",
    ):
        assert getattr(blank, field) is None, field

    decoded = decode_its_sensors(ISSUE_11_SENSORS_BODY)
    for field in ("tf_temp", "tp_temp", "th_temp", "odu_current", "odu_voltage", "water_flow", "dc_current", "dc_voltage"):
        assert getattr(decoded, field) is not None, field


def test_its_status_telemetry_defaults_are_unknown() -> None:
    blank = ITSStatus()
    for field in (
        "exv_drg",
        "pressure_h",
        "pressure_l",
        "comp_frq",
        "total_kwh",
        "total_thermal_kwh",
        "comp_total_run_hours",
    ):
        assert getattr(blank, field) is None, field


def test_kjrh120l_compressor_and_counters_are_unknown() -> None:
    state = make_state(
        decode_kjrh120l_status(KJRH120L_SNAPSHOT_A), ITSSensors(), sn8=KJRH120L_SN8
    )

    assert state.compressor_frequency is None
    assert _BY_KEY["compressor_freq"].value_fn(state) is None
    assert state.total_energy_kwh is None
    assert state.compressor_run_hours is None
    assert state.compressor_running is False


@pytest.mark.parametrize(
    "state",
    [
        make_state(decode_its_status(bytearray([0x01] + [0] * 30)), ITSSensors()),
        make_state(
            decode_kjrh120l_status(KJRH120L_SNAPSHOT_A), ITSSensors(), sn8=KJRH120L_SN8
        ),
        make_state(
            decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"]),
            decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
            sn8=ATW_SN8,
        ),
    ],
    ids=["standard_short", "kjrh120l", "atw_placeholder_no_0x10"],
)
def test_total_increasing_sensors_never_report_zero_for_unknown(state) -> None:
    assert _TOTAL_INCREASING_KEYS
    for key in _TOTAL_INCREASING_KEYS:
        assert _BY_KEY[key].value_fn(state) is None, key


def test_atw_0x10_telemetry_routes_to_domain_properties() -> None:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    state = make_state(
        status,
        decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY),
        {0x02: ATW_PLACEHOLDER_SENSORS_BODY, 0x10: NORDIS_UNIT_PARAMS_0X10},
        sn8=ATW_SN8,
    )

    assert state.exv_opening == 480
    assert state.high_pressure == 1220
    assert state.low_pressure is None  # 0x10 does not carry it
    assert state.water_flow_m3h == 0.61
    assert state.dc_bus_voltage == 320
    assert state.dc_current == 0
    assert state.total_energy_kwh == 6463
    assert state.water_outlet_temp == 24.0
    assert state.outdoor_temp == 14.0
    assert state.compressor_frequency == 0
    assert state.compressor_running is False


def test_silent_and_eco_routing() -> None:
    # Legacy STANDARD: ctrl_flag 1 = mute, mute_level 1 = Level 2.
    legacy = make_state(ITSStatus(mode=1), ITSSensors(ctrl_flag=1, mute_level=1))
    assert legacy.silent_level == 2
    assert legacy.silent_enabled is True
    assert legacy.eco_enabled is None
    assert legacy.reported_silent_level is None
    assert make_state(ITSStatus(), ITSSensors(ctrl_flag=2)).boost_enabled is True
    assert make_state(ITSStatus(), ITSSensors(ctrl_flag=2)).silent_level == 0

    # ATW without 0x05/0x07: the C3 basic flags.
    atw_status = decode_atw_status(_body(ROTENSO_BASELINE))
    atw = make_state(atw_status, ITSSensors(), sn8=ATW_SN8)
    basic = atw_status.c3_basic
    assert basic is not None
    assert atw.silent_level == (1 if basic.silent_mode else 0)
    assert atw.silent_enabled is basic.silent_mode
    assert atw.eco_enabled is basic.eco_mode
    assert atw.reported_silent_level is None

    # ATW with a Super Silent 0x05 and an ECO-on 0x07 frame.
    overlaid = make_state(
        atw_status,
        ITSSensors(),
        {
            0x01: atw_status.raw_body,
            0x05: b"\x05\x09\x00\x00\x00\x00\x00\x00\x00\x00",
            0x07: b"\x07\x01\x00\x00\x00\x00\x00",
        },
        sn8=ATW_SN8,
    )
    assert overlaid.silent_level == 2
    assert overlaid.reported_silent_level == 2
    assert overlaid.silent_enabled is True
    assert overlaid.eco_enabled is True


def test_zone_and_dhw_routing_per_dialect() -> None:
    dual_status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A)
    dual = make_state(dual_status, ITSSensors(), sn8=KJRH120L_SN8)
    assert dual.zone1_target_temp == dual_status.kjrh120l_zone1_setpoint == 50.0
    assert dual.zone1_run_mode == "heat"
    assert dual.zone1_power is dual_status.kjrh120l_zone1_power
    assert dual.dhw_power is dual_status.kjrh120l_dhw_power
    assert dual.dhw_target_temp == dual_status.kjrh120l_dhw_setpoint
    assert dual.zone1_target_range is not None

    # Zone 1 off: the DHW setpoint sits in t5s_def (pure-DHW behavior).
    off_status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_B)
    off = make_state(off_status, ITSSensors(), sn8=KJRH120L_SN8)
    assert off_status.kjrh120l_dhw_setpoint is None
    assert off_status.t5s_def is not None
    assert off.dhw_target_temp == off_status.t5s_def

    atw_status = decode_atw_status(_body(ROTENSO_BASELINE))
    basic = atw_status.c3_basic
    assert basic is not None
    atw = make_state(atw_status, ITSSensors(), sn8=ATW_SN8)
    assert atw.dhw_target_temp == float(basic.dhw_target)
    assert atw.dhw_power is basic.dhw_power
    assert atw.dhw_tank_temp == float(basic.dhw_tank_temp)
    assert atw.dhw_target_range == (float(basic.dhw_min), float(basic.dhw_max))
    assert atw.zone1_power is basic.zone1_power
    assert atw.zone1_curve_enabled is basic.zone1_curve
    assert atw.fast_dhw_enabled is basic.fast_dhw
    assert atw.zone1_run_mode is None

    standard = make_state(ITSStatus(mode=1, t5s_def=None, set_temperature=42), ITSSensors())
    assert standard.dhw_power is None
    assert standard.zone1_power is None
    assert standard.dhw_target_range is None
    assert standard.active_setpoint == 42.0


def test_atw_non_c3_25_byte_status_reports_unknown_compressor_state() -> None:
    """Neither C3 basic nor the 27-byte layout: running state is unknown, not False."""
    body = bytearray(25)
    body[0] = 0x01
    body[4] = 0x00  # not C3 heat (3) / cool (2): no C3 basic match
    body[9] = 44  # Zone-1 setpoint x2
    body[22] = 45  # DHW tank
    status = decode_atw_status(bytes(body))
    assert status.c3_basic is None

    state = make_state(status, ITSSensors(), sn8=ATW_SN8)
    assert state.compressor_running is None
    assert state.compressor_frequency is None
    assert state.active_setpoint == 22.0
    binary = next(d for d in BINARY_SENSOR_DESCRIPTIONS if d.key == "compressor_running")
    assert binary.is_on_fn(state) is None

    # The C3 basic and 27-byte layouts were already unknown and stay so.
    for frame in (_body(ROTENSO_BASELINE), NORDIS_ATW_STATUS_FRAMES["issue132"]):
        assert decode_atw_status(frame).comp_running is None
    # A 0x10 overlay still supplies a real running state.
    with_0x10 = make_state(
        status,
        ITSSensors(),
        {0x01: status.raw_body, 0x10: NORDIS_UNIT_PARAMS_0X10},
        sn8=ATW_SN8,
    )
    assert with_0x10.compressor_running is False
    assert with_0x10.compressor_frequency == 0
