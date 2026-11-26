"""Tests for ATW subtypes 0x05 (Silent/Super-Silent) and 0x07 (ECO) (#139/#188)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSSensors,
    ITSStatus,
    build_c3_query,
    decode_its_sensors,
)
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.diagnostics import async_get_config_entry_diagnostics
from custom_components.iletcomfort.model_profiles import (
    ATW_SN8,
    ModelProfile,
    apply_atw_eco,
    apply_atw_silent,
    apply_profile_to_sensors,
    decode_atw_status,
)
from custom_components.iletcomfort.select import ILetComfortMuteSelect


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


AIRWELL_0X01 = _bytes(
    "01,09,73,90,03,03,28,1e,32,30,41,23,19,05,37,19,19,05,3c,22,3c,14,19,00,e0,05,00"
)
AIRWELL_0X02 = _bytes(
    "02,00,03,28,00,00,00,00,03,28,00,00,00,00,03,28,00,00,00,00,"
    "03,28,00,00,00,00,03,28,00,00,00,00,03,28,00,00,00,00"
)


def _c3_frame(body: bytes) -> str:
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x03])
    return (header + body + b"\x00").hex()


@pytest.mark.parametrize(
    ("frame_hex", "expected_on", "expected_level", "expected_super_sel"),
    [
        ("05,03,06,1e,09,1e,16,00,07,00", True, 1, False),   # L1 scheduled
        ("05,01,06,1e,09,1e,16,00,07,00", True, 1, False),   # L1 forced
        ("05,09,06,1e,09,1e,16,00,07,00", True, 2, True),    # Super Silent forced
        ("05,08,06,1e,09,1e,16,00,07,00", False, 0, True),   # OFF (bit 3 retained)
        ("05,0b,06,1e,09,1e,16,00,07,00", True, 2, True),    # Super Silent scheduled
    ],
)
def test_apply_atw_silent_decodes_airwell_captures(
    frame_hex: str,
    expected_on: bool,
    expected_level: int,
    expected_super_sel: bool,
) -> None:
    """All 5 controlled Airwell 0x05 Silent captures (#139 C8) decode cleanly."""
    status = decode_atw_status(AIRWELL_0X01)
    out = apply_atw_silent(_bytes(frame_hex), status)
    assert out.atw_silent_mode is expected_on
    assert out.atw_silent_level == expected_level
    assert out.atw_silent_super_selected is expected_super_sel


@pytest.mark.parametrize(
    ("frame_hex", "expected_on", "expected_timer"),
    [
        ("07,03,10,00,16,1e,07", True, True),    # ECO scheduled
        ("07,01,10,00,16,1e,07", True, False),   # ECO forced ON
        ("07,00,10,00,16,1e,07", False, False),  # ECO OFF
    ],
)
def test_apply_atw_eco_decodes_airwell_captures(
    frame_hex: str,
    expected_on: bool,
    expected_timer: bool,
) -> None:
    """All 3 controlled Airwell 0x07 ECO captures (#139 C8) decode cleanly."""
    status = decode_atw_status(AIRWELL_0X01)
    out = apply_atw_eco(_bytes(frame_hex), status)
    assert out.atw_eco_mode is expected_on
    assert out.atw_eco_timer is expected_timer


def test_0x05_short_payload_length_discriminator_decodes_eco_fallback() -> None:
    """midea-local #300: 0x05 reply <= 6 bytes carries ECO, never Silent."""
    status = decode_atw_status(AIRWELL_0X01)
    short_eco = _bytes("05,03,00,00,00,00")
    out1 = apply_atw_silent(short_eco, status)
    assert out1.atw_eco_mode is True
    assert out1.atw_eco_timer is True
    assert out1.atw_silent_level is None

    out2 = apply_atw_eco(short_eco, status)
    assert out2.atw_eco_mode is True
    assert out2.atw_eco_timer is True

    # Conversely, a full 10-byte Silence 0x05 frame is ignored by apply_atw_eco.
    full_silent = _bytes("05,09,06,1e,09,1e,16,00,07,00")
    assert apply_atw_eco(full_silent, status) is status


def test_client_query_atw_silent_and_eco() -> None:
    client = ILetComfortClient(api_base="https://eu.dollin.net")
    silent_body = _bytes("05,09,06,1e,09,1e,16,00,07,00")
    eco_body = _bytes("07,03,10,00,16,1e,07")

    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(silent_body)
    ) as mock_send:
        assert client.query_atw_silent("APPL1") == silent_body
    mock_send.assert_called_once_with("APPL1", build_c3_query(0x05))

    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(eco_body)
    ) as mock_send:
        assert client.query_atw_eco("APPL1") == eco_body
    mock_send.assert_called_once_with("APPL1", build_c3_query(0x07))

    # Shared-0x05 ECO fallback reply accepted by query_atw_eco.
    shared_eco = _bytes("05,01,00,00,00,00")
    with patch.object(client, "send_hex_command", return_value=_c3_frame(shared_eco)):
        assert client.query_atw_eco("APPL1") == shared_eco

    with (
        patch.object(client, "send_hex_command", return_value=_c3_frame(b"\x05")),
        pytest.raises(ApiError, match="0x05"),
    ):
        client.query_atw_silent("APPL1")


def _atw_coordinator(hass: HomeAssistant) -> tuple[ILetComfortCoordinator, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="ATW",
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {"applianceCode": "APPL1", "sn8": ATW_SN8}
    client = cls.return_value
    client.query_status.return_value = decode_atw_status(AIRWELL_0X01)
    client.query_sensors.return_value = apply_profile_to_sensors(
        ModelProfile.ATW, decode_its_sensors(AIRWELL_0X02), client.query_status.return_value
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, client


async def test_coordinator_probes_silent_and_eco_and_wires_entities_and_diagnostics(
    hass: HomeAssistant,
) -> None:
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = ApiError("code=1214")
    client.query_atw_silent.return_value = _bytes("05,09,06,1e,09,1e,16,00,07,00")
    client.query_atw_eco.return_value = _bytes("07,03,10,00,16,1e,07")

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data = await coord._poll()
    coord.async_set_updated_data(data)

    assert coord.capabilities.atw_silent.supported is True
    assert coord.capabilities.atw_eco.supported is True
    assert data["status"].atw_silent_level == 2
    assert data["status"].atw_eco_mode is True

    mute = ILetComfortMuteSelect(coord)
    assert mute.current_option == "Level 2"

    # Switching to Silent OFF with bit 3 still set reports Off, not Level 2.
    client.query_atw_silent.return_value = _bytes("05,08,06,1e,09,1e,16,00,07,00")
    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        data = await coord._poll()
    coord.async_set_updated_data(data)
    assert mute.current_option == "Off"

    climate = ILetComfortClimate(coord)
    attrs = climate.extra_state_attributes
    assert attrs["eco_mode"] is True
    assert attrs["silent_level"] == "off"

    diag = await async_get_config_entry_diagnostics(hass, coord.entry)
    assert diag["protocol"]["probes"]["0x05_silent"] == {"supported": True, "error": None}
    assert diag["protocol"]["probes"]["0x07_eco"] == {"supported": True, "error": None}
    assert "0x05" in diag["protocol"]["raw_frames"]
    assert "0x07" in diag["protocol"]["raw_frames"]


async def test_coordinator_disables_0x05_and_0x07_on_initial_api_error_and_retries_after_ok(
    hass: HomeAssistant,
) -> None:
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = ApiError("code=1214")
    client.query_atw_silent.side_effect = ApiError("code=1214")
    client.query_atw_eco.side_effect = [
        _bytes("07,01,10,00,16,1e,07"),
        ApiError("transient"),
        _bytes("07,00,10,00,16,1e,07"),
    ]

    with patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()):
        d1 = await coord._poll()
        d2 = await coord._poll()
        d3 = await coord._poll()

    assert client.query_atw_silent.call_count == 1
    assert coord.capabilities.atw_silent.supported is False
    assert client.query_atw_eco.call_count == 3
    assert coord.capabilities.atw_eco.supported is True
    assert d1["status"].atw_eco_mode is True
    assert d2["status"].atw_eco_mode is None
    assert d3["status"].atw_eco_mode is False


def test_select_falls_back_to_c3_basic_silent_flag_when_0x05_not_probed(
    hass: HomeAssistant,
) -> None:
    coord, _ = _atw_coordinator(hass)
    body = bytearray(AIRWELL_0X01)
    body[3] = 0x92  # bit 1 silent active on 0x01
    coord.data = {
        "status": decode_atw_status(bytes(body)),
        "sensors": ITSSensors(),
    }
    assert ILetComfortMuteSelect(coord).current_option == "Level 1"
    # Pre-probe climate attributes fall back to basic.eco_mode and omit silent_level.
    attrs = ILetComfortClimate(coord).extra_state_attributes
    assert attrs["eco_mode"] is False
    assert "silent_level" not in attrs


def test_apply_atw_silent_and_eco_ignore_short_or_wrong_subtype() -> None:
    status = decode_atw_status(AIRWELL_0X01)
    for bad_silent in (b"", b"\x05", b"\x07\x01\x00"):
        assert apply_atw_silent(bad_silent, status) is status
    for bad_eco in (b"", b"\x07", b"\x01\x01"):
        assert apply_atw_eco(bad_eco, status) is status


def test_select_and_climate_keep_non_atw_behavior(hass: HomeAssistant) -> None:
    coord, _ = _atw_coordinator(hass)
    coord.appliance_meta = None
    coord.data = {
        "status": ITSStatus(mode=1),
        "sensors": ITSSensors(ctrl_flag=1, mute_level=1, twin_temp=22.0),
    }
    assert ILetComfortMuteSelect(coord).current_option == "Level 2"
    attrs = ILetComfortClimate(coord).extra_state_attributes
    assert "eco_mode" not in attrs
    assert "silent_level" not in attrs


async def test_coordinator_probe_bubbles_auth_error(hass: HomeAssistant) -> None:
    coord, client = _atw_coordinator(hass)
    client.query_atw_unit_params.side_effect = ApiError("code=1214")
    client.query_atw_silent.side_effect = AuthError("token expired")

    with (
        patch("custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()),
        pytest.raises(AuthError, match="token expired"),
    ):
        await coord._poll()

