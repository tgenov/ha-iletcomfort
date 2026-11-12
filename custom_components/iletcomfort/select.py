"""Select entities for the iLetComfort integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info

MUTE_OPTIONS = ["Off", "Level 1", "Level 2"]
_MUTE_TO_API = {"Off": 0, "Level 1": 1, "Level 2": 2}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up select entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [ILetComfortMuteSelect(coordinator)]
    full = (coordinator.data or {}).get("full")
    if isinstance(full, dict) and "run_mode_set" in full:  # KJRH-120L
        entities.append(ILetComfortKjrh120lModeSelect(coordinator))
    async_add_entities(entities)


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
        sensors = self.coordinator.data.get("sensors")
        if sensors is None or sensors.ctrl_flag != 1:
            return "Off"
        # mute_level: 0=Level 1, 1=Level 2
        return "Level 2" if sensors.mute_level == 1 else "Level 1"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_device(mute=_MUTE_TO_API[option])


# ---------------------------------------------------------------------------
# KJRH-120L: run-mode (heat/cool) via captured field 0x03 (heat=3, cool=2).
# The app pairs it with the current Zone-1 setpoint, so we do too.
# ---------------------------------------------------------------------------

_MODE_TO_CODE = {"heat": 3, "cool": 2}
_CODE_TO_MODE = {3: "heat", 2: "cool", 1: "auto"}


class ILetComfortKjrh120lModeSelect(
    CoordinatorEntity[ILetComfortCoordinator], SelectEntity
):
    """Run mode for the KJRH-120L (field 0x03)."""

    _attr_has_entity_name = True
    _attr_name = "Mode"
    _attr_icon = "mdi:sun-snowflake-variant"
    _attr_options = ["heat", "cool"]

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_mode"
        self._attr_device_info = build_device_info(coordinator)

    def _full(self) -> dict:
        full = (self.coordinator.data or {}).get("full")
        return full if isinstance(full, dict) else {}

    @property
    def current_option(self) -> str | None:
        return self._full().get("run_mode_set")

    async def async_select_option(self, option: str) -> None:
        code = _MODE_TO_CODE.get(option)
        if code is None:
            return
        cmd = "0003%02x%02xff" % (1, code)
        # Mirror the app: re-assert the current Zone-1 setpoint alongside mode.
        try:
            z = int(float(self._full().get("zone1_temp_set")))
            cmd += "0004%02x%02xff" % (1, z)
        except (TypeError, ValueError):
            pass
        await self.coordinator.async_send_raw(cmd)
