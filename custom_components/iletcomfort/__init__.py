"""The iLetComfort Heat Pump integration."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL
from homeassistant.core import HomeAssistant

from .const import CONF_APPLIANCE_CODE, CONF_REGION, DEFAULT_REGION, DOMAIN, PLATFORMS
from .coordinator import ILetComfortCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up iLetComfort from a config entry."""
    coordinator = ILetComfortCoordinator(hass, entry)
    await coordinator.async_first_refresh_with_login()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries from older formats."""
    if entry.version == 1:
        data = {**entry.data}
        data.setdefault(CONF_REGION, DEFAULT_REGION)

        email = data.get(CONF_EMAIL, "")
        appliance_code = data.get(CONF_APPLIANCE_CODE, "")
        new_unique_id = f"{email.lower()}:{appliance_code}"

        await hass.async_add_executor_job(
            _migrate_shared_token_file, hass, entry,
        )

        hass.config_entries.async_update_entry(
            entry,
            data=data,
            unique_id=new_unique_id,
            version=2,
        )
        _LOGGER.info(
            "Migrated entry %s to v2 (unique_id=%s, region=%s)",
            entry.entry_id, new_unique_id, data[CONF_REGION],
        )

    return True


def _migrate_shared_token_file(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Rename `.storage/iletcomfort_token` → `iletcomfort_token_<entry_id>`.

    Pre-v2 the token cache was a single shared file. Carrying it forward
    avoids forcing a re-login after the upgrade.
    """
    storage = Path(hass.config.path(".storage"))
    old_path = storage / "iletcomfort_token"
    new_path = storage / f"iletcomfort_token_{entry.entry_id}"

    if old_path.exists() and not new_path.exists():
        old_path.rename(new_path)
        _LOGGER.info(
            "Migrated shared token file to per-entry path: %s", new_path,
        )
