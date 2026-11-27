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
    build_c3_query,
)
from custom_components.iletcomfort.commands import (
    HeatPumpCommand,
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
from custom_components.iletcomfort.protocols.atw import decode_atw_status
from custom_components.iletcomfort.protocols.kjrh120l import decode_kjrh120l_status
from custom_components.iletcomfort.protocols.profile import ATW_SN8, KJRH120L_SN8
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A
from tests.test_api import ISSUE_11_STATUS_BODY
from tests.test_c3_basic import (
    KAISAI_DUAL_ZONE,
    ROTENSO_ECO_QUERY,
    ROTENSO_SILENCE_QUERY,
    SET_ECO_ON,
    SET_SILENT_OFF,
    SET_SILENT_ON,
)
from tests.snapshot_helpers import make_state

Q01 = build_c3_query(0x01)
Q05 = build_c3_query(0x05)
Q07 = build_c3_query(0x07)


def _reply(body: bytes) -> str:
    return (bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x03]) + body + b"\x00").hex()


ISSUE_11_REPLY = _reply(ISSUE_11_STATUS_BODY)


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
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]
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


class _Transport:
    """Fake cloud transport: records every hex command, answers fresh reads."""

    def __init__(self, replies: dict[str, str] | None = None) -> None:
        self.replies = replies or {}
        self.sent: list[str] = []

    def __call__(self, _appliance_code: str, command_hex: str) -> str:
        self.sent.append(command_hex)
        return self.replies.get(command_hex, "ack")

    def pop(self) -> list[str]:
        sent, self.sent = self.sent, []
        return sent


def _wire(coord: ILetComfortCoordinator, replies: dict[str, str] | None = None) -> _Transport:
    transport = _Transport(replies)
    coord.client.send_hex_command = transport  # type: ignore[method-assign]
    return transport


async def test_async_send_command_dispatches_standard_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass)
    transport = _wire(coord, {Q01: ISSUE_11_REPLY})

    async def _set_frame(command: HeatPumpCommand) -> bytes:
        await coord.async_send_command(command)
        sent = transport.pop()
        assert sent[0] == Q01  # fresh status echo read inside the transaction
        assert len(sent) == 2
        return bytes.fromhex(sent[1])

    # Power-on restores the last on-state (mode, temperature).
    coord._last_on_state = (MODE_COOL, 22)
    frame = await _set_frame(SetPowerCommand(on=True))
    assert (frame[12], frame[13] - 35) == (MODE_COOL, 22)
    coord._last_on_state = None

    assert (await _set_frame(SetPowerCommand(on=False)))[12] == MODE_OFF
    assert (await _set_frame(SetHvacModeCommand("cool")))[12] == MODE_COOL
    assert (await _set_frame(SetHvacModeCommand("fan_only")))[12] == MODE_WATERPUMP
    assert (await _set_frame(SetTargetTemperatureCommand(28.0)))[13] == 28 + 35
    assert (await _set_frame(SetBoostCommand(enabled=True)))[25:27] == bytes([0, 2])
    assert (await _set_frame(SetSilentModeCommand(level=2)))[25:27] == bytes([1, 1])
    assert (await _set_frame(SetDhwPowerCommand(on=True)))[12] == MODE_HEAT


async def test_async_send_command_dispatches_atw_c3_basic_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, sn8=ATW_SN8, c3_basic_control=True)
    coord.data = make_state(decode_atw_status(KAISAI_DUAL_ZONE), ITSSensors(), sn8=coord.sn8)
    transport = _wire(
        coord,
        {
            Q01: _reply(KAISAI_DUAL_ZONE),
            Q05: _reply(ROTENSO_SILENCE_QUERY),
            Q07: _reply(ROTENSO_ECO_QUERY),
        },
    )

    async def _basic_set(command: HeatPumpCommand) -> bytes:
        await coord.async_send_command(command)
        sent = transport.pop()
        assert sent[0] == Q01  # fresh status read immediately before the SET
        assert len(sent) == 2
        frame = bytes.fromhex(sent[1])
        assert frame[9:11] == bytes([0x02, 0x01])
        return frame[11:18]  # [power, mode, z1, z2, dhw, room*2, flags]

    # Kaisai baseline: power 0x07 (z1, z2, DHW), heat, Z1 35, Z2 25, DHW 44,
    # room 0x30, curve flags 0x03.
    assert await _basic_set(SetHvacModeCommand("heat")) == bytes.fromhex("070323192c3003")

    with pytest.raises(HomeAssistantError, match="only Heat and Off"):
        await coord.async_send_command(SetHvacModeCommand("cool"))
    assert transport.pop() == []

    assert (await _basic_set(SetTargetTemperatureCommand(36.0)))[2] == 36
    assert (await _basic_set(SetZonePowerCommand(zone=2, on=False)))[0] == 0x05
    assert (await _basic_set(SetZoneTargetCommand(zone=2, temperature=27.0)))[3] == 27
    assert (await _basic_set(SetZoneCurveCommand(zone=1, enabled=False)))[6] == 0x02
    assert (await _basic_set(SetZoneCurveCommand(zone=2, enabled=True)))[6] == 0x03
    assert (await _basic_set(SetDhwPowerCommand(on=False)))[0] == 0x03
    assert (await _basic_set(SetDhwTargetCommand(50.0)))[4] == 50
    assert (await _basic_set(SetFastDhwCommand(enabled=True)))[6] == 0x0B
    assert (await _basic_set(SetPowerCommand(on=False)))[0] == 0x06

    await coord.async_send_command(SetSilentModeCommand(level=1))
    assert transport.pop() == [Q05, SET_SILENT_ON]
    await coord.async_send_command(SetSilentModeCommand(enabled=False))
    assert transport.pop() == [Q05, SET_SILENT_OFF]
    await coord.async_send_command(SetEcoModeCommand(enabled=True))
    assert transport.pop() == [Q07, SET_ECO_ON]

    # Room-temperature mode routes SetTargetTemperatureCommand to room_target.
    room_frame = bytearray(KAISAI_DUAL_ZONE)
    room_frame[2] &= ~0x10
    coord.data = make_state(decode_atw_status(bytes(room_frame)), coord.data.sensors, sn8=coord.sn8)
    transport.replies[Q01] = _reply(bytes(room_frame))
    assert (await _basic_set(SetTargetTemperatureCommand(22.5)))[5] == 45


async def test_async_send_command_atw_without_opt_in_sends_nothing(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, sn8=ATW_SN8, c3_basic_control=False)
    coord.data = make_state(decode_atw_status(KAISAI_DUAL_ZONE), ITSSensors(), sn8=coord.sn8)
    transport = _wire(coord)

    with pytest.raises(HomeAssistantError, match="C3 basic control is not enabled"):
        await coord.async_send_command(SetDhwTargetCommand(50.0))
    with pytest.raises(HomeAssistantError, match="Heat pump command failed.*171H120F"):
        await coord.async_send_command(SetTargetTemperatureCommand(30.0))
    assert transport.sent == []
    coord.async_request_refresh.assert_not_awaited()  # type: ignore[attr-defined]


async def test_async_send_command_dispatches_kjrh120l_intents(
    hass: HomeAssistant,
) -> None:
    coord = _make_coordinator(hass, sn8=KJRH120L_SN8)
    coord.data = make_state(decode_kjrh120l_status(KJRH120L_SNAPSHOT_A), ITSSensors(), sn8=coord.sn8)
    transport = _wire(coord)

    await coord.async_send_command(SetDhwPowerCommand(on=True))
    await coord.async_send_command(SetDhwTargetCommand(52.0))
    await coord.async_send_command(SetZonePowerCommand(zone=1, on=False))
    await coord.async_send_command(SetZoneTargetCommand(zone=1, temperature=45.0))
    # Switching KJRH-120L to Cool clamps the 50 °C heating target into 5-25 °C Cool range.
    await coord.async_send_command(SetZoneRunModeCommand(zone=1, mode="cool"))

    # One captured short command per intent, never a status read.
    assert transport.sent == [
        "00020101ff",
        "00070134ff",
        "00000100ff",
        "0004012dff",
        "00030102ff00040119ff",
    ]
