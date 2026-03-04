"""Climate entity for the iLetComfort integration."""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import MODE_COOL, MODE_HEAT, MODE_OFF, TEMP_RANGES
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator

# Query response mode (from device) → HA HVAC mode
_QUERY_MODE_TO_HVAC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.COOL,
    2: HVACMode.HEAT,
    4: HVACMode.HEAT,  # waterpump shown as heat with preset
}

# HA HVAC mode → SET command mode
_HVAC_TO_SET_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
}

PRESET_NONE = "none"
PRESET_BOOST = "boost"
PRESET_MUTE_1 = "mute_1"
PRESET_MUTE_2 = "mute_2"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate entity."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortClimate(coordinator)])


class ILetComfortClimate(CoordinatorEntity[ILetComfortCoordinator], ClimateEntity):
    """Climate entity for ITS heat pump."""

    _attr_has_entity_name = True
    _attr_name = "Heat Pump"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
    _attr_preset_modes = [PRESET_NONE, PRESET_BOOST, PRESET_MUTE_1, PRESET_MUTE_2]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_climate"

    @property
    def _status(self):
        """Shortcut to current status data."""
        if self.coordinator.data:
            return self.coordinator.data.get("status")
        return None

    @property
    def _sensors(self):
        """Shortcut to current sensors data."""
        if self.coordinator.data:
            return self.coordinator.data.get("sensors")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        if self._status is None:
            return HVACMode.OFF
        return _QUERY_MODE_TO_HVAC.get(self._status.mode, HVACMode.OFF)

    @property
    def current_temperature(self) -> float | None:
        if self._sensors is None:
            return None
        return self._sensors.twin_temp

    @property
    def target_temperature(self) -> float | None:
        if self._status is None:
            return None
        return float(self._status.set_temperature)

    @property
    def min_temp(self) -> float:
        mode = self._current_set_mode()
        if mode in TEMP_RANGES:
            return float(TEMP_RANGES[mode][0])
        return 10.0

    @property
    def max_temp(self) -> float:
        mode = self._current_set_mode()
        if mode in TEMP_RANGES:
            return float(TEMP_RANGES[mode][1])
        return 40.0

    @property
    def preset_mode(self) -> str:
        if self._status is None:
            return PRESET_NONE
        ef1 = self._status.enable_flags_1
        if ef1 & 0x04:  # boost active
            return PRESET_BOOST
        if ef1 & 0x02:  # mute active
            ef2 = self._status.enable_flags_2
            return PRESET_MUTE_2 if ef2 & 0x01 else PRESET_MUTE_1
        return PRESET_NONE

    def _current_set_mode(self) -> int:
        """Map current query mode to SET mode."""
        if self._status is None:
            return MODE_HEAT
        from .api import QUERY_TO_SET_MODE
        return QUERY_TO_SET_MODE.get(self._status.mode, MODE_HEAT)

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        set_mode = _HVAC_TO_SET_MODE.get(hvac_mode)
        if set_mode is not None:
            await self.coordinator.async_set_device(mode=set_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.async_set_device(temperature=int(temp))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == PRESET_BOOST:
            await self.coordinator.async_set_device(boost=True)
        elif preset_mode == PRESET_MUTE_1:
            await self.coordinator.async_set_device(mute=1)
        elif preset_mode == PRESET_MUTE_2:
            await self.coordinator.async_set_device(mute=2)
        else:
            # PRESET_NONE: disable boost and mute
            await self.coordinator.async_set_device(boost=False, mute=0)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_device(power_on=True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_device(mode=MODE_OFF)
