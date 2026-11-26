"""Tests for typed semantic command intents and coordinator dispatch (#113)."""

from __future__ import annotations

import math
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    MODE_COOL,
    MODE_HEAT,
    MODE_OFF,
    MODE_WATERPUMP,
    ITSSensors,
)
from custom_components.iletcomfort.c3_basic import FEATURE_ECO, FEATURE_SILENT
from custom_components.iletcomfort.commands import (
    SetBoostCommand,
    SetDhwPowerCommand,
    SetDhwTargetCommand,
    SetEcoModeCommand,
    SetFastDhwCommand,
    SetHvacModeCommand,
    SetPowerCommand,
    SetSilentModeCommand,
    SetTargetTemperatureCommand,
    SetZoneCurveCommand,
    SetZonePowerCommand,
    SetZoneRunModeCommand,
    SetZoneTargetCommand,
)
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.model_profiles import (
    ATW_SN8,
    KJRH120L_RUN_MODE_COOL,
    KJRH120L_SN8,
    decode_atw_status,
    decode_kjrh120l_status,
)
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A
from tests.test_c3_basic import KAISAI_DUAL_ZONE


def _make_coordinator(
    hass: HomeAssistant,
    *,
    sn8: str | None = None,
    c3_basic_control: bool = False,
) -> ILetComfortCoordinator:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "u@example.com",
            CONF_PASSWORD: "p",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {"sn8": sn8} if sn8 else None
    coord._c3_basic_control = c3_basic_control
    coord.async_set_device = AsyncMock()  # type: ignore[method-assign]
    coord.async_set_c3_basic = AsyncMock()  # type: ignore[method-assign]
    coord.async_set_c3_feature = AsyncMock()  # type: ignore[method-assign]
    return coord


def test_command_validation_rejects_invalid_and_contradictory_intents() -> None:
    with pytest.raises(ValueError, match="HVAC mode"):
        SetHvacModeCommand("auto")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="finite"):
        SetTargetTemperatureCommand(math.nan)
    with pytest.raises(ValueError, match="finite"):
        SetDhwTargetCommand(math.inf)
    with pytest.raises(ValueError, match="finite"):
        SetTargetTemperatureCommand(True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="either level or enabled"):
        SetSilentModeCommand()
    with pytest.raises(ValueError, match="both level and enabled"):
        SetSilentModeCommand(level=1, enabled=True)
    with pytest.raises(ValueError, match="Silent level"):
        SetSilentModeCommand(level=3)
    with pytest.raises(ValueError, match="Silent level"):
        SetSilentModeCommand(level=True)
    with pytest.raises(ValueError, match="Silent level"):
        SetSilentModeCommand(level=1.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Silent enabled"):
        SetSilentModeCommand(enabled="off")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Zone must be 1 or 2"):
        SetZonePowerCommand(zone=3, on=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Zone must be 1 or 2"):
        SetZonePowerCommand(zone=1.0, on=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Zone must be 1 or 2"):
        SetZonePowerCommand(zone=True, on=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Zone must be 1 or 2"):
        SetZoneCurveCommand(zone=0, enabled=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Zone 1"):
        SetZoneRunModeCommand(zone=2, mode="heat")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Zone run mode"):
        SetZoneRunModeCommand(zone=1, mode="auto")  # type: ignore[arg-type]


async def test_async_send_command_dispatches_standard_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)

    await coord.async_send_command(SetPowerCommand(on=True))
    coord.async_set_device.assert_awaited_with(power_on=True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetPowerCommand(on=False))
    coord.async_set_device.assert_awaited_with(mode=MODE_OFF)  # type: ignore[attr-defined]

    await coord.async_send_command(SetHvacModeCommand("cool"))
    coord.async_set_device.assert_awaited_with(mode=MODE_COOL)  # type: ignore[attr-defined]

    await coord.async_send_command(SetHvacModeCommand("fan_only"))
    coord.async_set_device.assert_awaited_with(mode=MODE_WATERPUMP)  # type: ignore[attr-defined]

    await coord.async_send_command(SetTargetTemperatureCommand(28.0))
    coord.async_set_device.assert_awaited_with(temperature=28)  # type: ignore[attr-defined]

    await coord.async_send_command(SetBoostCommand(enabled=True))
    coord.async_set_device.assert_awaited_with(boost=True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetSilentModeCommand(level=2))
    coord.async_set_device.assert_awaited_with(mute=2)  # type: ignore[attr-defined]


async def test_async_send_command_dispatches_atw_c3_basic_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, sn8=ATW_SN8, c3_basic_control=True)
    coord.data = {
        "status": decode_atw_status(KAISAI_DUAL_ZONE),
        "sensors": ITSSensors(),
    }

    await coord.async_send_command(SetHvacModeCommand("heat"))
    coord.async_set_c3_basic.assert_awaited_with(zone1_power=True)  # type: ignore[attr-defined]

    with pytest.raises(HomeAssistantError, match="only Heat and Off"):
        await coord.async_send_command(SetHvacModeCommand("cool"))

    await coord.async_send_command(SetTargetTemperatureCommand(36.0))
    coord.async_set_c3_basic.assert_awaited_with(zone1_water_target=36.0)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZonePowerCommand(zone=2, on=False))
    coord.async_set_c3_basic.assert_awaited_with(zone2_power=False)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZoneTargetCommand(zone=2, temperature=27.0))
    coord.async_set_c3_basic.assert_awaited_with(zone2_water_target=27.0)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZoneCurveCommand(zone=1, enabled=False))
    coord.async_set_c3_basic.assert_awaited_with(zone1_curve=False)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZoneCurveCommand(zone=2, enabled=True))
    coord.async_set_c3_basic.assert_awaited_with(zone2_curve=True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetDhwPowerCommand(on=True))
    coord.async_set_c3_basic.assert_awaited_with(dhw_power=True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetDhwTargetCommand(50.0))
    coord.async_set_c3_basic.assert_awaited_with(dhw_target=50.0)  # type: ignore[attr-defined]

    await coord.async_send_command(SetFastDhwCommand(enabled=True))
    coord.async_set_c3_basic.assert_awaited_with(fast_dhw=True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetSilentModeCommand(level=1))
    coord.async_set_c3_feature.assert_awaited_with(  # type: ignore[attr-defined]
        FEATURE_SILENT, True, silent_level=1
    )

    await coord.async_send_command(SetSilentModeCommand(enabled=True))
    coord.async_set_c3_feature.assert_awaited_with(FEATURE_SILENT, True)  # type: ignore[attr-defined]

    await coord.async_send_command(SetPowerCommand(on=True))
    coord.async_set_c3_basic.assert_awaited_with(zone1_power=True)  # type: ignore[attr-defined]

    # Room-temperature mode routes SetTargetTemperatureCommand to room_target.
    room_frame = bytearray(KAISAI_DUAL_ZONE)
    room_frame[2] &= ~0x10
    coord.data["status"] = decode_atw_status(bytes(room_frame))
    await coord.async_send_command(SetTargetTemperatureCommand(22.5))
    coord.async_set_c3_basic.assert_awaited_with(room_target=22.5)  # type: ignore[attr-defined]

    await coord.async_send_command(SetEcoModeCommand(enabled=True))
    coord.async_set_c3_feature.assert_awaited_with(FEATURE_ECO, True)  # type: ignore[attr-defined]


async def test_async_send_command_dispatches_kjrh120l_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, sn8=KJRH120L_SN8)
    coord.data = {
        "status": decode_kjrh120l_status(KJRH120L_SNAPSHOT_A),
        "sensors": ITSSensors(),
    }

    await coord.async_send_command(SetDhwPowerCommand(on=True))
    coord.async_set_device.assert_awaited_with(mode=MODE_HEAT)  # type: ignore[attr-defined]

    await coord.async_send_command(SetDhwTargetCommand(52.0))
    coord.async_set_device.assert_awaited_with(temperature=52)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZonePowerCommand(zone=1, on=False))
    coord.async_set_device.assert_awaited_with(zone1_power=False)  # type: ignore[attr-defined]

    await coord.async_send_command(SetZoneTargetCommand(zone=1, temperature=45.0))
    coord.async_set_device.assert_awaited_with(zone1_temperature=45)  # type: ignore[attr-defined]

    # Switching KJRH-120L to Cool clamps the 50 °C heating target into 5-25 °C Cool range.
    await coord.async_send_command(SetZoneRunModeCommand(zone=1, mode="cool"))
    coord.async_set_device.assert_awaited_with(  # type: ignore[attr-defined]
        zone1_run_mode=KJRH120L_RUN_MODE_COOL, zone1_temperature=25
    )
