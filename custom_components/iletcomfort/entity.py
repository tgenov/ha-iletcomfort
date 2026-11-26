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
def async_add_when_capable(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    *,
    is_capable: Callable[[], bool],
    can_become_capable: Callable[[], bool],
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add capability-gated entities immediately or on the first matching update.

    Avoids attaching a coordinator listener when ``can_become_capable()`` is
    already False (for example, when ``sn8`` resolves to an incompatible model
    profile), and unsubscribes automatically once entities are added or the
    config entry unloads (#171).
    """
    if is_capable():
        async_add_entities(build())
        return
    if not can_become_capable():
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
        if unsubscribe is None:
            return
        if not can_become_capable():
            _remove_listener()
            return
        if not is_capable():
            return
        _remove_listener()
        async_add_entities(build())

    unsubscribe = coordinator.async_add_listener(_on_update)
    entry.async_on_unload(_remove_listener)


@callback
def async_add_kjrh120l_zone1_entities(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add a platform's KJRH-120L Zone 1 entities once the unit is capable."""
    async_add_when_capable(
        coordinator,
        entry,
        async_add_entities,
        is_capable=lambda: coordinator.kjrh120l_zone1_capable,
        can_become_capable=lambda: (
            coordinator.sn8 is None
            or resolve_profile(coordinator.sn8) is ModelProfile.KJRH120L
        ),
        build=build,
    )


def build_device_info(coordinator: ILetComfortCoordinator) -> DeviceInfo:
    """Build the DeviceInfo that groups every entity under one HA Device.

    Keyed on the stable appliance code so all platforms attach to the same
    device. ``sw_version`` is taken from the outdoor-unit firmware when the
    coordinator already has sensor data (it does by the time platforms are set
    up, since the first refresh runs during ``async_setup_entry``).
    """
    state = coordinator.data
    sw_version = state.odu_firmware_version if state is not None else None
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.appliance_code)},
        name=coordinator.entry.title,
        manufacturer="iLetComfort",
        model=coordinator.sn8,
        sw_version=sw_version,
    )
