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
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import (
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    MODE_WATERPUMP,
)
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info
from .protocols import (
    HeatPumpState,
    get_adapter,
    is_kjrh120l_dual_state,
    profile_climate_temp_bounds,
    resolve_state,
)

# Query response mode (from device) → HA HVAC mode
_QUERY_MODE_TO_HVAC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    4: HVACMode.FAN_ONLY,  # water pump / circulation
}

# HA HVAC mode → SET command mode
_HVAC_TO_SET_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
    HVACMode.FAN_ONLY: MODE_WATERPUMP,
}


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
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.FAN_ONLY]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_climate"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _status(self):
        if self.coordinator.data:
            return self.coordinator.data.get("status")
        return None

    @property
    def _sensors(self):
        if self.coordinator.data:
            return self.coordinator.data.get("sensors")
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        if self._status is None:
            return HVACMode.OFF
        return _QUERY_MODE_TO_HVAC.get(self._status.mode, HVACMode.OFF)

    @property
    def _state(self) -> HeatPumpState | None:
        return resolve_state(self.coordinator.sn8, self.coordinator.data)

    @property
    def _is_kjrh120l_dual(self) -> bool:
        return is_kjrh120l_dual_state(
            self.coordinator.sn8,
            self.coordinator.data,
            self.coordinator.kjrh120l_zone1_capable is True,
        )

    @property
    def supported_features(self) -> ClimateEntityFeature:
        if self._is_kjrh120l_dual:
            return ClimateEntityFeature(0)
        if self.hvac_mode is HVACMode.FAN_ONLY:
            return (
                self._attr_supported_features
                & ~ClimateEntityFeature.TARGET_TEMPERATURE
            )
        return self._attr_supported_features

    def _ensure_climate_control_supported(self) -> None:
        if self._is_kjrh120l_dual:
            raise HomeAssistantError(
                "Zone-1 control is unavailable because its write command has not "
                "been validated; use the DHW setpoint entity for hot water"
            )

    @property
    def current_temperature(self) -> float | None:
        state = self._state
        return state.climate_current_temperature if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        if self._sensors is not None:
            if self._sensors.twin_temp is not None:
                attrs["water_inlet"] = self._sensors.twin_temp
            if self._sensors.twout_temp is not None:
                attrs["water_outlet"] = self._sensors.twout_temp
            if self._sensors.t4_temp is not None:
                attrs["outdoor_ambient"] = self._sensors.t4_temp
        state = self._state
        basic = state.atw_c3_basic if state is not None else None
        if basic is not None:
            # Both candidate Zone-1 setpoints of the C3 basic layout. Which
            # one the official app shows is not settled for every unit (#124),
            # so expose both raw values as evidence.
            attrs["zone1_water_target"] = float(basic.zone1_target)
            attrs["room_target"] = basic.room_target
            attrs["weather_curve"] = basic.zone1_curve
        return attrs

    @property
    def target_temperature(self) -> float | None:
        if self._status is None or self.hvac_mode is HVACMode.FAN_ONLY:
            return None
        # On a latched dual KJRH-120L unit, keep showing the Zone-1 setpoint
        # (body[12]) even when Zone 1 is switched off rather than flipping to
        # the DHW setpoint (body[15]).
        if (
            self._is_kjrh120l_dual
            and self._status.kjrh120l_zone1_setpoint is not None
        ):
            return self._status.kjrh120l_zone1_setpoint
        # t5s_def (d+2, offset-encoded) is the active mode setpoint.
        # set_temperature (d+4, direct) is the DHW tank target.
        if self._status.t5s_def is not None:
            return self._status.t5s_def
        return float(self._status.set_temperature)



    @property
    def min_temp(self) -> float:
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        state = self._state
        if state is not None:
            return state.climate_temp_bounds(set_mode)[0]
        return profile_climate_temp_bounds(
            get_adapter(self.coordinator.sn8).profile, set_mode
        )[0]

    @property
    def max_temp(self) -> float:
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        state = self._state
        if state is not None:
            return state.climate_temp_bounds(set_mode)[1]
        return profile_climate_temp_bounds(
            get_adapter(self.coordinator.sn8).profile, set_mode
        )[1]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._ensure_climate_control_supported()
        set_mode = _HVAC_TO_SET_MODE.get(hvac_mode)
        if set_mode is not None:
            await self.coordinator.async_set_device(mode=set_mode)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self._ensure_climate_control_supported()
        if self.hvac_mode is HVACMode.FAN_ONLY:
            raise HomeAssistantError(
                "Temperature control is unavailable in Fan Only mode"
            )
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            # Clamp to the entity's min/max before sending. For the KJRH-120L
            # this keeps the short DHW-setpoint write inside the valid range;
            # for other profiles the value is already mode-constrained by HA.
            clamped = max(self.min_temp, min(float(temp), self.max_temp))
            await self.coordinator.async_set_device(temperature=int(clamped))

    async def async_turn_on(self) -> None:
        self._ensure_climate_control_supported()
        await self.coordinator.async_set_device(power_on=True)

    async def async_turn_off(self) -> None:
        self._ensure_climate_control_supported()
        await self.coordinator.async_set_device(mode=MODE_OFF)
