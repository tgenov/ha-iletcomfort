"""Sensor entities for the iLetComfort integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfEnergy,
    UnitOfFrequency,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info


@dataclass(frozen=True, kw_only=True)
class ILetComfortSensorDescription(SensorEntityDescription):
    """Describe an iLetComfort sensor."""

    value_fn: Callable[[dict[str, Any]], Any]


def _s(attr: str) -> Callable[[dict[str, Any]], Any]:
    """Helper: get attribute from sensors data."""
    def _get(data: dict[str, Any]) -> Any:
        sensors = data.get("sensors")
        return getattr(sensors, attr, None) if sensors else None
    return _get


def _st(attr: str) -> Callable[[dict[str, Any]], Any]:
    """Helper: get attribute from status data."""
    def _get(data: dict[str, Any]) -> Any:
        status = data.get("status")
        return getattr(status, attr, None) if status else None
    return _get


SENSOR_DESCRIPTIONS: tuple[ILetComfortSensorDescription, ...] = (
    ILetComfortSensorDescription(
        key="water_inlet",
        name="Water Inlet Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("twin_temp"),
    ),
    ILetComfortSensorDescription(
        key="water_outlet",
        name="Water Outlet Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("twout_temp"),
    ),
    ILetComfortSensorDescription(
        key="dhw_tank",
        name="DHW Tank Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("th_temp"),
    ),
    ILetComfortSensorDescription(
        key="outdoor_ambient",
        name="Outdoor Ambient Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("t4_temp"),
    ),
    ILetComfortSensorDescription(
        key="condenser",
        name="Condenser Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("t3_temp"),
    ),
    ILetComfortSensorDescription(
        key="evaporator",
        name="Evaporator Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("t2_temp"),
    ),
    ILetComfortSensorDescription(
        key="refrigerant",
        name="Refrigerant Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("tf_temp"),
    ),
    ILetComfortSensorDescription(
        key="plate_hx",
        name="Plate Heat Exchanger Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("tp_temp"),
    ),
    ILetComfortSensorDescription(
        key="compressor_freq",
        name="Compressor Frequency",
        native_unit_of_measurement=UnitOfFrequency.HERTZ,
        device_class=SensorDeviceClass.FREQUENCY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_st("comp_frq"),
    ),
    ILetComfortSensorDescription(
        key="total_energy",
        name="Total Energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_st("total_kwh"),
    ),
    ILetComfortSensorDescription(
        key="comp_run_hours",
        name="Compressor Run Hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=_st("comp_total_run_hours"),
    ),
    ILetComfortSensorDescription(
        key="pressure_high",
        name="High Pressure",
        icon="mdi:gauge-full",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_st("pressure_h"),
    ),
    ILetComfortSensorDescription(
        key="pressure_low",
        name="Low Pressure",
        icon="mdi:gauge-low",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_st("pressure_l"),
    ),
    ILetComfortSensorDescription(
        key="error_code",
        name="Error Code",
        icon="mdi:alert-circle-outline",
        value_fn=_st("error_code"),
    ),
    ILetComfortSensorDescription(
        key="odu_voltage",
        name="ODU Voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_s("odu_voltage"),
    ),
    # odu_current is decoded as fixed-point Amperes (raw 16-bit value / 256).
    # The ÷256 scale was confirmed against the official app for the MSC-70D2N8-A
    # (issue #11): raw 1024 -> 4.0 A. See decode_its_sensors() in api.py.
    ILetComfortSensorDescription(
        key="odu_current",
        name="ODU Current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=_s("odu_current"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up sensor entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ILetComfortSensor(coordinator, description)
        for description in SENSOR_DESCRIPTIONS
    )


class ILetComfortSensor(CoordinatorEntity[ILetComfortCoordinator], SensorEntity):
    """Sensor entity for iLetComfort heat pump."""

    _attr_has_entity_name = True
    entity_description: ILetComfortSensorDescription

    def __init__(
        self,
        coordinator: ILetComfortCoordinator,
        description: ILetComfortSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.appliance_code}_{description.key}"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
