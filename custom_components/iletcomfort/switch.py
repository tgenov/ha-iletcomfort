"""Switch entities for the iLetComfort integration."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .c3_basic import FEATURE_ECO, FEATURE_SILENT
from .commands import (
    SetBoostCommand,
    SetDhwPowerCommand,
    SetEcoModeCommand,
    SetFastDhwCommand,
    SetSilentModeCommand,
    SetZoneCurveCommand,
    SetZonePowerCommand,
)
from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .entity import (
    async_add_kjrh120l_zone1_entities,
    async_add_supported,
    async_remove_unsupported,
    build_device_info,
)
from .protocols import HeatPumpState


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up switch entities."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.boost,
        lambda: [ILetComfortBoostSwitch(coordinator)],
    )
    # Boost used to be created for every model; drop it where unsupported.
    async_remove_unsupported(
        coordinator,
        entry,
        "switch",
        [boost_unique_id(coordinator.appliance_code)],
        lambda caps: caps.boost,
    )
    async_add_kjrh120l_zone1_entities(
        coordinator,
        entry,
        async_add_entities,
        lambda: [
            ILetComfortKjrh120lZone1PowerSwitch(coordinator),
            ILetComfortKjrh120lDhwPowerSwitch(coordinator),
        ],
    )
    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.c3_controls,
        lambda: _build_c3_switches(coordinator),
    )


def boost_unique_id(appliance_code: str) -> str:
    """Return the Boost switch unique_id (pinned; see test_hardware_corpus)."""
    return f"{appliance_code}_boost"


def _build_c3_switches(coordinator: ILetComfortCoordinator) -> list[SwitchEntity]:
    """Build opt-in C3 basic switches for an ATW unit."""
    switches: list[SwitchEntity] = [
        ILetComfortC3CurveSwitch(coordinator),
        ILetComfortC3FeatureSwitch(
            coordinator,
            FEATURE_SILENT,
            "Silent Mode",
            "mdi:volume-off",
            lambda state: state.silent_enabled,
        ),
        ILetComfortC3FeatureSwitch(
            coordinator,
            FEATURE_ECO,
            "ECO Mode",
            "mdi:leaf",
            lambda state: state.eco_enabled,
        ),
    ]
    caps = coordinator.entity_capabilities
    if caps.zone2_curve.exists:
        switches.append(ILetComfortC3Zone2CurveSwitch(coordinator))
    if caps.fast_dhw.exists:
        switches.append(ILetComfortC3FastDhwSwitch(coordinator))
    return switches


class ILetComfortBoostSwitch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Switch entity for boost mode."""

    _attr_has_entity_name = True
    _attr_name = "Boost"
    _attr_icon = "mdi:rocket-launch"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = boost_unique_id(coordinator.appliance_code)
        self._attr_device_info = build_device_info(coordinator)

    @property
    def is_on(self) -> bool:
        if self.coordinator.data is None:
            return False
        return self.coordinator.data.boost_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetBoostCommand(enabled=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetBoostCommand(enabled=False))


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
        state = self.coordinator.data
        return state.zone1_power if state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZonePowerCommand(zone=1, on=True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZonePowerCommand(zone=1, on=False)
        )


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
        state = self.coordinator.data
        return state.dhw_power if state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetDhwPowerCommand(on=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetDhwPowerCommand(on=False))


class _ILetComfortC3Switch(CoordinatorEntity[ILetComfortCoordinator], SwitchEntity):
    """Base for switches backed by the C3 basic status (sn8 171H120F)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: ILetComfortCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.appliance_code}_{key}"
        self._attr_device_info = build_device_info(coordinator)

    @property
    def _controlled(self) -> bool:
        """Whether C3 basic control is active for the current status."""
        return self.coordinator.entity_capabilities.c3_controls.available

    @property
    def available(self) -> bool:
        return super().available and self._controlled

    def _controlled_value(self, read: Callable[[HeatPumpState], bool | None]) -> bool | None:
        """Read a domain value only while C3 basic control is active."""
        state = self.coordinator.data
        if not self._controlled or state is None:
            return None
        return read(state)


class ILetComfortC3CurveSwitch(_ILetComfortC3Switch):
    """Zone-1 weather-compensation curve on/off."""

    _attr_name = "Weather Curve"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "zone1_curve")

    @property
    def is_on(self) -> bool | None:
        return self._controlled_value(lambda state: state.zone1_curve_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZoneCurveCommand(zone=1, enabled=True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZoneCurveCommand(zone=1, enabled=False)
        )


class ILetComfortC3Zone2CurveSwitch(_ILetComfortC3Switch):
    """Zone-2 weather-compensation curve on/off (dual-zone ATW units)."""

    _attr_name = "Zone 2 Weather Curve"
    _attr_icon = "mdi:chart-bell-curve-cumulative"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "zone2_curve")

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.entity_capabilities.zone2_curve.available
        )

    @property
    def is_on(self) -> bool | None:
        return self._controlled_value(lambda state: state.zone2_curve_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZoneCurveCommand(zone=2, enabled=True)
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(
            SetZoneCurveCommand(zone=2, enabled=False)
        )


class ILetComfortC3FastDhwSwitch(_ILetComfortC3Switch):
    """Fast DHW on/off (C3Basic flag bit 0x40 in status, bit 0x08 in SET)."""

    _attr_name = "Fast DHW"
    _attr_icon = "mdi:water-boiler-alert"

    def __init__(self, coordinator: ILetComfortCoordinator) -> None:
        super().__init__(coordinator, "fast_dhw")

    @property
    def is_on(self) -> bool | None:
        return self._controlled_value(lambda state: state.fast_dhw_enabled)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetFastDhwCommand(enabled=True))

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(SetFastDhwCommand(enabled=False))


class ILetComfortC3FeatureSwitch(_ILetComfortC3Switch):
    """Silent / ECO mode; the device keeps its own timer schedule."""

    def __init__(
        self,
        coordinator: ILetComfortCoordinator,
        feature: str,
        name: str,
        icon: str,
        state_fn: Callable[[HeatPumpState], bool | None],
    ) -> None:
        super().__init__(coordinator, f"{feature}_mode")
        self._attr_name = name
        self._attr_icon = icon
        self._feature = feature
        self._state_fn = state_fn

    @property
    def is_on(self) -> bool | None:
        return self._controlled_value(self._state_fn)

    async def async_turn_on(self, **kwargs: Any) -> None:
        if self._feature == FEATURE_SILENT:
            await self.coordinator.async_send_command(SetSilentModeCommand(enabled=True))
        elif self._feature == FEATURE_ECO:
            await self.coordinator.async_send_command(SetEcoModeCommand(enabled=True))
        else:
            raise HomeAssistantError(f"Unsupported C3 feature {self._feature!r}")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._feature == FEATURE_SILENT:
            await self.coordinator.async_send_command(SetSilentModeCommand(enabled=False))
        elif self._feature == FEATURE_ECO:
            await self.coordinator.async_send_command(SetEcoModeCommand(enabled=False))
        else:
            raise HomeAssistantError(f"Unsupported C3 feature {self._feature!r}")
