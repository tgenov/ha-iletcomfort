"""Domestic hot water entity for C3 basic-layout units (sn8 171H120F)."""

from __future__ import annotations

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .c3_basic import C3BasicStatus
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import async_add_when_capable, build_device_info
from .model_profiles import ModelProfile
from .protocols import get_adapter, resolve_state


def _coordinator_c3_basic(coordinator: ILetComfortCoordinator) -> C3BasicStatus | None:
    state = resolve_state(coordinator.sn8, coordinator.data)
    return state.atw_c3_basic if state is not None else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the DHW entity once a C3 basic-layout DHW unit has been seen."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]

    def _has_dhw() -> bool:
        basic = _coordinator_c3_basic(coordinator)
        return basic is not None and basic.cap_dhw

    async_add_when_capable(
        coordinator,
        entry,
        async_add_entities,
        is_capable=_has_dhw,
        can_become_capable=lambda: (
            coordinator.sn8 is None
            or get_adapter(coordinator.sn8).profile is ModelProfile.ATW
        ),
        build=lambda: [ILetComfortDhw(coordinator)],
    )


class ILetComfortDhw(CoordinatorEntity[ILetComfortCoordinator], WaterHeaterEntity):
    """Domestic hot water: state, target and tank temperature."""

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
    def _basic(self) -> C3BasicStatus | None:
        return _coordinator_c3_basic(self.coordinator)

    @property
    def available(self) -> bool:
        return super().available and self._basic is not None

    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        # Read-only: DHW writes are not part of this entity yet.
        return WaterHeaterEntityFeature(0)

    @property
    def current_operation(self) -> str | None:
        basic = self._basic
        if basic is None:
            return None
        return STATE_HEAT_PUMP if basic.dhw_power else STATE_OFF

    @property
    def current_temperature(self) -> float | None:
        basic = self._basic
        return float(basic.dhw_tank_temp) if basic is not None else None

    @property
    def target_temperature(self) -> float | None:
        basic = self._basic
        return float(basic.dhw_target) if basic is not None else None

    def _dhw_limits(self) -> tuple[float, float] | None:
        """The unit's DHW limits, ignoring zeroed or inverted values."""
        basic = self._basic
        if basic is None or not 0 < basic.dhw_min < basic.dhw_max:
            return None
        return float(basic.dhw_min), float(basic.dhw_max)

    @property
    def min_temp(self) -> float:
        limits = self._dhw_limits()
        return limits[0] if limits is not None else super().min_temp

    @property
    def max_temp(self) -> float:
        limits = self._dhw_limits()
        return limits[1] if limits is not None else super().max_temp
