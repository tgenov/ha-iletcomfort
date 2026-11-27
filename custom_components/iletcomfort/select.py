"""Select entities for the iLetComfort integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .commands import SetSilentModeCommand, SetZoneRunModeCommand
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import (
    async_add_kjrh120l_zone1_entities,
    async_add_supported,
    async_remove_unsupported,
    build_device_info,
)
from .protocols.kjrh120l import (
    KJRH120L_RUN_MODE_COOL,
    KJRH120L_RUN_MODE_HEAT,
)

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
    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.mute,
        lambda: [ILetComfortMuteSelect(coordinator)],
    )
    # Silent Mode used to be created for every model; drop it where unsupported.
    async_remove_unsupported(
        coordinator,
        entry,
        "select",
        [mute_unique_id(coordinator.appliance_code)],
        "mute",
    )
    async_add_kjrh120l_zone1_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda: [ILetComfortKjrh120lModeSelect(coordinator)],
    )


def mute_unique_id(appliance_code: str) -> str:
    """Return the Silent Mode select unique_id (pinned; see test_hardware_corpus)."""
    return f"{appliance_code}_mute"


class ILetComfortMuteSelect(CoordinatorEntity[ILetComfortCoordinator], SelectEntity):
    """Select entity for mute/silent mode."""

    _attr_has_entity_name = True
    _attr_name = "Silent Mode"
    _attr_icon = "mdi:volume-off"
    _attr_options = MUTE_OPTIONS

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = mute_unique_id(coordinator.appliance_code)
        self._attr_device_info = build_device_info(coordinator)

    @property
    def current_option(self) -> str:
        state = self.coordinator.data
        if state is None:
            return "Off"
        return {0: "Off", 1: "Level 1", 2: "Level 2"}.get(state.silent_level, "Off")

    async def async_select_option(self, option: str) -> None:
        level = _MUTE_TO_API[option]
        await self.coordinator.async_send_command(SetSilentModeCommand(level=level))


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
        if self.coordinator.data is None:
            return None
        run_mode = self.coordinator.data.zone1_run_mode
        return run_mode if run_mode in _KJRH120L_RUN_MODE_OPTIONS else None

    async def async_select_option(self, option: str) -> None:
        if option not in _KJRH120L_RUN_MODE_OPTIONS:
            raise HomeAssistantError(f"Unsupported KJRH-120L mode {option!r}")
        mode_intent: str = option
        await self.coordinator.async_send_command(
            SetZoneRunModeCommand(zone=1, mode=mode_intent)  # type: ignore[arg-type]
        )
