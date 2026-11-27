"""KJR-86T3 table dialect (sn8 17186T3A, AQS Energie AQUAPURA variant, #126).

Real reply bodies captured from one unit; the ODU serial in table 1000 is
zeroed. Values were cross-checked against the official app at the same moment.
The write frames are the exact ones validated on that unit.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.iletcomfort.api import ILetComfortClient
from custom_components.iletcomfort.cloud import ApiError
from custom_components.iletcomfort.commands import (
    SetBoostCommand,
    SetDhwTargetCommand,
    SetHvacModeCommand,
    SetPowerCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
)
from custom_components.iletcomfort.protocols import (
    UNSUPPORTED,
    ViewContext,
    WriteContext,
    WriteRefused,
    build_snapshot,
    get_adapter,
    run_write_plan,
)
from custom_components.iletcomfort.protocols.kjr86t3 import (
    build_kjr86t3_settings_write,
    build_kjr86t3_table_query,
    decode_kjr86t3_sensors,
    decode_kjr86t3_status,
    kjr86t3_table_matches,
)
from custom_components.iletcomfort.protocols.profile import (
    KJR86T3_SN8,
    ModelProfile,
    resolve_profile,
)

TABLE_500 = bytes.fromhex(
    "0001f4ffff83ff00ffffff15ff0140022600ffff01ffff00ff000032ffffffff"
    "ffffff00"
)
TABLE_900 = bytes.fromhex(
    "000384ffff83ff00ffffff150000000000ff000001ff0000000000ffffff023f"
    "2f0000000000000000000000ffffffffffffffff"
)
TABLE_1000 = bytes.fromhex(
    "0003e8ffff83ff00ffffff0fffff00000000ff01e0ffffffff00000000ffff00"
    "eaffffffff0000ffffffff00e400b100a700c1ffffffffffff7fff7fff7fffff"
    "ffffffffff0000ffffffffffff003200647fff00000000cff4ffffffffffffff"
    "ffffffffffffffffff0000028d0000028d00000000ffffffff00ffffff000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000056380000000000000000000000000000ffff00"
    "00"
)

ADAPTER = get_adapter(KJR86T3_SN8)


def _frame(body: bytes) -> str:
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x03])
    return (header + body + b"\x00").hex()


def _client() -> ILetComfortClient:
    return ILetComfortClient(api_base="https://eu.dollin.net")


def _replies() -> dict[str, str]:
    return {
        build_kjr86t3_table_query(500): _frame(TABLE_500),
        build_kjr86t3_table_query(900): _frame(TABLE_900),
        build_kjr86t3_table_query(1000): _frame(TABLE_1000),
    }


def _state(settings: bytes = TABLE_500):
    status = decode_kjr86t3_status(settings, TABLE_1000)
    sensors = decode_kjr86t3_sensors(TABLE_1000, TABLE_900)
    return build_snapshot(ADAPTER, status, sensors, {0x01: settings})


# -- selection and reads --------------------------------------------------------


def test_sn8_selects_the_kjr86t3_dialect() -> None:
    assert resolve_profile(KJR86T3_SN8) is ModelProfile.KJR86T3
    assert ADAPTER.profile is ModelProfile.KJR86T3


def test_table_query_matches_hardware_validated_frame() -> None:
    # The device only answers the zero-padded 16-byte body; this exact frame
    # returned table 500 on hardware.
    assert (
        build_kjr86t3_table_query(500)
        == "aa1ac3000000000000030101f4000000000000000000000000002a"
    )


def test_table_match_rejects_echoed_request() -> None:
    echoed = bytes.fromhex(build_kjr86t3_table_query(500))[10:-1]
    assert not kjr86t3_table_matches(echoed, 500)
    assert kjr86t3_table_matches(TABLE_500, 500)
    assert not kjr86t3_table_matches(TABLE_500, 900)


def test_status_decode() -> None:
    status = decode_kjr86t3_status(TABLE_500, TABLE_1000)
    assert status.mode == 1
    assert status.t5s_def == 55.0
    assert status.set_temperature == 55
    assert status.total_kwh == 532.36
    assert status.error_code == 0
    assert status.mute_valid is False
    # Not exposed by this controller: unknown rather than a fake 0/False.
    assert status.comp_running is None
    assert status.comp_frq is None


def test_status_decode_power_off_and_silence() -> None:
    body = bytearray(TABLE_500)
    body[13] = 0x00
    body[17] = 0x01
    status = decode_kjr86t3_status(body, TABLE_1000)
    assert status.mode == 0
    assert status.mode_name == "Off"
    assert status.mute_valid is True


def test_zero_energy_counter_stays_unknown() -> None:
    params = bytearray(TABLE_1000)
    params[85:89] = b"\x00\x00\x00\x00"
    assert decode_kjr86t3_status(TABLE_500, params).total_kwh is None


def test_sensors_decode() -> None:
    sensors = decode_kjr86t3_sensors(TABLE_1000, TABLE_900)
    assert sensors.th_temp == 57.5  # DHW tank, routed to the tank sensor
    assert sensors.t4_temp == 19.3
    assert sensors.t3_temp == 16.7
    assert sensors.tp_temp == 22.8
    assert sensors.odu_voltage == 234
    assert sensors.twin_temp is None
    assert sensors.tf_temp is None


def test_client_queries_route_to_table_reads() -> None:
    client = _client()
    replies = _replies()
    with patch.object(
        client, "send_hex_command", side_effect=lambda _code, cmd: replies[cmd]
    ) as send:
        status = client.query_status("APPL1", sn8=KJR86T3_SN8)
        sensors = client.query_sensors("APPL1", sn8=KJR86T3_SN8)
    assert status.t5s_def == 55.0
    assert sensors.th_temp == 57.5
    sent = [call.args[1] for call in send.call_args_list]
    assert sent == [
        build_kjr86t3_table_query(500),
        build_kjr86t3_table_query(1000),
        build_kjr86t3_table_query(1000),
        build_kjr86t3_table_query(900),
    ]


def test_echoed_table_query_raises() -> None:
    client = _client()
    with patch.object(
        client, "send_hex_command", side_effect=lambda _code, cmd: cmd
    ), pytest.raises(ApiError):
        client.query_status("APPL1", sn8=KJR86T3_SN8)


# -- projections ------------------------------------------------------------------


def test_snapshot_projects_tank_setpoint_and_silence() -> None:
    state = _state()
    assert state.climate_current_temperature == 57.5
    assert state.dhw_tank_temp == 57.5
    assert state.active_setpoint == 55.0
    assert state.silent_level == 0
    assert state.boost_enabled is False

    silent = bytearray(TABLE_500)
    silent[17] = 0x01
    assert _state(bytes(silent)).silent_level == 1


def test_climate_view_is_dhw_setpoint_only() -> None:
    view = ADAPTER.climate_view(_state().status, ViewContext())
    assert view.hvac_mode == "heat"
    assert view.hvac_modes == ("off", "heat")
    assert view.target_temperature == 55.0
    assert (view.min_temp, view.max_temp) == (38.0, 70.0)
    assert view.supports_target_temperature is True
    assert view.supports_turn_on_off is False


def test_entity_capabilities_expose_silent_but_not_boost() -> None:
    caps = ADAPTER.entity_capabilities(_state().status, ViewContext())
    assert caps.mute.exists and caps.mute.writable
    assert caps.boost == UNSUPPORTED
    assert caps.dhw_water_heater == UNSUPPORTED


# -- writes -----------------------------------------------------------------------


def test_settings_write_matches_hardware_frames() -> None:
    # These exact frames moved the setpoint 55 -> 54 -> 55 C on the device.
    assert build_kjr86t3_settings_write(setpoint=55) == (
        "aa2ec3000000000000020001f4ffff83ff00ffffff0226"
        "ffffffffffffffffffffffffffffffffffffffffffffff8a"
    )
    frame = bytes.fromhex(build_kjr86t3_settings_write(setpoint=54, silence=False))
    body = frame[10:-1]
    assert len(body) == 36
    assert body[11:14] == bytes([0x02, 0x1C, 0x00])  # setpoint 54.0, silence off
    assert sum(frame[1:]) & 0xFF == 0


def test_settings_write_rejects_out_of_range() -> None:
    with pytest.raises(ValueError):
        build_kjr86t3_settings_write(setpoint=75)
    with pytest.raises(ValueError):
        build_kjr86t3_settings_write()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SetTargetTemperatureCommand(54), build_kjr86t3_settings_write(setpoint=54)),
        (SetDhwTargetCommand(54.4), build_kjr86t3_settings_write(setpoint=54)),
        # Clamped to the table-100 range the climate entity shows.
        (SetTargetTemperatureCommand(80), build_kjr86t3_settings_write(setpoint=70)),
        (SetSilentModeCommand(level=1), build_kjr86t3_settings_write(silence=True)),
        (SetSilentModeCommand(level=0), build_kjr86t3_settings_write(silence=False)),
        (SetSilentModeCommand(enabled=True), build_kjr86t3_settings_write(silence=True)),
    ],
)
def test_plan_write(command, expected) -> None:
    plan = ADAPTER.plan_write(command, WriteContext(status=_state().status))
    assert plan.command == expected


@pytest.mark.parametrize(
    "command",
    [
        SetPowerCommand(True),
        SetHvacModeCommand("off"),
        SetBoostCommand(True),
        SetSilentModeCommand(level=2),
    ],
)
def test_unvalidated_writes_are_refused(command) -> None:
    with pytest.raises(WriteRefused):
        ADAPTER.plan_write(command, WriteContext(status=_state().status))


def test_write_accepts_table_reply_and_rejects_echo() -> None:
    plan = ADAPTER.plan_write(SetTargetTemperatureCommand(54), WriteContext())
    result = run_write_plan(plan, lambda _cmd: _frame(TABLE_500))
    assert result["sent"] == build_kjr86t3_settings_write(setpoint=54)

    with pytest.raises(ApiError):
        run_write_plan(plan, lambda cmd: cmd)
