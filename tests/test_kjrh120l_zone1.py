"""KJRH-120L Zone 1 setpoint, run mode and Zone 1 power entities.

Fixtures are the two real status bodies in ``tests/kjrh120l_snapshots.py``.
Expected command strings come from the iOS app capture (2026-11-12 and
2026-11-14).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    ILetComfortClient,
    ITSSensors,
    decode_its_status,
)
from custom_components.iletcomfort.commands import SetDhwPowerCommand
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_KJRH120L_ZONE1_CAPABLE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.model_profiles import (
    KJRH120L_SN8,
    apply_profile_to_status,
    decode_kjrh120l_status,
    resolve_profile,
)
from custom_components.iletcomfort.number import ILetComfortKjrh120lZone1Setpoint
from custom_components.iletcomfort.protocols import (
    WriteContext,
    get_adapter,
    run_write_plan,
)
from custom_components.iletcomfort.select import ILetComfortKjrh120lModeSelect
from custom_components.iletcomfort.switch import (
    ILetComfortKjrh120lDhwPowerSwitch,
    ILetComfortKjrh120lZone1PowerSwitch,
)
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B
from tests.write_helpers import bind_write_path, sent_commands
from tests.snapshot_helpers import make_state


def _with(body: bytes, **index_values: int) -> bytes:
    out = bytearray(body)
    for key, value in index_values.items():
        out[int(key.lstrip("b"))] = value
    return bytes(out)


def _coordinator(body: bytes) -> MagicMock:
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.sn8 = KJRH120L_SN8
    coordinator.last_update_success = True
    coordinator.data = make_state(decode_kjrh120l_status(body), ITSSensors(), sn8=KJRH120L_SN8)
    coordinator.c3_basic_controllable = None
    return bind_write_path(coordinator, kjrh120l_zone1_latched=True)


# --- Zone 1 target number ---------------------------------------------------


def test_zone1_number_reads_setpoint_and_heat_range():
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(KJRH120L_SNAPSHOT_A))

    assert entity.available is True
    assert entity.native_value == 50.0
    assert entity.native_min_value == 35.0
    assert entity.native_max_value == 75.0
    assert entity.native_step == 1.0


def test_zone1_number_uses_cool_range_in_cool_mode():
    entity = ILetComfortKjrh120lZone1Setpoint(
        _coordinator(_with(KJRH120L_SNAPSHOT_A, b11=2, b12=20))
    )

    assert entity.native_value == 20.0
    assert entity.native_min_value == 5.0
    assert entity.native_max_value == 25.0


@pytest.mark.parametrize(("value", "expected"), [(51, 51), (63, 63), (35.0, 35)])
async def test_zone1_number_writes_field_04(value, expected):
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(KJRH120L_SNAPSHOT_A))

    await entity.async_set_native_value(value)

    assert sent_commands(entity.coordinator) == ["000401%02xff" % expected]


@pytest.mark.parametrize("value", [34, 76, 50.5, float("nan")])
async def test_zone1_number_refuses_values_outside_reported_range(value):
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(KJRH120L_SNAPSHOT_A))

    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(value)

    entity.coordinator.async_execute_plan.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        _with(KJRH120L_SNAPSHOT_A, b11=1),  # run mode not heat or cool
        _with(KJRH120L_SNAPSHOT_A, b37=35, b38=35),  # heat min == max
        _with(KJRH120L_SNAPSHOT_A, b37=30, b38=35),  # heat min > max
        KJRH120L_SNAPSHOT_A[:40],  # limits missing
    ],
)
async def test_zone1_number_unavailable_and_refuses_without_valid_range(body):
    """Reviewer P2: no invented 20-75 fallback."""
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(body))

    assert entity.available is False
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(50)

    entity.coordinator.async_execute_plan.assert_not_awaited()


async def test_zone1_number_follows_live_status_without_caching():
    """Reviewer P2: the value and range come from the latest coordinator.data."""
    coordinator = _coordinator(KJRH120L_SNAPSHOT_A)
    entity = ILetComfortKjrh120lZone1Setpoint(coordinator)
    assert entity.native_value == 50.0

    coordinator.data = make_state(decode_kjrh120l_status(_with(KJRH120L_SNAPSHOT_A, b11=2, b12=20)), ITSSensors(), sn8=KJRH120L_SN8)

    assert entity.native_value == 20.0
    assert entity.native_max_value == 25.0
    with pytest.raises(HomeAssistantError):
        await entity.async_set_native_value(50)


def test_zone1_number_stays_available_when_zone_is_off():
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(KJRH120L_SNAPSHOT_B))

    assert entity.available is True
    assert entity.native_value == 37.0


def test_zone1_number_keeps_previous_unique_id_and_name():
    entity = ILetComfortKjrh120lZone1Setpoint(_coordinator(KJRH120L_SNAPSHOT_A))

    assert entity.unique_id == "APPL1_kjrh120l_zone1_setpoint"
    assert entity.name == "Zone 1 Target"


# --- Mode select --------------------------------------------------------------


@pytest.mark.parametrize(
    ("run_mode", "expected"), [(3, "heat"), (2, "cool"), (1, None), (0, None), (4, None)]
)
def test_mode_select_reports_heat_and_cool_only(run_mode, expected):
    entity = ILetComfortKjrh120lModeSelect(
        _coordinator(_with(KJRH120L_SNAPSHOT_A, b11=run_mode))
    )

    assert entity.options == ["heat", "cool"]
    assert entity.current_option == expected


async def test_mode_select_heat_to_cool_sends_cool_range_target_not_heat_target():
    """Reviewer P1: Heat 50 -> Cool with range 5-25 sends 25."""
    entity = ILetComfortKjrh120lModeSelect(_coordinator(KJRH120L_SNAPSHOT_A))

    await entity.async_select_option("cool")

    assert sent_commands(entity.coordinator) == ["00030102ff00040119ff"]


async def test_mode_select_cool_to_heat_clamps_up_to_heat_minimum():
    entity = ILetComfortKjrh120lModeSelect(
        _coordinator(_with(KJRH120L_SNAPSHOT_A, b11=2, b12=20))
    )

    await entity.async_select_option("heat")

    assert sent_commands(entity.coordinator) == ["00030103ff00040123ff"]


async def test_mode_select_heat_keeps_setpoint_inside_heat_range():
    entity = ILetComfortKjrh120lModeSelect(_coordinator(KJRH120L_SNAPSHOT_A))

    await entity.async_select_option("heat")

    assert sent_commands(entity.coordinator) == ["00030103ff00040132ff"]


@pytest.mark.parametrize(
    "body",
    [
        _with(KJRH120L_SNAPSHOT_A, b39=5, b40=25),  # cool range inverted
        KJRH120L_SNAPSHOT_A[:39],  # cool limits missing
        KJRH120L_SNAPSHOT_A[:12],  # setpoint missing
    ],
)
async def test_mode_select_refuses_without_setpoint_or_destination_range(body):
    entity = ILetComfortKjrh120lModeSelect(_coordinator(body))

    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("cool")

    entity.coordinator.async_execute_plan.assert_not_awaited()


async def test_mode_select_refuses_unknown_option():
    entity = ILetComfortKjrh120lModeSelect(_coordinator(KJRH120L_SNAPSHOT_A))

    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("auto")

    entity.coordinator.async_execute_plan.assert_not_awaited()


def test_mode_select_keeps_previous_unique_id_and_name():
    entity = ILetComfortKjrh120lModeSelect(_coordinator(KJRH120L_SNAPSHOT_A))

    assert entity.unique_id == "APPL1_kjrh120l_mode"
    assert entity.name == "Mode"


# --- Zone 1 power switch -------------------------------------------------------


def test_zone1_power_switch_reads_body8():
    on = ILetComfortKjrh120lZone1PowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A))
    off = ILetComfortKjrh120lZone1PowerSwitch(_coordinator(KJRH120L_SNAPSHOT_B))

    assert on.is_on is True
    assert off.is_on is False
    assert off.available is True


async def test_zone1_power_switch_writes_field_00():
    entity = ILetComfortKjrh120lZone1PowerSwitch(_coordinator(KJRH120L_SNAPSHOT_B))

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert sent_commands(entity.coordinator) == ["00000101ff", "00000100ff"]


def test_zone1_power_switch_keeps_previous_unique_id_and_name():
    entity = ILetComfortKjrh120lZone1PowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A))

    assert entity.unique_id == "APPL1_kjrh120l_zone1_power"
    assert entity.name == "Zone 1 Power"


# --- DHW power switch ---------------------------------------------------------


def test_dhw_power_switch_reads_body10_and_handles_truncated_or_none():
    on = ILetComfortKjrh120lDhwPowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A))
    off = ILetComfortKjrh120lDhwPowerSwitch(
        _coordinator(_with(KJRH120L_SNAPSHOT_A, b10=0))
    )
    short = ILetComfortKjrh120lDhwPowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A[:10]))
    no_data_coord = _coordinator(KJRH120L_SNAPSHOT_A)
    no_data_coord.data = None
    no_data = ILetComfortKjrh120lDhwPowerSwitch(no_data_coord)

    assert on.is_on is True
    assert off.is_on is False
    assert short.is_on is None
    assert no_data.is_on is None


async def test_dhw_power_switch_writes_mode_on_off():
    entity = ILetComfortKjrh120lDhwPowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A))

    await entity.async_turn_on()
    await entity.async_turn_off()

    assert sent_commands(entity.coordinator) == ["00020101ff", "00020100ff"]


def test_dhw_power_switch_unique_id_and_name():
    entity = ILetComfortKjrh120lDhwPowerSwitch(_coordinator(KJRH120L_SNAPSHOT_A))

    assert entity.unique_id == "APPL1_kjrh120l_dhw_power"
    assert entity.name == "DHW Power"


def test_client_kjrh120l_dhw_power_commands():
    client = ILetComfortClient(api_base="https://us.dollin.net")
    adapter = get_adapter(KJRH120L_SN8)
    with patch.object(client, "send_hex_command", return_value="") as mock_send:
        for on in (True, False):
            plan = adapter.plan_write(SetDhwPowerCommand(on=on), WriteContext())
            run_write_plan(plan, lambda hex_: client.send_hex_command("APPL1", hex_))

    assert mock_send.call_args_list[0].args == ("APPL1", "00020101ff")
    assert mock_send.call_args_list[1].args == ("APPL1", "00020100ff")


# --- Platform setup and the capability latch ----------------------------------

_NEW_ENTITIES = (
    ("number", "APPL1_kjrh120l_zone1_setpoint"),
    ("select", "APPL1_kjrh120l_mode"),
    ("switch", "APPL1_kjrh120l_zone1_power"),
    ("switch", "APPL1_kjrh120l_dhw_power"),
)


def _status_for(sn8: str | None, body: bytes):
    return apply_profile_to_status(
        resolve_profile(sn8), decode_its_status(bytearray(body))
    )


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


async def _setup(
    hass: HomeAssistant,
    client: MagicMock,
    sn8: str | None,
    body: bytes,
    *,
    latched: bool = False,
) -> MockConfigEntry:
    data = {
        CONF_EMAIL: "user@example.com",
        CONF_PASSWORD: "secret",
        CONF_APPLIANCE_CODE: "APPL1",
        CONF_REGION: REGION_US,
    }
    if latched:
        data[CONF_KJRH120L_ZONE1_CAPABLE] = True
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="user@example.com:APPL1", data=data, version=2
    )
    entry.add_to_hass(hass)
    client.list_appliances.return_value = [{"applianceCode": "APPL1", "sn8": sn8}]
    client.query_status.return_value = _status_for(sn8, body)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _entity_ids(hass: HomeAssistant) -> dict[str, str | None]:
    registry = er.async_get(hass)
    return {
        unique_id: registry.async_get_entity_id(platform, DOMAIN, unique_id)
        for platform, unique_id in _NEW_ENTITIES
    }


def _existing_entities_present(hass: HomeAssistant) -> bool:
    registry = er.async_get(hass)
    return all(
        registry.async_get_entity_id(platform, DOMAIN, unique_id)
        for platform, unique_id in (
            ("select", "APPL1_mute"),
            ("switch", "APPL1_boost"),
            ("climate", "APPL1_climate"),
        )
    )


async def test_dual_unit_gets_entities_at_setup_and_latches_without_reload(
    hass: HomeAssistant, mock_client
):
    entry = await _setup(hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_A)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    await hass.async_block_till_done()

    assert entry.data[CONF_KJRH120L_ZONE1_CAPABLE] is True
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is coordinator
    ids = _entity_ids(hass)
    assert all(ids.values())
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_setpoint"]).state == "50.0"
    assert hass.states.get(ids["APPL1_kjrh120l_mode"]).state == "heat"
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_power"]).state == STATE_ON
    assert hass.states.get(ids["APPL1_kjrh120l_dhw_power"]).state == STATE_ON
    assert _existing_entities_present(hass)


@pytest.mark.parametrize("sn8", [None, "171H120F", "171000AU", "17100007"])
async def test_non_kjrh_unit_gets_no_zone1_entities_even_with_flag(
    hass: HomeAssistant, mock_client, sn8
):
    entry = await _setup(hass, mock_client, sn8, KJRH120L_SNAPSHOT_A, latched=True)

    assert not any(_entity_ids(hass).values())
    assert _existing_entities_present(hass)

    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()
    assert not any(_entity_ids(hass).values())
    mock_client.send_hex_command.assert_not_called()


async def test_never_dual_kjrh_unit_gets_no_zone1_entities(
    hass: HomeAssistant, mock_client
):
    entry = await _setup(hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_B)
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()

    assert CONF_KJRH120L_ZONE1_CAPABLE not in entry.data
    assert not any(_entity_ids(hass).values())
    assert _existing_entities_present(hass)


async def test_latched_unit_restarting_with_zone_off_keeps_entities_available(
    hass: HomeAssistant, mock_client
):
    await _setup(
        hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_B, latched=True
    )

    ids = _entity_ids(hass)
    assert all(ids.values())
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_power"]).state == STATE_OFF
    assert hass.states.get(ids["APPL1_kjrh120l_dhw_power"]).state == STATE_ON
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_setpoint"]).state == "37.0"
    assert hass.states.get(ids["APPL1_kjrh120l_mode"]).state == "heat"

    # DHW setpoint number and read-only climate stay consistent when Zone 1 is off.
    registry = er.async_get(hass)
    dhw_id = registry.async_get_entity_id("number", DOMAIN, "APPL1_kjrh120l_dhw_setpoint")
    assert dhw_id is not None
    assert hass.states.get(dhw_id).state == "35.0"
    climate_id = registry.async_get_entity_id("climate", DOMAIN, "APPL1_climate")
    climate_state = hass.states.get(climate_id)
    assert climate_state.attributes["supported_features"] == 0


async def test_zone_switched_off_after_setup_keeps_entities_available(
    hass: HomeAssistant, mock_client
):
    entry = await _setup(hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_A)
    mock_client.query_status.return_value = _status_for(
        KJRH120L_SN8, KJRH120L_SNAPSHOT_B
    )

    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()

    ids = _entity_ids(hass)
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_power"]).state == STATE_OFF
    assert hass.states.get(ids["APPL1_kjrh120l_dhw_power"]).state == STATE_ON
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_setpoint"]).state == "37.0"
    assert entry.data[CONF_KJRH120L_ZONE1_CAPABLE] is True

    registry = er.async_get(hass)
    dhw_id = registry.async_get_entity_id("number", DOMAIN, "APPL1_kjrh120l_dhw_setpoint")
    assert dhw_id is not None
    assert hass.states.get(dhw_id).state == "35.0"
    climate_id = registry.async_get_entity_id("climate", DOMAIN, "APPL1_climate")
    climate_state = hass.states.get(climate_id)
    assert climate_state.attributes["supported_features"] == 0


def test_latched_dual_climate_keeps_zone1_target_when_zone_off():
    from custom_components.iletcomfort.climate import ILetComfortClimate

    coord = _coordinator(KJRH120L_SNAPSHOT_B)
    coord.kjrh120l_zone1_capable = True
    entity = ILetComfortClimate(coord)

    assert entity.supported_features == 0
    assert entity.target_temperature == 37.0


async def test_late_capability_adds_entities_without_reload(
    hass: HomeAssistant, mock_client, caplog
):
    entry = await _setup(hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_B)
    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert not any(_entity_ids(hass).values())

    mock_client.query_status.return_value = _status_for(
        KJRH120L_SN8, KJRH120L_SNAPSHOT_A
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    ids = _entity_ids(hass)
    assert all(ids.values())
    assert entry.data[CONF_KJRH120L_ZONE1_CAPABLE] is True
    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is coordinator
    assert hass.states.get(ids["APPL1_kjrh120l_zone1_setpoint"]).state == "50.0"

    # The one-shot listener removed itself: later refreshes add nothing twice.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_options_change_still_reloads_entry(hass: HomeAssistant, mock_client):
    from custom_components.iletcomfort.const import (
        CONF_OPERATION_MODE,
        OPERATION_MODE_HA_PRIMARY,
    )

    entry = await _setup(hass, mock_client, KJRH120L_SN8, KJRH120L_SNAPSHOT_A)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.config_entries.async_update_entry(
        entry, options={CONF_OPERATION_MODE: OPERATION_MODE_HA_PRIMARY}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert hass.data[DOMAIN][entry.entry_id] is not coordinator
