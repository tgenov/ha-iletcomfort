"""KJRH-120L (sn8 17100003): the 0x02 sensors frame is not live telemetry.

On every KJRH-120L capture the 0x02 response is a static block (mostly
zeroes plus a repeating ``32,00,00,00,00`` trailer), and in one capture
(#142 keramys v0.18.3) it carried a stray ``03,64,08`` that decoded as
refrigerant 100 °C / plate HX 8 °C. No field of it has ever matched the
official app, so nothing decoded from it may reach an entity; only the
status-frame outdoor temperature (body[57] - 196) is published as ``t4``.
The status frame also has no confirmed compressor-running signal, so
``comp_running`` is unknown (``None``), never ``False`` (#231).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from custom_components.iletcomfort.api import (
    ITSSensors,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.protocols import build_snapshot, get_adapter

KJRH_SN8 = "17100003"

# 100-byte 0x02 frame shared by #131, #136 and #142 (v0.22.0).
_STATIC_0X02_100 = (
    "02,fe,00,46,00,9f,00,5a,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,32,00,00,00,00,"
    "32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,ff"
)

# (case id, 0x01 status, 0x02 sensors, expected outdoor °C from body[57]).
REPORTER_FRAMES: list[tuple[str, str, str, float]] = [
    (
        "issue131_airwell",
        "01,fe,00,00,00,41,00,55,00,00,00,03,23,1e,30,14,00,00,00,00,00,00,01,00,00,"
        "00,00,00,00,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,22,46,14,dd,"
        "00,01,01,06,03,01,01,cf,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,00,00,00,00,1a,0a,11,12,38,00,00,00,00,ff",
        _STATIC_0X02_100,
        11.0,
    ),
    (
        "issue136_mundoclima",
        "01,fe,00,00,00,41,00,55,01,01,00,03,32,1e,30,28,00,00,00,00,00,00,01,00,00,"
        "00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,22,46,14,dd,"
        "00,01,00,07,03,01,01,ce,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,00,00,00,00,1a,0a,1c,17,03,00,00,00,00,ff",
        _STATIC_0X02_100,
        10.0,
    ),
    (
        "issue142_keramys_v0183",
        "01,fe,00,00,00,42,00,56,01,01,00,03,32,1e,30,32,00,00,00,00,00,00,01,00,00,"
        "00,00,00,00,00,00,00,00,00,01,02,02,4b,23,19,05,2d,19,19,05,3c,22,46,14,dd,"
        "00,01,01,06,03,01,01,ce,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,00,00,00,00,1a,0b,0f,0a,17,00,00,00,00,00,ff",
        # Static block but with 03,64,08 at body[14:17] (decoded tf=100, tp=8).
        (
            "02,fe,00,46,00,9f,00,5a,00,00,00,00,00,00,03,64,08,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,32,00,00,00,00,"
            "32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,ff"
        ),
        10.0,
    ),
    (
        "issue142_keramys_v0220",
        "01,fe,00,00,00,42,00,56,01,01,00,03,2d,1e,30,32,01,00,00,00,00,01,01,00,00,"
        "00,00,00,00,00,00,01,00,00,01,02,02,4b,23,19,05,2d,19,19,05,3c,22,46,14,dd,"
        "00,01,01,09,03,01,01,ca,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,01,00,00,00,1a,0b,19,17,1f,00,00,00,00,00,ff",
        _STATIC_0X02_100,
        6.0,
    ),
    (
        "issue119_airwell_a",
        "01,fe,00,00,00,41,00,55,01,01,00,03,22,1e,30,29,00,00,00,00,00,00,01,01,00,"
        "00,00,00,00,00,01,00,00,00,02,02,02,37,19,19,05,37,19,19,05,3c,22,46,14,dd,"
        "00,01,00,04,03,01,01,cd,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,00,00,00,00,1a,09,16,07,12,00,00,00,00,ff",
        (
            "02,fe,00,46,00,9f,00,5a,01,03,4a,04,1e,00,00,00,00,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,32,00,00,00,00,"
            "32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,ff"
        ),
        9.0,
    ),
    (
        "issue119_airwell_b",
        "01,fe,00,00,00,41,00,55,01,01,00,03,22,1e,30,29,00,00,00,00,00,00,01,01,00,"
        "00,00,00,00,00,01,00,00,00,02,02,02,37,19,19,05,37,19,19,05,3c,22,46,14,dd,"
        "00,01,00,04,03,01,01,d9,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,30,ff,ff,ff,ff,"
        "00,00,00,01,00,00,00,00,00,1a,09,16,10,25,00,00,00,00,ff",
        (
            "02,fe,00,46,00,9f,00,5a,01,03,4a,04,00,00,00,00,00,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
            "00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,32,00,00,00,00,"
            "32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,00,00,32,00,00,ff"
        ),
        21.0,
    ),
]


def _hex_csv(csv: str) -> bytes:
    return bytes(int(part, 16) for part in csv.split(","))


def _corpus_frames() -> list[tuple[str, str, str, float]]:
    path = Path(__file__).parent / "corpus" / "kjrh120l_17100003.json"
    cases: list[dict[str, Any]] = json.loads(path.read_text())
    return [
        (
            case["id"],
            case["frames"]["0x01"],
            case["frames"]["0x02"],
            case["expected_status"]["kjrh120l_outdoor_temp"],
        )
        for case in cases
    ]


ALL_FRAMES = _corpus_frames() + REPORTER_FRAMES

# Every 0x02-derived value that an entity, attribute or device-info field reads.
_UNKNOWN_STATE_PROPERTIES = (
    "dhw_tank_temp",
    "refrigerant_temp",
    "plate_hx_temp",
    "water_inlet_temp",
    "water_outlet_temp",
    "condenser_temp",
    "evaporator_temp",
    "odu_voltage",
    "odu_current",
    "water_flow_m3h",
    "dc_bus_voltage",
    "dc_current",
    "odu_firmware_version",
    "climate_current_temperature",
    "compressor_running",
)


def _decode(status_csv: str, sensors_csv: str) -> tuple[Any, Any]:
    adapter = get_adapter(KJRH_SN8)
    status = adapter.decode_status(decode_its_status(bytearray(_hex_csv(status_csv))))
    raw_sensors = _hex_csv(sensors_csv)
    sensors = decode_its_sensors(bytearray(raw_sensors))
    state = build_snapshot(adapter, status, sensors, {0x01: _hex_csv(status_csv), 0x02: raw_sensors})
    return adapter, state


@pytest.mark.parametrize(
    ("case_id", "status_csv", "sensors_csv", "outdoor"),
    ALL_FRAMES,
    ids=[frame[0] for frame in ALL_FRAMES],
)
def test_kjrh120l_publishes_nothing_from_static_0x02(
    case_id: str, status_csv: str, sensors_csv: str, outdoor: float
) -> None:
    _adapter, state = _decode(status_csv, sensors_csv)

    for prop in _UNKNOWN_STATE_PROPERTIES:
        assert getattr(state, prop) is None, f"{case_id}: {prop}"
    assert state.status.comp_running is None, case_id

    # The validated status-frame outdoor temperature is kept.
    assert state.outdoor_temp == outdoor, case_id
    assert state.sensors.t4_temp == outdoor, case_id
    # The raw 0x02 body stays available to diagnostics.
    assert state.sensors.raw_body == _hex_csv(sensors_csv), case_id


@pytest.mark.parametrize(
    ("case_id", "status_csv", "sensors_csv", "outdoor"),
    ALL_FRAMES,
    ids=[frame[0] for frame in ALL_FRAMES],
)
def test_kjrh120l_decode_sensors_is_an_allowlist(
    case_id: str, status_csv: str, sensors_csv: str, outdoor: float
) -> None:
    """Every decoded 0x02 field reverts to its 'not reported' default."""
    adapter = get_adapter(KJRH_SN8)
    status = adapter.decode_status(decode_its_status(bytearray(_hex_csv(status_csv))))
    raw_sensors = _hex_csv(sensors_csv)
    out = adapter.decode_sensors(decode_its_sensors(bytearray(raw_sensors)), status)

    expected = ITSSensors(raw_body=raw_sensors, t4_temp=outdoor)
    for field in dataclasses.fields(ITSSensors):
        assert getattr(out, field.name) == getattr(expected, field.name), (
            f"{case_id}: sensors.{field.name}"
        )


def test_kjrh120l_status_keeps_validated_fields() -> None:
    """Zone-1 / DHW setpoints and power are untouched by the fix (#142 v0.22.0)."""
    _, status_csv, sensors_csv, _ = next(
        f for f in REPORTER_FRAMES if f[0] == "issue142_keramys_v0220"
    )
    _adapter, state = _decode(status_csv, sensors_csv)
    kjrh = state.status.kjrh120l
    assert kjrh.zone1_power is True
    assert kjrh.run_mode == 3
    assert kjrh.zone1_setpoint == 45.0
    assert kjrh.dhw_setpoint == 50.0
    assert kjrh.outdoor_temp == 6.0


def test_kjrh120l_outdoor_absent_leaves_t4_unknown() -> None:
    """A status frame too short for body[57] gives no outdoor temperature."""
    adapter = get_adapter(KJRH_SN8)
    short = _hex_csv(REPORTER_FRAMES[0][1])[:40]
    status = adapter.decode_status(decode_its_status(bytearray(short)))
    out = adapter.decode_sensors(
        decode_its_sensors(bytearray(_hex_csv(_STATIC_0X02_100))), status
    )
    assert out.t4_temp is None
    assert out.th_temp is None
