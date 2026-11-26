"""Shared entity helpers for the iLetComfort integration."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator
from .protocols import EntityCapabilities, EntitySupport

SupportSelector = Callable[[EntityCapabilities], EntitySupport]


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
def async_add_supported(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    support: SupportSelector,
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add entities when the dialect's capability projection says they exist (#192).

    ``support`` picks one ``EntitySupport`` from
    ``coordinator.entity_capabilities``: ``exists`` adds now, ``possible``
    keeps listening (e.g. while ``sn8`` is still unknown at setup).
    """
    async_add_when_capable(
        coordinator,
        entry,
        async_add_entities,
        is_capable=lambda: support(coordinator.entity_capabilities).exists,
        can_become_capable=lambda: support(coordinator.entity_capabilities).possible,
        build=build,
    )


@callback
def async_add_kjrh120l_zone1_entities(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[], Iterable[Entity]],
) -> None:
    """Add a platform's KJRH-120L Zone 1 entities once the unit is capable."""
    async_add_supported(
        coordinator,
        entry,
        async_add_entities,
        lambda caps: caps.kjrh120l_zone1,
        build,
    )


@callback
def async_remove_unsupported(
    coordinator: ILetComfortCoordinator,
    entry: ConfigEntry,
    domain: str,
    unique_ids: Iterable[str],
    support: SupportSelector,
) -> None:
    """Remove registry entries for entities the resolved dialect never exposes (#192).

    Entities that used to be created for every model (Boost, Silent Mode) stay
    in the entity registry of upgrading users once the platform stops creating
    them. When ``support`` is not ``possible`` this removes exactly those
    ``unique_ids``, and only entries owned by this config entry; removing the
    registry entry also removes a live entity. While ``sn8`` is unknown the
    check re-runs on coordinator updates until the profile settles, so a unit
    that resolves to such a model only after setup is cleaned up too.
    """
    ids = tuple(unique_ids)

    def _check() -> bool:
        """Remove stale entries if unsupported; return True once nothing can change."""
        caps = coordinator.entity_capabilities
        if support(caps).possible:
            return caps.profile_settled
        registry = er.async_get(coordinator.hass)
        for unique_id in ids:
            entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
            if entity_id is None:
                continue
            reg_entry = registry.async_get(entity_id)
            if reg_entry is None or reg_entry.config_entry_id != entry.entry_id:
                continue
            registry.async_remove(entity_id)
        return True

    if _check():
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
        if unsubscribe is not None and _check():
            _remove_listener()

    unsubscribe = coordinator.async_add_listener(_on_update)
    entry.async_on_unload(_remove_listener)


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
