"""Number entities for the iLetComfort integration."""

from __future__ import annotations

import math

from homeassistant.components.number import (
    DEFAULT_MAX_VALUE,
    DEFAULT_MIN_VALUE,
    NumberEntity,
    NumberMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commands import SetDhwTargetCommand, SetZoneTargetCommand
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import (
    async_add_kjrh120l_zone1_entities,
    build_device_info,
    target_command,
)
from .protocols.kjrh120l import (
    KJRH120L_DHW_TEMP_MAX,
    KJRH120L_DHW_TEMP_MIN,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create DHW and Zone 1 controls for the latched dual KJRH-120L variant.

    Both entities follow the latched Zone 1 capability (see
    ``async_add_kjrh120l_zone1_entities``) so they stay available across Zone 1
    off states and restarts while leaving pure-DHW #5 units untouched.
    """
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_kjrh120l_zone1_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda: [
            ILetComfortKjrh120lDhwSetpoint(coordinator),
            ILetComfortKjrh120lZone1Setpoint(coordinator),
        ],
    )


class ILetComfortKjrh120lDhwSetpoint(
    CoordinatorEntity[ILetComfortCoordinator], NumberEntity
):
    """DHW target using the hardware-confirmed KJRH-120L field 0x07 write.

    The dialect clamps the requested value into its 35-60 °C envelope.
    """

    _attr_has_entity_name = True
    _attr_name = "DHW Setpoint"
    _attr_icon = "mdi:water-thermometer"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_min_value = float(KJRH120L_DHW_TEMP_MIN)
    _attr_native_max_value = float(KJRH120L_DHW_TEMP_MAX)
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_dhw_setpoint"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data
        return state.dhw_target_temp if state is not None else None

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_send_command(
            target_command(SetDhwTargetCommand, value)
        )


class ILetComfortKjrh120lZone1Setpoint(
    CoordinatorEntity[ILetComfortCoordinator], NumberEntity
):
    """Zone 1 target for the KJRH-120L, written with field 0x04.

    Value is status body[12]. Min and max are the range the device reports
    for its current run mode (body[37]/[38] heat, body[39]/[40] cool). Without
    a valid range the entity is unavailable and refuses writes. Whole-degree
    and range validation is the dialect's (``kjrh120l_zone1_target``).
    """

    _attr_has_entity_name = True
    _attr_name = "Zone 1 Target"
    _attr_icon = "mdi:radiator"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_native_step = 1.0
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_zone1_setpoint"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _range(self) -> tuple[int, int] | None:
        state = self.coordinator.data
        return state.zone1_target_range if state is not None else None

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._range is not None
            and self.native_value is not None
        )

    @property
    def native_value(self) -> float | None:
        state = self.coordinator.data
        return state.zone1_target_temp if state is not None else None

    @property
    def native_min_value(self) -> float:
        # Without a range the entity is unavailable and writes are refused,
        # so the platform default only fills the state attributes.
        limits = self._range
        return float(limits[0]) if limits else DEFAULT_MIN_VALUE

    @property
    def native_max_value(self) -> float:
        limits = self._range
        return float(limits[1]) if limits else DEFAULT_MAX_VALUE

    async def async_set_native_value(self, value: float) -> None:
        # An unavailable entity refuses writes (HA skips it for service calls).
        if self._range is None:
            raise HomeAssistantError(
                "The heat pump has not reported a valid Zone 1 range for its "
                "current mode"
            )
        # Commands reject non-finite values at construction; keep the refusal
        # the dialect gives for a value that is not a whole degree.
        if not math.isfinite(value):
            raise HomeAssistantError(f"Zone 1 target must be a whole degree, got {value}")
        await self.coordinator.async_send_command(
            SetZoneTargetCommand(zone=1, temperature=value)
        )
