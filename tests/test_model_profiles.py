"""Tests for sn8-gated model decode profiles (issues #22 and #12).

Different C3 heat-pump models pack their status/sensor frames differently, but
the cloud metadata exposes no device-class field — both an air-to-water (ATW)
unit and an air-to-air unit report ``applianceType="0xC3"`` /
``modelNumber="0"``. The only differentiator is the 8-char ``sn8`` model-code
serial prefix, so model-specific decoding is gated on an sn8 lookup table.

These tests pin:
- STANDARD (unknown / None sn8): byte-for-byte identical to the legacy decode.
- ATW (sn8 ``171H120F``, issue #22): the 25-byte status layout.
- AQUAPURA (sn8 ``171000AU``, issue #12): water temp sourced from
  ``status.box_bottom_temp`` instead of ``sensors.twin_temp``.
"""

from __future__ import annotations

import pytest

from custom_components.iletcomfort.api import (
    ILetComfortClient,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.model_profiles import (
    AQUAPURA_SN8,
    ATW_SN8,
    ModelProfile,
    apply_profile_to_sensors,
    apply_profile_to_status,
    decode_atw_status,
    resolve_profile,
)


def _make_client() -> ILetComfortClient:
    return ILetComfortClient(api_base="https://us.dollin.net")


def _c3_frame(body: bytes) -> str:
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x04])
    return (header + body + b"\x00").hex()


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


# --- The five real ATW (Italtherm, sn8 171H120F) status frames, issue #22 ---
ATW_FRAMES = {
    "state1": _bytes(
        "01,08,07,68,03,03,1e,28,32,2a,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state2": _bytes(
        "01,0d,07,68,03,03,1e,28,32,3a,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state3": _bytes(
        "01,0c,07,68,03,03,1e,28,2d,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2e,00,80"
    ),
    "state4": _bytes(
        "01,0c,07,68,03,03,1e,28,32,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2d,00,80"
    ),
    "state5": _bytes(
        "01,08,07,68,03,03,1e,28,32,28,37,19,19,05,41,23,19,05,3c,22,3c,14,2d,00,80"
    ),
}

# Expected per-frame values (DHW setpoint, Zone-1 setpoint °C, DHW tank °C).
ATW_EXPECTED = {
    "state1": (50, 21.0, 46.0),
    "state2": (50, 29.0, 46.0),
    "state3": (45, 20.0, 46.0),
    "state4": (50, 20.0, 45.0),
    "state5": (50, 20.0, 45.0),
}


# ---------------------------------------------------------------------------
# Profile resolution
# ---------------------------------------------------------------------------

def test_resolve_profile_known_sn8():
    assert resolve_profile(ATW_SN8) is ModelProfile.ATW
    assert resolve_profile(AQUAPURA_SN8) is ModelProfile.AQUAPURA


def test_resolve_profile_unknown_or_none_falls_back_to_standard():
    assert resolve_profile(None) is ModelProfile.STANDARD
    assert resolve_profile("") is ModelProfile.STANDARD
    assert resolve_profile("UNKNOWN8") is ModelProfile.STANDARD
    assert resolve_profile("00000000") is ModelProfile.STANDARD


# ---------------------------------------------------------------------------
# ATW status layout (issue #22)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", list(ATW_FRAMES))
def test_atw_decode_field_mappings(name):
    """Each captured ATW frame decodes to the validated DHW/zone-1/tank values."""
    dhw_set, zone1, tank = ATW_EXPECTED[name]
    status = decode_atw_status(ATW_FRAMES[name])

    # DHW setpoint = byte[8], direct value (NOT offset-decoded).
    assert status.set_temperature == dhw_set
    # Zone-1 / climate target setpoint = byte[9] / 2 (0.5° resolution),
    # surfaced via t5s_def so the climate entity reads it unchanged.
    assert status.t5s_def == zone1
    # DHW tank current temp = byte[22], direct value, surfaced via box_bottom_temp.
    assert status.box_bottom_temp == tank
    # byte[24] = 0x80 is a flags/MSB byte, not an error code; the app shows no
    # fault, so the ATW profile must report no error.
    assert status.error_code == 0
    # No confirmed running signal in these frames → conservatively not running.
    assert not status.comp_running


def test_atw_space_heat_demand_flag():
    """byte[1] bit0 marks space-heat demand; only state2 has it set."""
    assert decode_atw_status(ATW_FRAMES["state2"]).status_flags_raw & 0x01
    for name in ("state1", "state3", "state4", "state5"):
        assert not (decode_atw_status(ATW_FRAMES[name]).status_flags_raw & 0x01)


def test_atw_profile_applied_to_status_object():
    """apply_profile_to_status(ATW) re-decodes the frame via the ATW layout."""
    std = decode_its_status(ATW_FRAMES["state1"])
    # The STANDARD decode misreads this frame.
    assert std.error_code == 128
    assert std.set_temperature == 3

    atw = apply_profile_to_status(ModelProfile.ATW, std)
    assert atw.set_temperature == 50
    assert atw.t5s_def == 21.0
    assert atw.box_bottom_temp == 46.0
    assert atw.error_code == 0
    assert not atw.comp_running


def test_atw_current_temperature_reflects_dhw_tank():
    """For ATW the climate current temperature is the DHW tank reading (byte[22]).

    The climate entity reads ``sensors.twin_temp``; the ATW sensors override
    routes the tank temp there so the meaningful "current" value is shown.
    """
    sensors = decode_its_sensors(bytes([0x02]) + bytes(48))
    status = decode_atw_status(ATW_FRAMES["state1"])
    out = apply_profile_to_sensors(ModelProfile.ATW, sensors, status)
    assert out.twin_temp == 46.0


# ---------------------------------------------------------------------------
# AQUAPURA water temperature source (issue #12)
# ---------------------------------------------------------------------------

# Representative AQUAPURA status frame: byte[17] = 0x4b → 75 → 75-35 = 40.0 °C
# is box_bottom_temp (water tank temp shown by the app). twin_temp source bytes
# are the 0x23 null-fill that decodes to 0 °C — the bug being fixed.
def _aquapura_status_body() -> bytes:
    body = bytearray([0x23] * 50)
    body[0] = 0x01
    body[17] = 0x4b  # status byte[17] = box_bottom_temp raw → 40.0 °C
    return bytes(body)


def _aquapura_sensors_body() -> bytes:
    # twin_temp = sensors byte[24] (d+23 region); 0x23 → 0x23-0x23... arranged
    # so twin_temp decodes to 0 °C. t4_temp byte[22]=0x36 → 19 °C (sanity).
    body = bytearray([0x23] * 50)
    body[0] = 0x02
    body[22] = 0x36  # t4_temp raw 0x36 = 54 → 19 °C
    body[25] = 0x23  # twin_temp raw 0x23 = 35 → 0.0 °C
    return bytes(body)


def test_aquapura_water_temp_from_box_bottom_temp():
    """AQUAPURA water/current temp must source box_bottom_temp, not twin_temp."""
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())

    # Baseline bug: twin_temp decodes to 0 while box_bottom_temp holds 40 °C.
    assert sensors.twin_temp == 0.0
    assert status.box_bottom_temp == 40.0

    out = apply_profile_to_sensors(ModelProfile.AQUAPURA, sensors, status)
    assert out.twin_temp == 40.0


def test_aquapura_ambient_unchanged():
    """The AQUAPURA override must not disturb the (already-correct) ambient temp."""
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())
    out = apply_profile_to_sensors(ModelProfile.AQUAPURA, sensors, status)
    assert out.t4_temp == 19.0


# ---------------------------------------------------------------------------
# STANDARD regression — unknown / None sn8 must decode exactly as before
# ---------------------------------------------------------------------------

# Reuse the real MSC-70D2N8-A frames (issue #11) already pinned in test_api.py.
ISSUE_11_STATUS_BODY = _bytes(
    "01,01,01,42,4b,2d,3f,37,0f,23,37,25,28,c0,00,00,80,23,2d,3f,37,00,"
    "23,37,00,3f,00,00,00,00,00,00,01,c2,00,00,01,00,0a,dc,04,e2,00,33,"
    "00,00,00,16,00,00,01"
)
ISSUE_11_SENSORS_BODY = _bytes(
    "02,01,00,00,00,00,00,00,00,00,00,00,01,02,00,29,49,12,00,01,00,33,"
    "3e,41,69,40,42,ef,04,00,eb,03,31,37,0c,31,37,0c,31,67,23,00,00,01,"
    "72,00,00,00,00,00,00"
)


def test_standard_profile_status_is_byte_identical_to_legacy():
    """STANDARD profile leaves decode_its_status output unchanged (regression)."""
    legacy = decode_its_status(ISSUE_11_STATUS_BODY)
    out = apply_profile_to_status(ModelProfile.STANDARD, legacy)
    assert out is legacy  # no copy, no mutation for the default path


@pytest.mark.parametrize("sn8", [None, "", "UNKNOWN8", "00000000"])
def test_standard_profile_sensors_unchanged_for_unknown_sn8(sn8):
    """An unknown/None sn8 resolves to STANDARD and does not touch the sensors."""
    profile = resolve_profile(sn8)
    status = decode_its_status(ISSUE_11_STATUS_BODY)
    sensors = decode_its_sensors(ISSUE_11_SENSORS_BODY)
    out = apply_profile_to_sensors(profile, sensors, status)
    assert out is sensors
    assert out.twin_temp == sensors.twin_temp


def test_query_status_standard_path_unchanged_with_unknown_sn8():
    """query_status with an unknown sn8 returns the identical legacy decode."""
    client = _make_client()
    with patch_send(client, _c3_frame(ISSUE_11_STATUS_BODY)):
        std = client.query_status("APPL1")
        with_sn8 = client.query_status("APPL1", sn8="UNKNOWN8")

    assert with_sn8.set_temperature == std.set_temperature
    assert with_sn8.error_code == std.error_code
    assert with_sn8.comp_running == std.comp_running


def test_query_status_atw_routes_through_atw_layout():
    """query_status with the ATW sn8 applies the ATW status layout."""
    client = _make_client()
    with patch_send(client, _c3_frame(ATW_FRAMES["state1"])):
        status = client.query_status("APPL1", sn8=ATW_SN8)

    assert status.set_temperature == 50
    assert status.t5s_def == 21.0
    assert status.box_bottom_temp == 46.0
    assert status.error_code == 0
    assert not status.comp_running


# Small context-manager helper to patch send_hex_command without importing
# unittest.mock at module top (keeps the import block focused on the SUT).
def patch_send(client, return_value):
    from unittest.mock import patch

    return patch.object(client, "send_hex_command", return_value=return_value)
