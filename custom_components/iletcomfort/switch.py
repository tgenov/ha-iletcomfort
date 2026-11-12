"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from typing import Any
import datetime

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import build_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [ILetComfortBoostSwitch(coordinator)]
    # KJRH-120L: KJRH-120L captured controls
    from .model_profiles import ModelProfile, resolve_profile
    if resolve_profile(coordinator.sn8) is ModelProfile.KJRH120L:
        entities.append(ILetComfortKjrh120lHolidaySwitch(coordinator))
        entities.append(ILetComfortKjrh120lFastDhwSwitch(coordinator))
    async_add_entities(entities)


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


# ---------------------------------------------------------------------------
# KJRH-120L: captured KJRH-120L switches (see docs/CONTROL_PROTOCOL.md)
# ---------------------------------------------------------------------------


def _full(coordinator: ILetComfortCoordinator) -> dict:
    full = (coordinator.data or {}).get("full")
    return full if isinstance(full, dict) else {}


class ILetComfortKjrh120lHolidaySwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity
):
    """Holiday / away mode (group 02, field 0x26 + a date range).

    Turning it on sends a holiday window of today..today+7 (the controller then
    holds a reduced setpoint); turning it off clears field 0x26.
    """

    _attr_has_entity_name = True
    _attr_name = "Holiday"
    _attr_icon = "mdi:palm-tree"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_holiday"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        return _full(self.coordinator).get("holiday_on_state") == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        today = datetime.date.today()
        end = today + datetime.timedelta(days=7)
        fields = [
            (0x26, 1), (0x27, 1), (0x28, 1), (0x29, 1),
            (0x2a, today.year % 100), (0x2b, today.month), (0x2c, today.day),
            (0x2d, end.year % 100), (0x2e, end.month), (0x2f, end.day),
            (0x30, 25), (0x31, 25),
        ]
        cmd = "".join("02%02x01%02xff" % (f, v) for f, v in fields)
        await self.coordinator.async_send_raw(cmd)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_raw("02260100ff")


class ILetComfortKjrh120lFastDhwSwitch(
    CoordinatorEntity[ILetComfortCoordinator], SwitchEntity
):
    """Fast DHW / tank backup heater boost (field 0x0b).

    Both the app's "Fast DHW" and "TBH" buttons send field 0x0b on this unit.
    The command only has a visible effect while there is hot-water demand (tank
    below its setpoint), so state feedback is best-effort and the switch is
    optimistic.
    """

    _attr_has_entity_name = True
    _attr_name = "Fast DHW"
    _attr_icon = "mdi:water-boiler-alert"
    _attr_assumed_state = True

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_kjrh120l_fast_dhw"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        full = _full(self.coordinator)
        return full.get("fastdhw_state") == "on" or full.get("forcetbh_state") == "on"

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_raw("000b0101ff")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_raw("000b0100ff")
