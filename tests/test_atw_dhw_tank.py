"""ATW DHW tank temperature is unavailable when the unit reports no DHW (#138).

The C3 basic status carries the tank reading at ``body[22]`` and the DHW
capability at ``body[2] & 0x04`` (``cap_dhw``). Units without DHW still fill
``body[22]``: the Italtherm Hydrablock 12 M (#138) sends ``0xf1`` (241 °C),
and five other no-DHW captures (#47 Dantex DN-30CEV/AG, whose reporter says it
has no DHW tank; #123 Kaisai; #124 "western"; #139 Airwell Wellea Split; #142)
all send a constant ``0x19`` (25 °C). The byte is not a
tank reading on those units, so the "DHW Tank Temperature" sensor, the water
heater (not created anyway) and the climate fallback must all see ``None``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.iletcomfort.api import (
    ITSSensors,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.protocols import (
    ViewContext,
    build_snapshot,
    get_adapter,
    project_entity_capabilities,
)
from custom_components.iletcomfort.protocols.atw import (
    apply_atw_unit_params,
    decode_atw_status,
)
from custom_components.iletcomfort.protocols.profile import ATW_SN8
from tests.test_atw_unit_params import NORDIS_UNIT_PARAMS_0X10


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


# Italtherm Hydrablock 12 M (#138) diagnostics, v0.18.3: body[2]=0x73 (no
# 0x04 DHW capability), body[22]=0xf1 → shown as "DHW Tank Temperature 241 °C".
ISSUE138_STATUS = _bytes(
    "01,00,73,90,03,03,23,1e,31,78,3c,23,19,05,37,19,19,05,3c,22,3c,28,f1,00,00"
)
ISSUE138_SENSORS = _bytes(
    "02,00,03,00,00,00,00,00,03,00,00,00,00,00,03,00,00,00,00,00,03,00,00,00,"
    "00,00,03,00,00,00,00,00,03,00,00,00,00,00"
)
# Kaisai KMK-100RY (#123): body[2]=0x11 (no DHW), body[22]=0x19 (25 °C).
ISSUE123_STATUS = _bytes(
    "01,08,11,50,03,03,23,28,32,30,41,23,19,05,41,23,19,05,3c,22,3c,14,19,00,80"
)
# Airwell Wellea Split (#139): 27-byte, body[2]=0x73 (no DHW), body[22]=0x19.
ISSUE139_STATUS = _bytes(
    "01,09,73,90,03,03,28,1e,32,30,41,23,19,05,37,19,19,05,3c,22,3c,14,19,00,e0,05,00"
)

# Dantex DN-30CEV/AG (#47), reporter: "no DHW tank". body[2]=0x13, Cool mode.
ISSUE47_STATUS = _bytes(
    "01,01,13,00,02,02,17,17,32,30,41,23,19,05,37,19,19,05,3c,22,3c,14,19,00,80"
)

NO_DHW_FRAMES = {
    "issue47_dantex_no_tank": ISSUE47_STATUS,
    "issue138_italtherm_hydrablock": ISSUE138_STATUS,
    "issue123_kaisai_kmk100ry": ISSUE123_STATUS,
    "issue139_airwell_split": ISSUE139_STATUS,
}


@pytest.mark.parametrize("name", list(NO_DHW_FRAMES))
def test_no_dhw_unit_reports_tank_unavailable(name: str) -> None:
    status = decode_atw_status(NO_DHW_FRAMES[name])

    assert status.c3_basic is not None
    assert status.c3_basic.cap_dhw is False
    assert status.box_bottom_temp is None
    # The raw C3 field is still decoded (diagnostics), it is just not a tank.
    assert status.c3_basic.dhw_tank_temp == NO_DHW_FRAMES[name][22]


def test_issue138_snapshot_hides_241c_tank_everywhere() -> None:
    adapter = get_adapter(ATW_SN8)
    status = adapter.decode_status(decode_its_status(ISSUE138_STATUS))
    sensors = decode_its_sensors(ISSUE138_SENSORS)
    state = build_snapshot(
        adapter, status, sensors, {0x01: ISSUE138_STATUS, 0x02: ISSUE138_SENSORS}
    )

    assert state.sensors is not None
    assert state.sensors.th_temp is None
    assert state.dhw_tank_temp is None  # "DHW Tank Temperature" sensor
    assert adapter.climate_current_temperature(state.sensors) is None
    caps = project_entity_capabilities(ATW_SN8, state, ViewContext())
    assert caps.dhw_water_heater.exists is False


def test_no_dhw_unit_discards_non_placeholder_sensor_th_temp() -> None:
    """A live (non-template) 0x02 th_temp must not leak through as a tank reading.

    Climate current temperature falls back to th_temp for non-placeholder ATW
    sensor frames, so it must be None too.
    """
    adapter = get_adapter(ATW_SN8)
    status = decode_atw_status(ISSUE139_STATUS)
    sensors = adapter.decode_sensors(ITSSensors(th_temp=99.0, raw_body=b"\x02"), status)

    assert sensors.th_temp is None
    assert adapter.climate_current_temperature(sensors) is None


def test_0x10_overlay_does_not_invent_tank_for_no_dhw_unit() -> None:
    """The 0x10 T5 fallback (body[38]) only fills th_temp for DHW units."""
    status = decode_atw_status(ISSUE138_STATUS)
    sensors = get_adapter(ATW_SN8).decode_sensors(
        decode_its_sensors(ISSUE138_SENSORS), status
    )
    assert sensors.th_temp is None

    _, out_sensors = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, status, sensors)

    assert out_sensors.th_temp is None
    # Other live 0x10 telemetry is unaffected.
    assert out_sensors.t4_temp == 14.0


def test_0x10_overlay_still_fills_tank_for_short_frame_without_c3_basic() -> None:
    """Short/non-C3 status keeps the existing 0x10 T5 fallback (no DHW evidence)."""
    status = decode_atw_status(ISSUE138_STATUS[:20])
    assert status.c3_basic is None
    sensors = get_adapter(ATW_SN8).decode_sensors(ITSSensors(), status)

    _, out_sensors = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, status, sensors)

    assert out_sensors.th_temp == float(NORDIS_UNIT_PARAMS_0X10[38])


def test_non_c3_atw_frame_keeps_body22_tank() -> None:
    """Without a C3 basic match there is no capability evidence: keep body[22]."""
    body = bytearray(ISSUE138_STATUS)
    body[4] = 0x01  # Auto: decode_c3_basic does not match
    status = decode_atw_status(body)

    assert status.c3_basic is None
    assert status.box_bottom_temp == 241.0


_CORPUS = json.loads(
    (Path(__file__).parent / "corpus" / "atw_171H120F.json").read_text("utf-8")
)
_DHW_CASES = [
    case
    for case in _CORPUS
    if _bytes(case["frames"]["0x01"])[2] & 0x04
]


@pytest.mark.parametrize("case", _DHW_CASES, ids=[c["id"] for c in _DHW_CASES])
def test_dhw_units_keep_tank_and_water_heater_matches_sensor(case: dict) -> None:
    """Every corpus unit with DHW keeps body[22] on the sensor and water heater."""
    frames = {int(k, 16): _bytes(v) for k, v in case["frames"].items()}
    adapter = get_adapter(ATW_SN8)
    status = adapter.decode_status(decode_its_status(frames[0x01]))
    sensors = decode_its_sensors(frames[0x02]) if 0x02 in frames else None
    state = build_snapshot(adapter, status, sensors, frames)

    tank = float(frames[0x01][22])
    assert state.status.box_bottom_temp == tank
    caps = project_entity_capabilities(ATW_SN8, state, ViewContext())
    assert caps.dhw_water_heater.exists is True
    if state.sensors is not None:
        assert state.sensors.th_temp == tank
        assert state.dhw_tank_temp == tank
