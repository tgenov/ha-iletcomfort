"""Shared entity helpers for the iLetComfort integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .model_profiles import ModelProfile, resolve_profile


@callback
def async_add_kjrh120l_zone1_entities(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add a platform's KJRH-120L Zone 1 entities once the unit is capable.

    Adds them now when ``coordinator.kjrh120l_zone1_capable`` is already true.
    Otherwise listens to the coordinator and adds them on the first update
    that latches the capability, then stops listening. A known non-KJRH sn8
    gets no listener because its profile cannot change.
    """
    if coordinator.kjrh120l_zone1_capable:
        async_add_entities(build())
        return
    if coordinator.sn8 is not None and (
        resolve_profile(coordinator.sn8) is not ModelProfile.KJRH120L
    ):
        return

    unsubscribe: Callable[[], None] | None = None

    @callback
    def _remove_listener() -> None:
        nonlocal unsubscribe
        if unsubscribe is not None:
            unsubscribe()
            unsubscribe = None

    @callback
    def _on_update() -> None:
        if unsubscribe is None or not coordinator.kjrh120l_zone1_capable:
            return
        _remove_listener()
        async_add_entities(build())

    unsubscribe = coordinator.async_add_listener(_on_update)
    entry.async_on_unload(_remove_listener)


def build_device_info(coordinator: ILetComfortCoordinator) -> DeviceInfo:
    """Build the DeviceInfo that groups every entity under one HA Device.

    Keyed on the stable appliance code so all platforms attach to the same
    device. ``sw_version`` is taken from the outdoor-unit firmware when the
    coordinator already has sensor data (it does by the time platforms are set
    up, since the first refresh runs during ``async_setup_entry``).
    """
    sensors = (coordinator.data or {}).get("sensors")
    sw_version = getattr(sensors, "odu_version", "") or None
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.appliance_code)},
        name=coordinator.entry.title,
        manufacturer="iLetComfort",
        model=coordinator.sn8,
        sw_version=sw_version,
    )
