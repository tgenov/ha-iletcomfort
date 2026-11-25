"""Domestic hot water entity for C3 basic-layout units (sn8 171H120F)."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.water_heater import (
    STATE_HEAT_PUMP,
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_OFF, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .c3_basic import C3BasicStatus
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .model_profiles import ModelProfile, atw_c3_basic, resolve_profile


def _coordinator_c3_basic(coordinator: ILetComfortCoordinator) -> C3BasicStatus | None:
    return atw_c3_basic(coordinator.sn8, (coordinator.data or {}).get("status"))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Add the DHW entity once a C3 basic-layout DHW unit has been seen.

    The model code (sn8) and the first status frame may not be known at setup
    (e.g. the cloud metadata lookup failed on first start), so the decision is
    re-evaluated on every coordinator update until the entity is added. Other
    models never get this entity.
    """
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    unsubscribe: Callable[[], None] | None = None

    @callback
    def _maybe_add() -> bool:
        nonlocal unsubscribe
        basic = _coordinator_c3_basic(coordinator)
        if basic is None or not basic.cap_dhw:
            return False
        if unsubscribe is not None:
            unsubscribe()
            unsubscribe = None
        async_add_entities([ILetComfortDhw(coordinator)])
        return True

    if _maybe_add():
        return
    if (
        coordinator.sn8 is not None
        and resolve_profile(coordinator.sn8) is not ModelProfile.ATW
    ):
        return
    unsubscribe = coordinator.async_add_listener(_maybe_add)

    @callback
    def _unload() -> None:
        if unsubscribe is not None:
            unsubscribe()

    entry.async_on_unload(_unload)


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
