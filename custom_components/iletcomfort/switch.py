"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MODE_HEAT, MODE_OFF
from .c3_basic import FEATURE_ECO, FEATURE_SILENT, C3BasicStatus
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import (
    async_add_kjrh120l_zone1_entities,
    async_add_when_capable,
    build_device_info,
)
from .model_profiles import ModelProfile
from .protocols import get_adapter


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
    async_add_when_capable(
        coordinator,
        entry,
        async_add_entities,
        is_capable=lambda: isinstance(coordinator.c3_basic_controllable, C3BasicStatus),
        can_become_capable=lambda: (
            coordinator.c3_basic_control_enabled
            and (
                coordinator.sn8 is None
                or get_adapter(coordinator.sn8).profile is ModelProfile.ATW
            )
        ),
        build=lambda: [
            ILetComfortC3CurveSwitch(coordinator),
            ILetComfortC3FeatureSwitch(
                coordinator,
                FEATURE_SILENT,
                "Silent Mode",
                "mdi:volume-off",
                lambda basic: basic.silent_mode,
            ),
            ILetComfortC3FeatureSwitch(
                coordinator,
                FEATURE_ECO,
                "ECO Mode",
                "mdi:leaf",
                lambda basic: basic.eco_mode,
            ),
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


class _ILetComfortC3Switch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Base for switches backed by the C3 basic status (sn8 171H120F)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ILetComfortCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_{key}"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _basic(self) -> C3BasicStatus | None:
        basic = self.coordinator.c3_basic_controllable
        return basic if isinstance(basic, C3BasicStatus) else None

    @property
    def available(self) -> bool:
        return super().available and self._basic is not None


class ILetComfortC3CurveSwitch(_ILetComfortC3Switch):
    """Zone-1 weather-compensation curve on/off."""

    _attr_name = "Weather Curve"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "zone1_curve")

    @property
    def is_on(self) -> bool | None:
        basic = self._basic
        return basic.zone1_curve if basic is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_c3_basic(zone1_curve=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_c3_basic(zone1_curve=False)


class ILetComfortC3FeatureSwitch(_ILetComfortC3Switch):
    """Silent / ECO mode; the device keeps its own timer schedule."""

    def __init__(
        self,
        coordinator: ILetComfortCoordinator,
        feature: str,
        name: str,
        icon: str,
        state_fn: Callable[[C3BasicStatus], bool],
    ) -> None:
        super().__init__(coordinator, f"{feature}_mode")
        self._attr_name = name
        self._attr_icon = icon
        self._feature = feature
        self._state_fn = state_fn

    @property
    def is_on(self) -> bool | None:
        basic = self._basic
        return self._state_fn(basic) if basic is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_c3_feature(self._feature, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_c3_feature(self._feature, False)
