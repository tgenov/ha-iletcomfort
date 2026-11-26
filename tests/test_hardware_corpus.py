"""Declarative Hardware Capture Corpus & HA entity-stability contract tests (#168).

This suite acts as the zero-regression gate for architectural refactors (EPIC #173):
1. Every real sanitized hardware capture in ``tests/corpus/*.json`` must decode
   identically across profile resolution, structural variant classification,
   status/sensors decoding, and ATW 0x10/0x0A overlays.
2. Every existing Home Assistant entity's ``unique_id``, ``device_class``,
   ``state_class``, and ``native_unit_of_measurement`` is pinned so no refactor
   can silently break user automations or HA Recorder / Energy history.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolumeFlowRate,
)

from custom_components.iletcomfort.api import decode_its_sensors, decode_its_status
from custom_components.iletcomfort.binary_sensor import BINARY_SENSOR_DESCRIPTIONS
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.model_profiles import (
    apply_profile_to_status,
    atw_sensors_are_placeholder,
    describe_profile_variant,
    resolve_profile,
)
from custom_components.iletcomfort.number import (
    ILetComfortKjrh120lDhwSetpoint,
    ILetComfortKjrh120lZone1Setpoint,
)
from custom_components.iletcomfort.protocols import build_snapshot, get_adapter
from custom_components.iletcomfort.select import (
    ILetComfortKjrh120lModeSelect,
    ILetComfortMuteSelect,
)
from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS
from custom_components.iletcomfort.switch import (
    ILetComfortBoostSwitch,
    ILetComfortKjrh120lDhwPowerSwitch,
    ILetComfortKjrh120lZone1PowerSwitch,
)
from custom_components.iletcomfort.water_heater import ILetComfortDhw

CORPUS_DIR = Path(__file__).parent / "corpus"


def _hex_csv(csv: str) -> bytes:
    return bytes(int(part, 16) for part in csv.split(","))


def _load_corpus() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    assert cases, f"No corpus fixtures found in {CORPUS_DIR}"
    return cases


CORPUS_CASES = _load_corpus()


@pytest.mark.parametrize("case", CORPUS_CASES, ids=[c["id"] for c in CORPUS_CASES])
def test_hardware_corpus_decode_contract(case: dict[str, Any]) -> None:
    """Every pinned hardware capture must decode identically to its contract."""
    sn8 = case["sn8"]
    profile = resolve_profile(sn8)
    assert profile.value == case["expected_profile"]

    frames = case["frames"]
    status = apply_profile_to_status(
        profile, decode_its_status(_hex_csv(frames["0x01"]))
    )
    assert describe_profile_variant(sn8, status) == case["expected_variant"]

    sensors = None
    if "0x02" in frames:
        raw_0x02 = _hex_csv(frames["0x02"])
        if "expected_atw_0x02_placeholder" in case:
            assert (
                atw_sensors_are_placeholder(raw_0x02)
                is case["expected_atw_0x02_placeholder"]
            )
        sensors = decode_its_sensors(raw_0x02)

    if "0x10" in frames:
        assert sensors is not None

    # Run the production snapshot builder (poll + MQTT push share it, #225)
    # rather than a hand-written copy of the overlay chain. This covers
    # decoding in poll mode only, not the coordinator's probe gating (e.g.
    # 0x0A is only queried after 0x10 succeeds, but #139 pins 0x0a alone).
    raw_frames = {int(key, 16): _hex_csv(value) for key, value in frames.items()}
    state = build_snapshot(get_adapter(sn8), status, sensors, raw_frames)
    assert state.profile is profile
    assert state.variant == case["expected_variant"]
    status = state.status
    sensors = state.sensors

    for field, expected in case.get("expected_status", {}).items():
        assert getattr(status, field) == expected, (
            f"{case['id']} ({case['source_issue']}): status.{field}"
        )

    for field, expected in case.get("expected_sensors", {}).items():
        assert sensors is not None
        assert getattr(sensors, field) == expected, (
            f"{case['id']} ({case['source_issue']}): sensors.{field}"
        )


# ---------------------------------------------------------------------------
# Home Assistant Entity-Stability Contract (Recorder / Energy / Automations)
# ---------------------------------------------------------------------------
# Never mutate an existing entry's key, device_class, state_class, or unit in
# place — HA Recorder statistics and user automations depend on these tuples.
_PINNED_SENSORS: dict[str, tuple[Any, Any, Any]] = {
    "water_inlet": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "water_outlet": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "dhw_tank": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "outdoor_ambient": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "condenser": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "evaporator": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "refrigerant": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "plate_hx": (
        SensorDeviceClass.TEMPERATURE,
        SensorStateClass.MEASUREMENT,
        UnitOfTemperature.CELSIUS,
    ),
    "compressor_freq": (
        SensorDeviceClass.FREQUENCY,
        SensorStateClass.MEASUREMENT,
        UnitOfFrequency.HERTZ,
    ),
    "total_energy": (
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        UnitOfEnergy.KILO_WATT_HOUR,
    ),
    "comp_run_hours": (
        SensorDeviceClass.DURATION,
        SensorStateClass.TOTAL_INCREASING,
        UnitOfTime.HOURS,
    ),
    "pressure_high": (None, SensorStateClass.MEASUREMENT, None),
    "pressure_low": (None, SensorStateClass.MEASUREMENT, None),
    "error_code": (None, None, None),
    "odu_voltage": (
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        UnitOfElectricPotential.VOLT,
    ),
    "odu_current": (
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        UnitOfElectricCurrent.AMPERE,
    ),
    "water_flow": (
        SensorDeviceClass.VOLUME_FLOW_RATE,
        SensorStateClass.MEASUREMENT,
        UnitOfVolumeFlowRate.CUBIC_METERS_PER_HOUR,
    ),
    "dc_voltage": (
        SensorDeviceClass.VOLTAGE,
        SensorStateClass.MEASUREMENT,
        UnitOfElectricPotential.VOLT,
    ),
    "dc_current": (
        SensorDeviceClass.CURRENT,
        SensorStateClass.MEASUREMENT,
        UnitOfElectricCurrent.AMPERE,
    ),
    "exv_steps": (None, SensorStateClass.MEASUREMENT, None),
    "total_thermal_energy": (
        SensorDeviceClass.ENERGY,
        SensorStateClass.TOTAL_INCREASING,
        UnitOfEnergy.KILO_WATT_HOUR,
    ),
}

_PINNED_BINARY_SENSORS: dict[str, Any] = {
    "compressor_running": BinarySensorDeviceClass.RUNNING,
    "ibh_running": BinarySensorDeviceClass.RUNNING,
    "error": BinarySensorDeviceClass.PROBLEM,
}


def test_sensor_entity_stability_contract() -> None:
    """All existing sensor keys, device_classes, state_classes, and units stay stable."""
    actual = {
        desc.key: (
            desc.device_class,
            desc.state_class,
            desc.native_unit_of_measurement,
        )
        for desc in SENSOR_DESCRIPTIONS
    }
    for key, contract in _PINNED_SENSORS.items():
        assert key in actual, f"Pinned sensor key removed: {key}"
        assert actual[key] == contract, f"Sensor contract changed for {key}"


def test_binary_sensor_entity_stability_contract() -> None:
    """All existing binary sensor keys and device_classes stay stable."""
    actual = {desc.key: desc.device_class for desc in BINARY_SENSOR_DESCRIPTIONS}
    for key, device_class in _PINNED_BINARY_SENSORS.items():
        assert key in actual, f"Pinned binary sensor key removed: {key}"
        assert actual[key] == device_class


def test_platform_unique_id_stability_contract() -> None:
    """Platform unique_id formats stay stable across all entity classes."""
    coord = MagicMock()
    coord.appliance_code = "APPL123"
    coord.sn8 = None
    coord.appliance_meta = None
    coord.data = None

    assert ILetComfortClimate(coord).unique_id == "APPL123_climate"
    assert ILetComfortDhw(coord).unique_id == "APPL123_dhw"
    assert ILetComfortBoostSwitch(coord).unique_id == "APPL123_boost"
    assert ILetComfortMuteSelect(coord).unique_id == "APPL123_mute"
    assert (
        ILetComfortKjrh120lDhwSetpoint(coord).unique_id
        == "APPL123_kjrh120l_dhw_setpoint"
    )
    assert (
        ILetComfortKjrh120lZone1Setpoint(coord).unique_id
        == "APPL123_kjrh120l_zone1_setpoint"
    )
    assert ILetComfortKjrh120lModeSelect(coord).unique_id == "APPL123_kjrh120l_mode"
    assert (
        ILetComfortKjrh120lZone1PowerSwitch(coord).unique_id
        == "APPL123_kjrh120l_zone1_power"
    )
    assert (
        ILetComfortKjrh120lDhwPowerSwitch(coord).unique_id
        == "APPL123_kjrh120l_dhw_power"
    )
