"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator


@dataclass(frozen=True, kw_only=True)
class ILetComfortSwitchDescription(SwitchEntityDescription):
    """Describe an iLetComfort switch."""

    turn_on_kwargs: dict[str, Any]
    turn_off_kwargs: dict[str, Any]
    is_on_fn: Any  # Callable[[dict], bool]


def _is_boost_on(data: dict[str, Any]) -> bool:
    status = data.get("status")
    if status is None:
        return False
    return bool(status.enable_flags_1 & 0x04)


def _is_mute_on(data: dict[str, Any], level: int) -> bool:
    status = data.get("status")
    if status is None:
        return False
    if not (status.enable_flags_1 & 0x02):
        return False
    if level == 1:
        return not (status.enable_flags_2 & 0x01)
    return bool(status.enable_flags_2 & 0x01)


SWITCH_DESCRIPTIONS: tuple[ILetComfortSwitchDescription, ...] = (
    ILetComfortSwitchDescription(
        key="boost",
        name="Boost",
        icon="mdi:rocket-launch",
        turn_on_kwargs={"boost": True},
        turn_off_kwargs={"boost": False},
        is_on_fn=_is_boost_on,
    ),
    ILetComfortSwitchDescription(
        key="mute_1",
        name="Mute Level 1",
        icon="mdi:volume-low",
        turn_on_kwargs={"mute": 1},
        turn_off_kwargs={"mute": 0},
        is_on_fn=lambda data: _is_mute_on(data, 1),
    ),
    ILetComfortSwitchDescription(
        key="mute_2",
        name="Mute Level 2",
        icon="mdi:volume-off",
        turn_on_kwargs={"mute": 2},
        turn_off_kwargs={"mute": 0},
        is_on_fn=lambda data: _is_mute_on(data, 2),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        ILetComfortSwitch(coordinator, description)
        for description in SWITCH_DESCRIPTIONS
    )


class ILetComfortSwitch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Switch entity for iLetComfort heat pump."""

    _attr_has_entity_name = True
    entity_description: ILetComfortSwitchDescription

    def __init__(
        self,
        coordinator: ILetComfortCoordinator,
        description: ILetComfortSwitchDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.appliance_code}_{description.key}"

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return self.entity_description.is_on_fn(self.coordinator.data)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(
            **self.entity_description.turn_on_kwargs
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_device(
            **self.entity_description.turn_off_kwargs
        )
