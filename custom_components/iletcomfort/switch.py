"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MODE_HEAT, MODE_OFF
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import async_add_kjrh120l_zone1_entities, build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortBoostSwitch(coordinator)])
    async_add_kjrh120l_zone1_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda: [
            ILetComfortKjrh120lZone1PowerSwitch(coordinator),
            ILetComfortKjrh120lDhwPowerSwitch(coordinator),
        ],
    )


class ILetComfortBoostSwitch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Switch entity for boost mode."""

    _attr_has_entity_name = True
    _attr_name = "Boost"
    _attr_icon = "mdi:rocket-launch"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_boost"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        sensors = self.coordinator.data.get("sensors")
        if sensors is None:
            return False
        return sensors.ctrl_flag == 2

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(boost=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(boost=False)


class ILetComfortKjrh120lZone1PowerSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity
):
    """KJRH-120L Zone 1 power, written with field 0x00.

    State is status body[8]. The 2026-11-14 capture shows zone1_power_state
    and zone2_power_state following this write while DHW stays untouched.
    """

    _attr_has_entity_name = True
    _attr_name = "Zone 1 Power"
    _attr_icon = "mdi:heating-coil"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_zone1_power"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        status = (self.coordinator.data or {}).get("status")
        return status.kjrh120l_zone1_power if status is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(zone1_power=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(zone1_power=False)


class ILetComfortKjrh120lDhwPowerSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity
):
    """KJRH-120L DHW power for dual Zone-1 units, written with field 0x02.

    Dual KJRH-120L units expose a read-only Zone-1 climate card, so DHW power
    (status body[10]; ``00020101ff`` / ``00020100ff``) lives on this switch
    alongside Zone 1 Power (#191).
    """

    _attr_has_entity_name = True
    _attr_name = "DHW Power"
    _attr_icon = "mdi:water-boiler"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_dhw_power"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool | None:
        status = (self.coordinator.data or {}).get("status")
        return status.kjrh120l_dhw_power if status is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(mode=MODE_HEAT)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(mode=MODE_OFF)
