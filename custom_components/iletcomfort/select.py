"""Select entities for the iLetComfort integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import async_add_kjrh120l_zone1_entities, build_device_info
from .model_profiles import (
    KJRH120L_RUN_MODE_COOL,
    KJRH120L_RUN_MODE_HEAT,
    kjrh120l_mode_change_setpoint,
)
from .protocols import resolve_state

MUTE_OPTIONS = ["Off", "Level 1", "Level 2"]
_MUTE_TO_API = {"Off": 0, "Level 1": 1, "Level 2": 2}

# KJRH-120L run mode codes at status body[11] and write field 0x03. The app
# capture has heat=3 and cool=2 only. The Auto code was never captured, so any
# other reported code shows as no option and cannot be selected.
_KJRH120L_RUN_MODE_OPTIONS = {
    "heat": KJRH120L_RUN_MODE_HEAT,
    "cool": KJRH120L_RUN_MODE_COOL,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ILetComfortMuteSelect(coordinator)])
    async_add_kjrh120l_zone1_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda: [ILetComfortKjrh120lModeSelect(coordinator)],
    )


class ILetComfortMuteSelect(CoordinatorEntity[ILetComfortCoordinator], SelectEntity):
    """Select entity for mute/silent mode."""

    _attr_has_entity_name = True
    _attr_name = "Silent Mode"
    _attr_icon = "mdi:volume-off"
    _attr_options = MUTE_OPTIONS

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_mute"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def current_option(self) -> str:
        if self.coordinator.data is None:
            return "Off"
        status = self.coordinator.data.get("status")
        if status is not None and status.atw_silent_level is not None:
            return {0: "Off", 1: "Level 1", 2: "Level 2"}.get(
                status.atw_silent_level, "Off"
            )
        state = resolve_state(self.coordinator.sn8, self.coordinator.data)
        basic = state.atw_c3_basic if state is not None else None
        if basic is not None:
            return "Level 1" if basic.silent_mode else "Off"
        sensors = self.coordinator.data.get("sensors")
        if sensors is None or sensors.ctrl_flag != 1:
            return "Off"
        # mute_level: 0=Level 1, 1=Level 2
        return "Level 2" if sensors.mute_level == 1 else "Level 1"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_device(mute=_MUTE_TO_API[option])


class ILetComfortKjrh120lModeSelect(
    CoordinatorEntity[ILetComfortCoordinator], SelectEntity
):
    """KJRH-120L run mode, Heat or Cool, written with field 0x03.

    The app sends the mode together with the Zone 1 setpoint. The setpoint
    sent here is the current one clamped into the destination mode's reported
    range, so selecting Cool never sends a heating target.
    """

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_icon = "mdi:sun-snowflake-variant"
    _attr_options = list(_KJRH120L_RUN_MODE_OPTIONS)

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_mode"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def current_option(self) -> str | None:
        status = (self.coordinator.data or {}).get("status")
        if status is None:
            return None
        for option, code in _KJRH120L_RUN_MODE_OPTIONS.items():
            if status.kjrh120l_run_mode == code:
                return option
        return None

    async def async_select_option(self, option: str) -> None:
        run_mode = _KJRH120L_RUN_MODE_OPTIONS.get(option)
        if run_mode is None:
            raise HomeAssistantError(f"Unsupported KJRH-120L mode {option!r}")
        status = (self.coordinator.data or {}).get("status")
        if status is None:
            raise HomeAssistantError("The heat pump has not reported its status yet")
        try:
            zone1_temperature = kjrh120l_mode_change_setpoint(status, run_mode)
        except ValueError as err:
            raise HomeAssistantError(f"Cannot switch to {option}: {err}") from err
        await self.coordinator.async_set_device(
            zone1_run_mode=run_mode, zone1_temperature=zone1_temperature
        )
