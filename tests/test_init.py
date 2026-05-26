"""Tests for the iLetComfort migration logic."""

from __future__ import annotations

from pathlib import Path

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort import async_migrate_entry
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DEFAULT_REGION,
    DOMAIN,
    REGION_EU,
)


async def test_migrate_v1_sets_new_unique_id_and_region(hass: HomeAssistant):
    """v1 entries (unique_id=email) must be migrated to v2 (email:code, region=us)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",  # v1 format: bare email
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            # NOTE: no CONF_REGION — v1 entries didn't have it
        },
        version=1,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True

    assert entry.version == 2
    assert entry.unique_id == "user@example.com:APPL1"
    assert entry.data[CONF_REGION] == DEFAULT_REGION


async def test_migrate_v1_preserves_existing_region(hass: HomeAssistant):
    """If a v1 entry happens to have CONF_REGION already, don't overwrite it."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert entry.version == 2
    assert entry.data[CONF_REGION] == REGION_EU


async def test_migrate_v1_lowercases_email_in_unique_id(hass: HomeAssistant):
    """The migrated unique_id must use a lowercased email so dedup works."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="USER@Example.com",
        data={
            CONF_EMAIL: "USER@Example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    assert entry.unique_id == "user@example.com:APPL1"


async def test_migrate_v1_renames_shared_token_file_to_per_entry_path(
    hass: HomeAssistant,
):
    """The old shared token file must be renamed to the per-entry path.

    Before this fix, bumping the token filename to include entry_id left the
    old `.storage/iletcomfort_token` orphaned and forced a re-login.
    """
    storage = Path(hass.config.path(".storage"))
    storage.mkdir(parents=True, exist_ok=True)
    old_path = storage / "iletcomfort_token"
    old_path.write_text('{"access_token": "legacy"}', encoding="utf-8")

    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    await async_migrate_entry(hass, entry)

    new_path = storage / f"iletcomfort_token_{entry.entry_id}"
    assert new_path.exists()
    assert new_path.read_text(encoding="utf-8") == '{"access_token": "legacy"}'
    assert not old_path.exists()


async def test_migrate_v1_handles_missing_token_file(hass: HomeAssistant):
    """Migration must not fail if no old token file exists (fresh installs)."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=1,
    )
    entry.add_to_hass(hass)

    # Should not raise.
    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2


async def test_migrate_v2_is_noop(hass: HomeAssistant):
    """A v2 entry must pass through migration untouched."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_REGION: REGION_EU,
            CONF_APPLIANCE_CODE: "APPL1",
        },
        version=2,
    )
    entry.add_to_hass(hass)

    assert await async_migrate_entry(hass, entry) is True
    assert entry.version == 2
    assert entry.unique_id == "user@example.com:APPL1"
    assert entry.data[CONF_REGION] == REGION_EU
