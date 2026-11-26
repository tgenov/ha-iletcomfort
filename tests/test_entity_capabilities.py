"""Dialect-owned entity capability projection and stale-entity cleanup (#192)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ITSSensors, ITSStatus, decode_its_status
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.model_profiles import (
    apply_profile_to_status,
    resolve_profile,
)
from custom_components.iletcomfort.protocols import (
    EntityCapabilities,
    EntitySupport,
    ViewContext,
    get_adapter,
    project_entity_capabilities,
)
from custom_components.iletcomfort.protocols.atw import decode_atw_status
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B
from tests.snapshot_helpers import make_state

ATW_SN8 = "171H120F"
KJRH_SN8 = "17100003"
AQUAPURA_SN8 = "171000AU"

# Rotenso Aquami status body (tests/test_c3_basic.py ROTENSO_BASELINE): one zone,
# DHW capable, water-temperature control.
ROTENSO = bytes.fromhex(
    "01 08 15 a0 03 03 1d 1e 37 30 37 19 19 05 37 19 19 05 3c 22 3c 14 10 00 80"
)
# Kaisai #151 (tests/test_c3_basic.py KAISAI_DUAL_ZONE): dual zone.
KAISAI_DUAL = bytes.fromhex(
    "01 1f 3f 00 03 03 23 19 2c 30 37 19 19 05 37 19 19 05 3c 22 3c 14 2c 00 80"
)

_FULL = EntitySupport(exists=True, possible=True, available=True, writable=True)


def _atw_state(body: bytes):
    return make_state(decode_atw_status(body), ITSSensors(), sn8=ATW_SN8)


def _caps(sn8, state, *, control=False, latched=False) -> EntityCapabilities:
    return project_entity_capabilities(
        sn8, state, ViewContext(c3_basic_control=control, kjrh120l_zone1_latched=latched)
    )


# -- pure projection -----------------------------------------------------------


@pytest.mark.parametrize("sn8", ["UNKNOWN8", AQUAPURA_SN8, "17100007"])
def test_legacy_dialects_expose_boost_and_mute_only(sn8):
    state = make_state(ITSStatus(mode=1), ITSSensors(), sn8=sn8)
    caps = _caps(sn8, state, control=True, latched=True)

    assert caps.boost == _FULL
    assert caps.mute == _FULL
    for name in (
        "zone2_climate",
        "dhw_water_heater",
        "c3_controls",
        "zone2_curve",
        "fast_dhw",
        "kjrh120l_zone1",
    ):
        assert getattr(caps, name) == EntitySupport(), name
    assert caps.profile_settled


@pytest.mark.parametrize("latched", [False, True])
@pytest.mark.parametrize("body", [KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B, None])
def test_kjrh120l_hides_boost_and_mute(body, latched):
    state = (
        None
        if body is None
        else make_state(
            get_adapter(KJRH_SN8).decode_status(ITSStatus(raw_body=body)),
            ITSSensors(),
            sn8=KJRH_SN8,
        )
    )
    caps = _caps(KJRH_SN8, state, latched=latched)

    assert caps.boost == EntitySupport()
    assert caps.mute == EntitySupport()
    assert caps.kjrh120l_zone1 == EntitySupport(
        exists=latched, possible=True, available=latched, writable=True
    )
    assert not caps.dhw_water_heater.possible


def test_atw_read_only_projection():
    caps = _caps(ATW_SN8, _atw_state(ROTENSO))

    assert caps.boost == EntitySupport(exists=True, possible=True, available=True)
    assert caps.mute == EntitySupport(exists=True, possible=True, available=True)
    assert caps.dhw_water_heater == EntitySupport(
        exists=True, possible=True, available=True, writable=False
    )
    assert caps.zone2_climate == EntitySupport(possible=True)
    # Without the opt-in the C3 switches can never appear.
    assert caps.c3_controls == EntitySupport()
    assert caps.zone2_curve == EntitySupport()
    assert caps.fast_dhw == EntitySupport()
    assert caps.kjrh120l_zone1 == EntitySupport()


def test_atw_controlled_dual_zone_projection():
    caps = _caps(ATW_SN8, _atw_state(KAISAI_DUAL), control=True)

    assert caps.mute.writable
    assert not caps.boost.writable
    assert caps.zone2_climate == _FULL
    assert caps.c3_controls == _FULL
    assert caps.zone2_curve == _FULL
    assert caps.fast_dhw.exists == caps.dhw_water_heater.exists


def test_atw_without_c3_layout_keeps_entities_possible_only():
    caps = _caps(ATW_SN8, None, control=True)

    assert caps.zone2_climate == EntitySupport(possible=True)
    assert caps.dhw_water_heater == EntitySupport(possible=True)
    assert caps.c3_controls == EntitySupport(possible=True)
    assert caps.boost.exists


@pytest.mark.parametrize("control", [False, True])
def test_unknown_sn8_uses_standard_now_and_keeps_every_dialect_possible(control):
    state = make_state(ITSStatus(mode=1), ITSSensors(), sn8=None)
    caps = _caps(None, state, control=control)

    assert not caps.profile_settled
    assert caps.boost == _FULL
    assert caps.mute == _FULL
    assert caps.zone2_climate == EntitySupport(possible=True)
    assert caps.dhw_water_heater == EntitySupport(possible=True)
    assert caps.kjrh120l_zone1 == EntitySupport(possible=True)
    assert caps.c3_controls == EntitySupport(possible=control)


# -- registry cleanup through a real config entry --------------------------------


@pytest.fixture
def mock_client():
    with (
        patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls,
        patch(
            "custom_components.iletcomfort.coordinator.asyncio.sleep",
            new=AsyncMock(),
        ),
    ):
        client = cls.return_value
        client.load_token.return_value = True
        client.query_sensors.return_value = ITSSensors()
        yield client


def _entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_US,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    return entry


def _preregister_boost_and_mute(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Simulate an upgrading user whose registry has the old Boost/Silent entities."""
    registry = er.async_get(hass)
    registry.async_get_or_create("switch", DOMAIN, "APPL1_boost", config_entry=entry)
    registry.async_get_or_create("select", DOMAIN, "APPL1_mute", config_entry=entry)


def _status_for(sn8: str | None, body: bytes) -> ITSStatus:
    return apply_profile_to_status(resolve_profile(sn8), decode_its_status(bytearray(body)))


def _registered(hass: HomeAssistant, platform: str, unique_id: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)


async def _setup(hass, client: MagicMock, entry, sn8, body) -> None:
    client.list_appliances.return_value = [{"applianceCode": "APPL1", "sn8": sn8}]
    client.query_status.return_value = _status_for(sn8, body)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_kjrh120l_upgrade_removes_stale_boost_and_mute(hass, mock_client):
    entry = _entry(hass)
    _preregister_boost_and_mute(hass, entry)

    await _setup(hass, mock_client, entry, KJRH_SN8, KJRH120L_SNAPSHOT_B)

    assert _registered(hass, "switch", "APPL1_boost") is None
    assert _registered(hass, "select", "APPL1_mute") is None
    assert _registered(hass, "climate", "APPL1_climate") is not None
    assert hass.states.get("switch.mock_title_boost") is None


@pytest.mark.parametrize(
    ("sn8", "body"),
    [
        ("UNKNOWN8", bytes(60)),
        (AQUAPURA_SN8, bytes(60)),
        (ATW_SN8, ROTENSO),
    ],
)
async def test_other_models_keep_boost_and_mute_unique_ids(hass, mock_client, sn8, body):
    entry = _entry(hass)
    _preregister_boost_and_mute(hass, entry)
    boost_id = _registered(hass, "switch", "APPL1_boost")
    mute_id = _registered(hass, "select", "APPL1_mute")

    await _setup(hass, mock_client, entry, sn8, body)

    assert _registered(hass, "switch", "APPL1_boost") == boost_id
    assert _registered(hass, "select", "APPL1_mute") == mute_id
    assert hass.states.get(boost_id) is not None
    assert hass.states.get(mute_id) is not None


async def test_sn8_resolving_to_kjrh120l_after_setup_removes_live_entities(
    hass, mock_client
):
    entry = _entry(hass)
    # sn8 unknown at setup: the STANDARD default creates Boost and Silent Mode.
    await _setup(hass, mock_client, entry, None, KJRH120L_SNAPSHOT_B)
    boost_id = _registered(hass, "switch", "APPL1_boost")
    mute_id = _registered(hass, "select", "APPL1_mute")
    assert boost_id is not None and hass.states.get(boost_id) is not None
    assert mute_id is not None and hass.states.get(mute_id) is not None

    mock_client.list_appliances.return_value = [{"applianceCode": "APPL1", "sn8": KJRH_SN8}]
    mock_client.query_status.return_value = _status_for(KJRH_SN8, KJRH120L_SNAPSHOT_B)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.sn8 == KJRH_SN8
    assert _registered(hass, "switch", "APPL1_boost") is None
    assert _registered(hass, "select", "APPL1_mute") is None
    assert hass.states.get(boost_id) is None
    assert hass.states.get(mute_id) is None
    assert _registered(hass, "climate", "APPL1_climate") is not None


async def test_sn8_resolving_to_other_model_after_setup_keeps_entities(
    hass, mock_client
):
    entry = _entry(hass)
    await _setup(hass, mock_client, entry, None, bytes(60))
    coordinator = hass.data[DOMAIN][entry.entry_id]
    # While sn8 is unknown, the add-when-capable and cleanup helpers listen.
    listeners_before = len(coordinator._listeners)

    mock_client.list_appliances.return_value = [{"applianceCode": "APPL1", "sn8": AQUAPURA_SN8}]
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert _registered(hass, "switch", "APPL1_boost") is not None
    assert _registered(hass, "select", "APPL1_mute") is not None
    # Once the profile settles every waiting listener has unsubscribed.
    assert len(coordinator._listeners) < listeners_before


async def test_cleanup_ignores_entries_of_another_config_entry(hass, mock_client):
    other = MockConfigEntry(domain=DOMAIN, unique_id="other@example.com:APPL1")
    other.add_to_hass(hass)
    registry = er.async_get(hass)
    foreign = registry.async_get_or_create(
        "switch", DOMAIN, "APPL1_boost", config_entry=other
    )
    entry = _entry(hass)

    await _setup(hass, mock_client, entry, KJRH_SN8, KJRH120L_SNAPSHOT_B)

    assert registry.async_get(foreign.entity_id) is not None
