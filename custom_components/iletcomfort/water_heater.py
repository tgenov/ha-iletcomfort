"""Domestic hot water entity for C3 basic-layout units (sn8 171H120F)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commands import SetDhwPowerCommand, SetDhwTargetCommand
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import async_add_supported, build_device_info, target_command
from .protocols import HeatPumpState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the DHW entity once a C3 basic-layout DHW unit has been seen."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]

    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.dhw_water_heater,
        lambda: [ILetComfortDhw(coordinator)],
    )


class ILetComfortDhw(CoordinatorEntity[ILetComfortCoordinator], WaterHeaterEntity):
    """Domestic hot water: state, target and tank temperature.

    On/off and the target are writable when the owner enabled C3 basic
    control for the unit (options flow); otherwise the entity is read-only.
    """

    _attr_has_entity_name = True
    _attr_name = "Hot Water"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0
    _attr_operation_list = [STATE_OFF, STATE_HEAT_PUMP]

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_dhw"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _backed(self) -> bool:
        """Whether the current status carries the DHW layout this entity reads."""
        return self.coordinator.entity_capabilities.dhw_water_heater.available

    @property
    def available(self) -> bool:
        return super().available and self._backed

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        if not self.coordinator.entity_capabilities.dhw_water_heater.writable:
            return WaterHeaterEntityFeature(0)
        return (
            WaterHeaterEntityFeature.TARGET_TEMPERATURE
            | WaterHeaterEntityFeature.ON_OFF
            | WaterHeaterEntityFeature.OPERATION_MODE
        )

    @property
    def _state(self) -> HeatPumpState | None:
        """The snapshot, only while it carries the C3 basic DHW layout."""
        state = self.coordinator.data
        return state if state is not None and self._backed else None

    @property
    def current_operation(self) -> str | None:
        state = self._state
        if state is None or state.dhw_power is None:
            return None
        return STATE_HEAT_PUMP if state.dhw_power else STATE_OFF

    @property
    def current_temperature(self) -> float | None:
        state = self._state
        return state.dhw_tank_temp if state is not None else None

    @property
    def target_temperature(self) -> float | None:
        state = self._state
        return state.dhw_target_temp if state is not None else None

    def _dhw_limits(self) -> tuple[float, float] | None:
        """The unit's DHW limits, ignoring zeroed or inverted values."""
        state = self._state
        return state.dhw_target_range if state is not None else None

    @property
    def min_temp(self) -> float:
        limits = self._dhw_limits()
        return limits[0] if limits is not None else super().min_temp

    @property
    def max_temp(self) -> float:
        limits = self._dhw_limits()
        return limits[1] if limits is not None else super().max_temp

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        # The dialect clamps to the DHW range shown here (#192).
        await self.coordinator.async_send_command(
            target_command(SetDhwTargetCommand, temp)
        )

    async def async_set_operation_mode(self, operation_mode: str) -> None:
        if operation_mode not in self._attr_operation_list:
            raise HomeAssistantError(f"Unsupported operation mode {operation_mode}")
        await self.coordinator.async_send_command(
            SetDhwPowerCommand(on=operation_mode == STATE_HEAT_PUMP)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetDhwPowerCommand(on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetDhwPowerCommand(on=False))
