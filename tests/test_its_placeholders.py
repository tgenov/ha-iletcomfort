"""ITS placeholder values read as unknown, not as 0.

Frames are from a 171000AU unit (maintainer hardware, integration 0.33.3)
that publishes live inlet/outlet temperatures but does not report a tank
temperature (status ``body[17]`` is the ``0x23`` null-fill) or cumulative
energy (status ``body[44..45]`` is ``00,00`` with 168 compressor hours).
"""

from __future__ import annotations

from custom_components.iletcomfort.api import decode_its_sensors, decode_its_status
from custom_components.iletcomfort.protocols import get_adapter
from custom_components.iletcomfort.protocols.profile import AQUAPURA_SN8
from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS
from tests.snapshot_helpers import make_state
_BY_KEY = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}


def _hex_csv(csv: str) -> bytearray:
    return bytearray(int(part, 16) for part in csv.split(","))


NULL_FILL_TANK_STATUS_BODY = _hex_csv(
    "01,01,00,45,4b,2d,3f,37,0f,23,37,25,28,c0,00,00,80,23,2d,3f,37,00,23,37,"
    "00,3f,00,00,00,00,00,00,01,2c,00,00,00,68,0a,96,04,06,00,27,00,00,00,a8,"
    "00,00,01"
)
NULL_FILL_TANK_SENSORS_BODY = _hex_csv(
    "02,01,00,00,00,00,00,00,00,00,00,00,01,02,00,1b,41,10,00,01,00,33,38,3f,"
    "62,43,44,ef,00,00,dd,00,31,37,0c,31,37,0c,31,37,07,00,00,01,72,00,00,00,"
    "00,00,00"
)


def _state():
    status = decode_its_status(NULL_FILL_TANK_STATUS_BODY)
    sensors = decode_its_sensors(NULL_FILL_TANK_SENSORS_BODY)
    return make_state(status, sensors, sn8=AQUAPURA_SN8)


def test_aquapura_null_fill_tank_is_unknown_not_zero() -> None:
    """A 0x23 null-fill box_bottom_temp must not publish a 0.0 °C tank."""
    state = _state()

    assert state.status.box_bottom_temp == 0.0
    assert state.dhw_tank_temp is None
    assert _BY_KEY["dhw_tank"].value_fn(state) is None


def test_aquapura_null_fill_tank_does_not_use_the_raw_sensors_byte() -> None:
    """Sensors body[17] (raw 16) has no app evidence as a tank reading."""
    sensors = decode_its_sensors(NULL_FILL_TANK_SENSORS_BODY)
    assert sensors.th_temp == 16

    assert _state().sensors.th_temp is None


def test_aquapura_null_fill_tank_keeps_live_water_temperatures() -> None:
    """Climate still falls back to water outlet; inlet/outlet are unchanged."""
    state = _state()

    assert state.climate_current_temperature == 33.0
    assert state.water_inlet_temp == 32.0
    assert state.water_outlet_temp == 33.0


def test_aquapura_without_status_keeps_sensors_unchanged() -> None:
    """A status too short for box_bottom_temp returns the sensors as decoded.

    This pins the existing pass-through only. The raw value is not evidence
    of a tank temperature.
    """
    status = decode_its_status(bytearray([0x01, 0x01, 0x00, 0x45, 0x4b, 0x2d]))
    assert status.box_bottom_temp is None
    sensors = decode_its_sensors(NULL_FILL_TANK_SENSORS_BODY)

    out = get_adapter(AQUAPURA_SN8).decode_sensors(sensors, status)

    assert out is sensors


def test_its_total_energy_zero_is_unknown() -> None:
    """A unit that does not report energy sends 0: unknown, never 0 kWh."""
    status = decode_its_status(NULL_FILL_TANK_STATUS_BODY)

    assert status.total_kwh is None
    # The other telemetry in the same block is unchanged.
    assert status.comp_total_run_hours == 168
    assert status.fan_total_run_hours == 0
    assert status.comp_frq == 39

    state = make_state(status, decode_its_sensors(NULL_FILL_TANK_SENSORS_BODY))
    assert state.total_energy_kwh is None
    assert _BY_KEY["total_energy"].value_fn(state) is None


def test_its_total_energy_nonzero_is_unchanged() -> None:
    """A real counter is still decoded."""
    body = bytearray(NULL_FILL_TANK_STATUS_BODY)
    body[44], body[45] = 0x05, 0x3C

    assert decode_its_status(body).total_kwh == 1340


# -- AQUAPURA water/coil block null-fill (issue #12) ----------------------------
#
# Issue #12 (AQS Energie AQUAPURA): the five sensors bytes body[23..27]
# (T2, T2B, water inlet, water outlet, T1) all held 0x23, while body[21] and
# body[22] held live values (0x34 = 17 °C). The frame below is a reduced
# reconstruction that keeps only those bytes. The full real frames are the
# corpus case ``aquapura_issue12_real_capture_v060``.


def _issue12_sensors_body(block: bytes = b"\x23" * 5) -> bytearray:
    body = bytearray(50)
    body[0] = 0x02
    body[16] = 0x12
    body[17] = 0x12
    body[21] = 0x34
    body[22] = 0x34
    body[23:28] = block
    return body


def _issue12_status() -> object:
    body = bytearray(50)
    body[0] = 0x01
    body[17] = 0x4B  # box_bottom_temp 40 °C (matches the app)
    return decode_its_status(body)


def test_aquapura_null_fill_water_block_is_unknown_not_zero() -> None:
    """A fully null-filled body[23..27] block publishes unknown, not 0.0 °C."""
    status = _issue12_status()
    sensors = decode_its_sensors(_issue12_sensors_body())
    assert sensors.twin_temp == 0.0  # the shared decode is unchanged

    out = get_adapter(AQUAPURA_SN8).decode_sensors(sensors, status)

    assert out.t2_temp is None
    assert out.t2b_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.t1_temp is None
    # Fields outside the block keep their values.
    assert out.t3_temp == 17.0
    assert out.t4_temp == 17.0
    assert out.th_temp == 40.0


def test_aquapura_null_fill_water_block_keeps_tank_as_climate_temperature() -> None:
    status = _issue12_status()
    sensors = decode_its_sensors(_issue12_sensors_body())

    state = make_state(status, sensors, sn8=AQUAPURA_SN8)

    assert state.water_inlet_temp is None
    assert state.water_outlet_temp is None
    assert state.dhw_tank_temp == 40.0
    assert state.climate_current_temperature == 40.0


def test_aquapura_partial_0x23_in_water_block_is_a_reading() -> None:
    """One or more 0x23 bytes is a real 0 °C; only the full block is filler."""
    status = _issue12_status()
    block = bytes([0x23, 0x23, 0x23, 0x23, 0x24])  # T1 = 1 °C
    sensors = decode_its_sensors(_issue12_sensors_body(block))

    out = get_adapter(AQUAPURA_SN8).decode_sensors(sensors, status)

    assert out.twin_temp == 0.0
    assert out.twout_temp == 0.0
    assert out.t2_temp == 0.0
    assert out.t1_temp == 1.0


def test_aquapura_live_water_block_is_unchanged() -> None:
    """The live-inlet/outlet 171000AU variant keeps all five values."""
    state = _state()

    assert state.water_inlet_temp == 32.0
    assert state.water_outlet_temp == 33.0
    assert state.sensors is not None
    assert state.sensors.t2_temp == 28.0
    assert state.sensors.t2b_temp == 63.0


def test_standard_null_fill_water_block_is_not_filtered() -> None:
    """The filter is AQUAPURA-owned: STANDARD keeps the shared decode."""
    status = _issue12_status()
    sensors = decode_its_sensors(_issue12_sensors_body())

    out = get_adapter(None).decode_sensors(sensors, status)

    assert out.twin_temp == 0.0
    assert out.twout_temp == 0.0


def test_aquapura_short_sensors_frame_is_not_filtered() -> None:
    """A frame too short for body[27] has no block to test."""
    status = _issue12_status()
    body = _issue12_sensors_body()[:27]
    sensors = decode_its_sensors(body)

    out = get_adapter(AQUAPURA_SN8).decode_sensors(sensors, status)

    assert out.th_temp == 40.0
    assert out.twin_temp is None  # not decoded by the shared decode either
