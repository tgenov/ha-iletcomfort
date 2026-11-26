"""Climate entities for the iLetComfort integration.

Both entities map a dialect-projected ``ClimateView`` (``coordinator.climate_view``
/ ``coordinator.zone2_climate_view``) onto Home Assistant. Modes, targets,
bounds, features and read-only reasons are decided by the selected dialect
(#192); target writes pass the requested value and the dialect clamps or
refuses it in ``plan_write``.
"""

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

from .commands import (
    HvacModeIntent,
    SetHvacModeCommand,
    SetPowerCommand,
    SetTargetTemperatureCommand,
    SetZonePowerCommand,
    SetZoneTargetCommand,
)
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import async_add_supported, build_device_info, target_command
from .protocols import ClimateView, HeatPumpState

# HA HVAC mode → semantic HVAC mode intent
_HVAC_TO_INTENT: dict[HVACMode, HvacModeIntent] = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.FAN_ONLY: "fan_only",
}

_SILENT_LEVELS = {0: "off", 1: "level_1", 2: "level_2"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities (primary heat pump + optional ATW Zone 2)."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortClimate(coordinator)])
    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.zone2_climate,
        lambda: [ILetComfortC3Zone2Climate(coordinator)],
    )


def _features(view: ClimateView) -> ClimateEntityFeature:
    features = ClimateEntityFeature(0)
    if view.supports_target_temperature:
        features |= ClimateEntityFeature.TARGET_TEMPERATURE
    if view.supports_turn_on_off:
        features |= ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
    return features


def _refuse_if_read_only(view: ClimateView) -> None:
    if view.read_only_reason is not None:
        raise HomeAssistantError(view.read_only_reason)


class _ILetComfortViewClimate(
    CoordinatorEntity[ILetComfortCoordinator], ClimateEntity
):
    """Climate entity whose shape comes from a dialect ``ClimateView``."""

    _attr_has_entity_name = True
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: ILetComfortCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_{key}"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _view(self) -> ClimateView:
        raise NotImplementedError

    @property
    def _state(self) -> HeatPumpState | None:
        return self.coordinator.data

    @property
    def hvac_modes(self) -> list[HVACMode]:
        return [HVACMode(mode) for mode in self._view.hvac_modes]

    @property
    def hvac_mode(self) -> HVACMode:
        return HVACMode(self._view.hvac_mode)

    @property
    def supported_features(self) -> ClimateEntityFeature:
        return _features(self._view)

    @property
    def target_temperature(self) -> float | None:
        return self._view.target_temperature

    @property
    def target_temperature_step(self) -> float:
        return self._view.target_temperature_step

    @property
    def min_temp(self) -> float:
        return self._view.min_temp

    @property
    def max_temp(self) -> float:
        return self._view.max_temp


class ILetComfortClimate(_ILetComfortViewClimate):
    """Climate entity for ITS heat pump."""

    _attr_name = "Heat Pump"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "climate")

    @property
    def _view(self) -> ClimateView:
        return self.coordinator.climate_view

    @property
    def current_temperature(self) -> float | None:
        state = self._state
        return state.climate_current_temperature if state is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        state = self._state
        if state is None:
            return attrs
        for key, value in (
            ("water_inlet", state.water_inlet_temp),
            ("water_outlet", state.water_outlet_temp),
            ("outdoor_ambient", state.outdoor_temp),
        ):
            if value is not None:
                attrs[key] = value
        zone1_temp_mode = state.zone1_temp_mode
        if zone1_temp_mode is not None:
            # Both candidate Zone-1 setpoints of the C3 basic layout. Which
            # one the official app shows is not settled for every unit (#124),
            # so expose both raw values as evidence.
            attrs["zone1_water_target"] = state.zone1_water_target
            attrs["room_target"] = state.room_target
            attrs["zone1_temp_mode"] = zone1_temp_mode
            attrs["weather_curve"] = state.zone1_curve_enabled
            attrs["eco_mode"] = state.eco_enabled
            silent_level = state.reported_silent_level
            if silent_level is not None:
                attrs["silent_level"] = _SILENT_LEVELS[silent_level]
        return attrs

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        _refuse_if_read_only(self._view)
        intent = _HVAC_TO_INTENT.get(hvac_mode)
        if intent is None:
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        await self.coordinator.async_send_command(SetHvacModeCommand(intent))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        view = self._view
        _refuse_if_read_only(view)
        if view.temperature_refusal is not None:
            raise HomeAssistantError(view.temperature_refusal)
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.async_send_command(
                target_command(SetTargetTemperatureCommand, temp)
            )

    async def async_turn_on(self) -> None:
        _refuse_if_read_only(self._view)
        await self.coordinator.async_send_command(SetPowerCommand(on=True))

    async def async_turn_off(self) -> None:
        _refuse_if_read_only(self._view)
        await self.coordinator.async_send_command(SetPowerCommand(on=False))


class ILetComfortC3Zone2Climate(_ILetComfortViewClimate):
    """Zone-2 climate entity for dual-zone ATW (sn8 171H120F) units (#151).

    Exposed dynamically once a C3 basic status with ``double_zone`` is seen.
    Read-only by default; writable when C3 basic control is enabled in options.
    """

    _attr_name = "Zone 2"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "zone2")

    @property
    def _view(self) -> ClimateView:
        return self.coordinator.zone2_climate_view

    @property
    def available(self) -> bool:
        return super().available and self._view.available

    @property
    def current_temperature(self) -> float | None:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        state = self._state
        if state is None or state.zone2_water_target is None:
            return {}
        return {
            "zone2_water_target": state.zone2_water_target,
            "weather_curve": state.zone2_curve_enabled,
        }

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        view = self._view
        _refuse_if_read_only(view)
        settable = view.settable_hvac_modes
        if settable is not None and hvac_mode not in settable:
            raise HomeAssistantError(
                f"{hvac_mode} is not supported; only Heat and Off can be set"
            )
        await self.coordinator.async_send_command(
            SetZonePowerCommand(zone=2, on=hvac_mode is HVACMode.HEAT)
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        _refuse_if_read_only(self._view)
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        await self.coordinator.async_send_command(
            target_command(lambda value: SetZoneTargetCommand(2, value), temp)
        )

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
