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
from .c3_basic import C3BasicStatus
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
from .entity import async_add_when_capable, build_device_info
from .model_profiles import ModelProfile
from .protocols import (
    HeatPumpState,
    get_adapter,
    is_kjrh120l_dual_state,
    profile_climate_temp_bounds,
)

# Query response mode (from device) → HA HVAC mode
_QUERY_MODE_TO_HVAC: dict[int, HVACMode] = {
    0: HVACMode.OFF,
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    4: HVACMode.FAN_ONLY,  # water pump / circulation
}

# HA HVAC mode → SET command mode (used for mode-specific temperature bounds)
_HVAC_TO_SET_MODE: dict[HVACMode, int] = {
    HVACMode.OFF: MODE_OFF,
    HVACMode.HEAT: MODE_HEAT,
    HVACMode.COOL: MODE_COOL,
    HVACMode.FAN_ONLY: MODE_WATERPUMP,
}

# HA HVAC mode → semantic HVAC mode intent
_HVAC_TO_INTENT: dict[HVACMode, HvacModeIntent] = {
    HVACMode.OFF: "off",
    HVACMode.HEAT: "heat",
    HVACMode.COOL: "cool",
    HVACMode.FAN_ONLY: "fan_only",
}


def _coordinator_c3_basic(coordinator: ILetComfortCoordinator) -> C3BasicStatus | None:
    state = coordinator.data
    return state.atw_c3_basic if state is not None else None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up climate entities (primary heat pump + optional ATW Zone 2)."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortClimate(coordinator)])

    def _has_zone2() -> bool:
        basic = _coordinator_c3_basic(coordinator)
        return basic is not None and basic.double_zone

    async_add_when_capable(
        coordinator,
        entry,
        async_add_entities,
        is_capable=_has_zone2,
        can_become_capable=lambda: (
            coordinator.sn8 is None
            or get_adapter(coordinator.sn8).profile is ModelProfile.ATW
        ),
        build=lambda: [ILetComfortC3Zone2Climate(coordinator)],
    )


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
    def _c3(self) -> C3BasicStatus | None:
        """C3 basic status when the owner enabled C3 basic control (171H120F)."""
        basic = self.coordinator.c3_basic_controllable
        return basic if isinstance(basic, C3BasicStatus) else None

    @property
    def hvac_modes(self) -> list[HVACMode]:
        basic = self._c3
        if basic is not None:
            # Only Heat/Off can be selected; Cool is listed only to report a unit
            # that is already cooling (mode switching is not validated).
            if basic.is_cooling_mode:
                return [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
            return [HVACMode.OFF, HVACMode.HEAT]
        state = self._state
        read_basic = state.atw_c3_basic if state is not None else None
        if isinstance(read_basic, C3BasicStatus):
            modes: list[HVACMode] = [HVACMode.OFF]
            if (
                read_basic.cap_heat
                or not read_basic.cap_cool
                or (read_basic.zone1_power and not read_basic.is_cooling_mode)
            ):
                modes.append(HVACMode.HEAT)
            if read_basic.cap_cool or read_basic.is_cooling_mode:
                modes.append(HVACMode.COOL)
            return modes
        return self._attr_hvac_modes

    @property
    def hvac_mode(self) -> HVACMode:
        basic = self._c3
        if basic is not None:
            if not basic.zone1_power:
                return HVACMode.OFF
            return HVACMode.COOL if basic.is_cooling_mode else HVACMode.HEAT
        state = self._state
        if state is None:
            return HVACMode.OFF
        return _QUERY_MODE_TO_HVAC.get(state.operating_mode, HVACMode.OFF)

    @property
    def _state(self) -> HeatPumpState | None:
        return self.coordinator.data

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
        state = self._state
        read_basic = state.atw_c3_basic if state is not None else None
        if isinstance(read_basic, C3BasicStatus) and self._c3 is None:
            raise HomeAssistantError(
                "ATW (171H120F) control is disabled by default; enable "
                "experimental ATW control in the integration options"
            )

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
        basic = state.atw_c3_basic
        if basic is not None:
            # Both candidate Zone-1 setpoints of the C3 basic layout. Which
            # one the official app shows is not settled for every unit (#124),
            # so expose both raw values as evidence.
            attrs["zone1_water_target"] = float(basic.zone1_target)
            attrs["room_target"] = basic.room_target
            attrs["zone1_temp_mode"] = "water" if basic.zone1_water_temp else "room"
            attrs["weather_curve"] = state.zone1_curve_enabled
            attrs["eco_mode"] = state.eco_enabled
            silent_level = state.reported_silent_level
            if silent_level is not None:
                attrs["silent_level"] = {
                    0: "off",
                    1: "level_1",
                    2: "level_2",
                }[silent_level]
        return attrs

    @property
    def target_temperature(self) -> float | None:
        basic = self._c3
        if basic is not None:
            # With control enabled the climate shows the field it writes. The
            # option text asks the owner to confirm it matches the app (#124).
            if basic.zone1_water_temp:
                return float(basic.zone1_target)
            return basic.room_target
        state = self._state
        if state is None or self.hvac_mode is HVACMode.FAN_ONLY:
            return None
        # On a latched dual KJRH-120L unit, keep showing the Zone-1 setpoint
        # (body[12]) even when Zone 1 is switched off rather than flipping to
        # the DHW setpoint (body[15]).
        if self._is_kjrh120l_dual and state.zone1_target_temp is not None:
            return state.zone1_target_temp
        return state.active_setpoint

    @property
    def target_temperature_step(self) -> float:
        basic = self._c3
        if basic is None:
            state = self._state
            read_basic = state.atw_c3_basic if state is not None else None
            if isinstance(read_basic, C3BasicStatus):
                basic = read_basic
        if basic is not None and not basic.zone1_water_temp:
            return 0.5
        return self._attr_target_temperature_step

    def _c3_range(self, basic: C3BasicStatus) -> tuple[float, float]:
        low, high = (
            basic.zone1_water_range
            if basic.zone1_water_temp
            else (basic.room_min, basic.room_max)
        )
        if not 0 < low < high:
            return 10.0, 40.0
        return low, high

    @property
    def min_temp(self) -> float:
        basic = self._c3
        if basic is not None:
            return self._c3_range(basic)[0]
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        state = self._state
        if state is not None:
            return state.climate_temp_bounds(set_mode)[0]
        return profile_climate_temp_bounds(
            get_adapter(self.coordinator.sn8).profile, set_mode
        )[0]

    @property
    def max_temp(self) -> float:
        basic = self._c3
        if basic is not None:
            return self._c3_range(basic)[1]
        set_mode = _HVAC_TO_SET_MODE.get(self.hvac_mode)
        state = self._state
        if state is not None:
            return state.climate_temp_bounds(set_mode)[1]
        return profile_climate_temp_bounds(
            get_adapter(self.coordinator.sn8).profile, set_mode
        )[1]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._ensure_climate_control_supported()
        intent = _HVAC_TO_INTENT.get(hvac_mode)
        if intent is None:
            raise HomeAssistantError(f"Unsupported HVAC mode {hvac_mode}")
        await self.coordinator.async_send_command(SetHvacModeCommand(intent))

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self._ensure_climate_control_supported()
        if self._c3 is None and self.hvac_mode is HVACMode.FAN_ONLY:
            raise HomeAssistantError(
                "Temperature control is unavailable in Fan Only mode"
            )
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            clamped = max(self.min_temp, min(float(temp), self.max_temp))
            await self.coordinator.async_send_command(
                SetTargetTemperatureCommand(clamped)
            )

    async def async_turn_on(self) -> None:
        self._ensure_climate_control_supported()
        await self.coordinator.async_send_command(SetPowerCommand(on=True))

    async def async_turn_off(self) -> None:
        self._ensure_climate_control_supported()
        await self.coordinator.async_send_command(SetPowerCommand(on=False))


class ILetComfortC3Zone2Climate(
    CoordinatorEntity[ILetComfortCoordinator], ClimateEntity
):
    """Zone-2 climate entity for dual-zone ATW (sn8 171H120F) units (#151).

    Exposed dynamically once a C3 basic status with ``double_zone`` is seen.
    Read-only by default; writable when C3 basic control is enabled in options.
    """

    _attr_has_entity_name = True
    _attr_name = "Zone 2"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1.0

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_zone2"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _basic(self) -> C3BasicStatus | None:
        return _coordinator_c3_basic(self.coordinator)

    @property
    def _c3(self) -> C3BasicStatus | None:
        basic = self.coordinator.c3_basic_controllable
        return basic if isinstance(basic, C3BasicStatus) else None

    @property
    def available(self) -> bool:
        basic = self._basic
        return super().available and basic is not None and basic.double_zone

    @property
    def supported_features(self) -> ClimateEntityFeature:
        basic = self._c3
        if basic is None or not basic.double_zone:
            return ClimateEntityFeature(0)
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if basic.zone2_water_temp:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def hvac_modes(self) -> list[HVACMode]:
        basic = self._c3
        if basic is not None:
            if basic.is_cooling_mode:
                return [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL]
            return [HVACMode.OFF, HVACMode.HEAT]
        read_basic = self._basic
        if read_basic is None:
            return [HVACMode.OFF]
        modes: list[HVACMode] = [HVACMode.OFF]
        if (
            read_basic.cap_heat
            or not read_basic.cap_cool
            or (read_basic.zone2_power and not read_basic.is_cooling_mode)
        ):
            modes.append(HVACMode.HEAT)
        if read_basic.cap_cool or read_basic.is_cooling_mode:
            modes.append(HVACMode.COOL)
        return modes

    @property
    def hvac_mode(self) -> HVACMode:
        basic = self._basic
        if basic is None or not basic.zone2_power:
            return HVACMode.OFF
        return HVACMode.COOL if basic.is_cooling_mode else HVACMode.HEAT

    @property
    def current_temperature(self) -> float | None:
        return None

    @property
    def target_temperature(self) -> float | None:
        basic = self._basic
        if basic is None or not basic.zone2_water_temp:
            return None
        return float(basic.zone2_target)

    def _zone2_range(self) -> tuple[float, float]:
        basic = self._basic
        if basic is not None:
            low, high = basic.zone2_water_range
            if 0 < low < high:
                return low, high
        return 10.0, 40.0

    @property
    def min_temp(self) -> float:
        return self._zone2_range()[0]

    @property
    def max_temp(self) -> float:
        return self._zone2_range()[1]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        basic = self._basic
        if basic is None:
            return {}
        return {
            "zone2_water_target": float(basic.zone2_target),
            "weather_curve": basic.zone2_curve,
        }

    def _ensure_control_enabled(self) -> None:
        if self._c3 is None:
            raise HomeAssistantError(
                "ATW (171H120F) control is disabled by default; enable "
                "experimental ATW control in the integration options"
            )

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        self._ensure_control_enabled()
        if hvac_mode not in (HVACMode.OFF, HVACMode.HEAT):
            raise HomeAssistantError(
                f"{hvac_mode} is not supported; only Heat and Off can be set"
            )
        await self.coordinator.async_send_command(
            SetZonePowerCommand(zone=2, on=hvac_mode is HVACMode.HEAT)
        )

    async def async_set_temperature(self, **kwargs: Any) -> None:
        self._ensure_control_enabled()
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is None:
            return
        clamped = max(self.min_temp, min(float(temp), self.max_temp))
        await self.coordinator.async_send_command(
            SetZoneTargetCommand(zone=2, temperature=clamped)
        )

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT)

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)
