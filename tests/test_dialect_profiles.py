"""Tests for sn8-gated model decode profiles (issues #22 and #12).

Different C3 heat-pump models pack their status/sensor frames differently, but
the cloud metadata exposes no device-class field — both an air-to-water (ATW)
unit and an air-to-air unit report ``applianceType="0xC3"`` /
``modelNumber="0"``. The only differentiator is the 8-char ``sn8`` model-code
serial prefix, so model-specific decoding is gated on an sn8 lookup table.

These tests pin:
- STANDARD (unknown / None sn8): byte-for-byte identical to the legacy decode.
- ATW (sn8 ``171H120F``, issue #22): the 25-byte status layout.
- AQUAPURA (sn8 ``171000AU``, issue #12): tank temp sourced from
  ``status.box_bottom_temp`` and surfaced on ``th_temp`` (the "DHW Tank
  Temperature" sensor) instead of ``sensors.twin_temp``.
"""

from __future__ import annotations

import pytest

from custom_components.iletcomfort.api import (
    ILetComfortClient,
    build_c3_query,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.protocols import get_adapter
from custom_components.iletcomfort.protocols.atw import (
    atw_sensors_are_placeholder,
    decode_atw_status,
)
from custom_components.iletcomfort.protocols.kjrh120l import (
    build_kjrh120l_set_temperature,
    decode_kjrh120l_status,
    kjrh120l_status,
    KJRH120L_DHW_OFF,
    KJRH120L_DHW_ON,
    KJRH120L_TEMP_MAX,
    KJRH120L_TEMP_MIN,
)
from custom_components.iletcomfort.protocols.profile import (
    AQUAPURA_SN8,
    ATW_SN8,
    KJRH120L2_SN8,
    KJRH120L_SN8,
    ModelProfile,
    resolve_profile,
)
from tests.snapshot_helpers import make_state


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

# Galmet Prima 8GT, sn8 171H120F (issue #38). Unlike the 25-byte Italtherm
# layout above, this variant emits a 27-byte status body. Same-moment official
# app readings independently establish Zone-1 target 25/28 C respectively.
GALMET_ATW_STATUS_FRAMES = {
    "target25": _bytes(
        "01,05,15,a0,03,03,19,1e,2c,30,37,19,19,05,37,19,19,05,3c,22,3c,14,27,00,e0,03,03"
    ),
    "target28": _bytes(
        "01,05,15,a0,03,03,1c,1e,2c,30,37,19,19,05,37,19,19,05,3c,22,3c,14,28,00,e0,03,03"
    ),
}

# NØRDIS Optimus Pro, sn8 171H120F (issue #132): the same 27-byte layout with
# different bytes at [2] and the [24:27] tail. Both captures were taken while
# the official app showed Zone-1 Heating: target 33 C / DHW 50 C / tank 46 C
# (#132) and target 35 C / DHW 60 C / tank 57 C a minute later (second unit).
NORDIS_ATW_STATUS_FRAMES = {
    "issue132": _bytes(
        "01,0d,15,a0,03,03,21,1e,32,30,37,19,19,05,37,19,19,05,3c,22,3c,14,2e,00,60,04,03"
    ),
    "unit2": _bytes(
        "01,0d,17,a0,03,03,23,1e,3c,30,37,19,19,05,37,19,19,05,3c,22,3c,14,3a,00,40,05,00"
    ),
}

# 25-byte status frames from other 171H120F brands (Rotenso #124, Kaisai #123).
ATW_25_BYTE_STATUS_FRAMES = {
    "rotenso": _bytes(
        "01,0d,15,90,03,03,28,1e,32,30,41,23,19,05,37,19,19,05,3c,22,3c,14,32,00,80"
    ),
    "kaisai": _bytes(
        "01,08,11,50,03,03,23,28,32,30,41,23,19,05,41,23,19,05,3c,22,3c,14,19,00,80"
    ),
}

# Galmet Prima 8GT, sn8 171H120F (issue #38). The cloud returned this repeated
# template instead of live sensor telemetry while the official app showed live
# Zone-1 water and outdoor readings. It must not surface as measurements.
ATW_PLACEHOLDER_SENSORS_BODY = _bytes(
    "02,00,03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00,"
    "03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00,00,03,1e,"
    "00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,"
    "00,00,03,1e,00,00,00,00,03,1e,00,00,00,00"
)

# Further non-telemetry variants of the same repeated-group template. Rotenso
# (#124) and Kaisai (#123): 38 bytes, constant 0x28, no separator. NØRDIS:
# Galmet's 75-byte shape, but four groups carry non-zero bytes (they read like
# hh,00,hh,00 time slots); the app's live water 26 C / outdoor 8 C appear nowhere.
ATW_TEMPLATE_SENSOR_VARIANTS = {
    "rotenso_kaisai": _bytes(
        "02,00,03,28,00,00,00,00,03,28,00,00,00,00,03,28,00,00,00,00,"
        "03,28,00,00,00,00,03,28,00,00,00,00,03,28,00,00,00,00"
    ),
    "nordis": _bytes(
        "02,00,03,1e,04,00,0a,00,03,1e,16,00,02,00,03,1e,0f,00,14,00,"
        "03,1e,05,00,14,00,03,1e,00,00,00,00,03,1e,00,00,00,00,00,03,1e,"
        "00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,"
        "00,00,03,1e,00,00,00,00,03,1e,00,00,00,00"
    ),
    # Airwell Wellea M (#128): 38-byte schedule frame with non-zero slot mask
    # (0x0a) and mixed 03/05 active time slots.
    "airwell_active_schedule": _bytes(
        "02,0a,03,28,05,00,0c,00,05,36,16,00,06,00,03,28,0c,00,10,00,"
        "05,34,11,00,14,00,03,28,16,00,05,00,03,28,00,00,00,00"
    ),
    # Sinclair MSH-100IB-3/9 (#150): 38-byte schedule frame where slot 1 holds
    # an active 05,32 window (11:00-15:00) while remaining slots are 03,28.
    "sinclair_active_schedule": _bytes(
        "02,00,05,32,0b,00,0f,00,03,28,12,00,16,1e,03,28,00,00,00,00,"
        "03,28,00,00,00,00,03,28,00,00,00,00,03,28,00,00,00,00"
    ),
    # Western MONO 2 (#124 Comment 1): 75-byte template with 02,0c (Cool 12 C) slots.
    "western_cool_template": _bytes(
        "02,00,02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00,"
        "02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00,00,"
        "02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00,"
        "02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00"
    ),
    # Kosner ATW (#134): 38-byte schedule mixing 03,19 / 03,28 (07:30->00:00) / 02,0c.
    "kosner_mixed_cool_schedule": _bytes(
        "02,00,03,19,00,00,00,00,03,28,07,1e,00,00,02,0c,00,00,00,00,"
        "02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00"
    ),
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


def test_resolve_profile_kjrh120l_sn8():
    """The KJRH-120L sn8 (17100003, issues #21/#5) resolves to its profile."""
    assert KJRH120L_SN8 == "17100003"
    assert resolve_profile(KJRH120L_SN8) is ModelProfile.KJRH120L


def test_known_unconfirmed_kjrh120l2_sn8_falls_back_to_standard():
    """The known 120L2 model remains on the safe default until hardware data exists."""
    assert KJRH120L2_SN8 == "17100007"
    assert resolve_profile(KJRH120L2_SN8) is ModelProfile.STANDARD


# ---------------------------------------------------------------------------
# Query command encoding (KJRH-120L short form, issues #21 / #5)
# ---------------------------------------------------------------------------

def test_build_query_kjrh120l_short_form():
    """KJRH-120L's cloud rejects the long C3 query (code 1214); its app uses a
    short ``ffff<ss><ss><ss>ff`` form (subtype byte repeated 3×)."""
    assert get_adapter(ModelProfile.KJRH120L).build_query(0x01) == "ffff010101ff"
    assert get_adapter(ModelProfile.KJRH120L).build_query(0x02) == "ffff020202ff"


@pytest.mark.parametrize("subtype", [0x01, 0x02])
def test_build_query_standard_matches_build_c3_query(subtype):
    """STANDARD must emit the legacy long C3 query frame, byte-for-byte."""
    assert get_adapter(ModelProfile.STANDARD).build_query(subtype) == build_c3_query(subtype)


@pytest.mark.parametrize("profile", [ModelProfile.ATW, ModelProfile.AQUAPURA])
@pytest.mark.parametrize("subtype", [0x01, 0x02])
def test_build_query_other_profiles_use_long_frame(profile, subtype):
    """Other model profiles keep the standard long C3 query frame, unchanged."""
    assert get_adapter(profile).build_query(subtype) == build_c3_query(subtype)


# --- KJRH-120L short WRITE commands (issue #35) ---------------------------
# The KJRH-120L's cloud rejects the standard 62-byte C3 SET frame (same reason
# its long query was rejected). The official app uses short literal write
# commands of shape ``00 <field> 01 <value> ff`` (no checksum byte; the cloud
# does the framing). Captured from the app's proxy traffic with code:0 replies:
#   set DHW setpoint to T °C  → 0007 01 <T:1 byte hex> ff   (field 0x07)
#   DHW power ON              → 00020101ff                  (field 0x02)
#   DHW power OFF             → 00020100ff


def test_build_kjrh120l_set_temperature_matches_captured_commands():
    """Confirmed captures: T=60 → 0007013cff, T=49 → 00070131ff."""
    assert build_kjrh120l_set_temperature(60) == "0007013cff"
    assert build_kjrh120l_set_temperature(49) == "00070131ff"


def test_build_kjrh120l_set_temperature_two_hex_digits():
    """Single-digit values are zero-padded to two hex digits (e.g. 9 → 09)."""
    assert build_kjrh120l_set_temperature(9) == "00070109ff"
    assert build_kjrh120l_set_temperature(15) == "0007010fff"


def test_kjrh120l_dhw_power_constants():
    """DHW power on/off are the literal captured command strings."""
    assert KJRH120L_DHW_ON == "00020101ff"
    assert KJRH120L_DHW_OFF == "00020100ff"


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


@pytest.mark.parametrize(
    ("name", "expected_target"),
    [("target25", 25.0), ("target28", 28.0)],
)
def test_galmet_atw_status_uses_direct_zone1_target(name, expected_target):
    """The 27-byte Galmet layout carries Zone-1 target directly at byte 6."""
    status = decode_atw_status(GALMET_ATW_STATUS_FRAMES[name])

    assert status.t5s_def == expected_target
    assert status.set_temperature == 44


@pytest.mark.parametrize(
    "body",
    [
        _bytes(
            "01,05,15,a0,03,03,23,1e,2c,30,37,19,19,05,37,19,19,05,"
            "3c,22,3c,14,2c,00,e0,03,03"
        ),
        _bytes(
            "01,05,15,a0,03,03,1c,1e,2c,30,37,19,19,05,37,19,19,05,"
            "3c,22,3c,14,2c,00,e0,03,03"
        ),
        _bytes(
            "01,04,15,a0,03,03,1c,1e,2c,30,37,19,19,05,37,19,19,05,"
            "3c,22,3c,14,2c,00,e0,03,03"
        ),
    ],
)
def test_galmet_atw_does_not_invent_compressor_telemetry(body):
    """Validated Galmet frames cannot support a running/frequency value."""
    status = decode_atw_status(body)

    assert status.comp_running is None
    assert status.comp_frq is None


@pytest.mark.parametrize(
    ("name", "target", "dhw_set", "tank"),
    [("issue132", 33.0, 50, 46.0), ("unit2", 35.0, 60, 58.0)],
)
def test_nordis_27_byte_status_uses_validated_27_byte_layout(name, target, dhw_set, tank):
    """NØRDIS 27-byte frames decode like Galmet despite a different tail (#132)."""
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES[name])

    assert status.mode_name == "Heat"
    assert status.t5s_def == target
    assert status.set_temperature == dhw_set
    assert status.box_bottom_temp == tank
    assert status.error_code == 0
    assert status.comp_running is None
    assert status.comp_frq is None


def test_nordis_27_byte_status_zone1_off_when_power_bit_clear():
    """Bit 0 of byte[1] is Zone-1 power on the 27-byte layout."""
    body = bytearray(NORDIS_ATW_STATUS_FRAMES["unit2"])
    body[1] = 0x0C

    assert decode_atw_status(bytes(body)).mode_name == "Off"


def test_25_byte_atw_frames_decode_via_c3_basic_layout():
    """All valid C3Basic 171H120F frames use Zone-1 power and cap_water selector."""
    body = ATW_25_BYTE_STATUS_FRAMES["kaisai"]
    status = decode_atw_status(body)

    # Kaisai [2]=0x11 has zone1_water_temp=True -> body[6] (35 °C), zone1_power=False.
    assert status.t5s_def == float(body[6])
    assert status.mode_name == "Off"
    assert status.comp_running is None
    assert status.comp_frq is None


def test_25_byte_rotenso_frame_decodes_zone1_power_and_water_target():
    """Rotenso [2]=0x15 (#124): Zone-1 power + zone1_water_temp selects body[6]."""
    body = ATW_25_BYTE_STATUS_FRAMES["rotenso"]
    status = decode_atw_status(body)

    assert status.t5s_def == float(body[6])
    assert status.mode_name == "Heat"
    assert status.comp_running is None


@pytest.mark.parametrize("mask", [0x05, 0x11, 0x13, 0x73])
def test_other_25_byte_capability_masks_decode_zone1_power_and_setpoint(mask):
    body = bytearray(ATW_25_BYTE_STATUS_FRAMES["rotenso"])
    body[2] = mask
    status = decode_atw_status(bytes(body))

    assert status.mode_name == "Heat"
    expected_target = float(body[6]) if (mask & 0x10) else body[9] / 2
    assert status.t5s_def == expected_target
    assert status.comp_running is None


def test_atw_profile_applied_to_status_object():
    """The ATW adapter decode_status re-decodes the frame via the ATW layout."""
    std = decode_its_status(ATW_FRAMES["state1"])
    # The STANDARD decode misreads this frame.
    assert std.error_code == 128
    assert std.set_temperature == 3

    atw = get_adapter(ModelProfile.ATW).decode_status(std)
    # Mode is Off because Italtherm state1 has zone1_power=False (body[4]=3 Heat).
    assert atw.mode_name == "Off"
    assert atw.set_temperature == 50
    assert atw.t5s_def == 21.0
    assert atw.box_bottom_temp == 46.0
    assert atw.error_code == 0
    assert atw.comp_running is None


def test_atw_tank_temp_routed_to_th_temp_not_twin_temp():
    """For ATW the DHW tank reading (byte[22]) surfaces on ``th_temp``.

    ``th_temp`` backs the "DHW Tank Temperature" sensor. ``twin_temp`` (the
    "Water Inlet Temperature" sensor) must be left honest — these units have no
    real inlet reading — so the override must NOT populate it.
    """
    sensors = decode_its_sensors(bytes([0x02]) + bytes(48))
    status = decode_atw_status(ATW_FRAMES["state1"])
    out = get_adapter(ModelProfile.ATW).decode_sensors(sensors, status)
    assert out.th_temp == 46.0
    # Water Inlet stays untouched (None/0); never the tank value.
    assert out.twin_temp != 46.0
    assert out.twin_temp == sensors.twin_temp


def test_atw_placeholder_sensors_are_unavailable():
    """A documented ATW template is not live sensor telemetry (issue #38)."""
    sensors = decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY)
    status = decode_atw_status(ATW_FRAMES["state1"])

    out = get_adapter(ModelProfile.ATW).decode_sensors(sensors, status)

    assert out.th_temp == 46.0  # Proven status-derived DHW tank temperature.
    assert out.t4_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.odu_current is None
    assert out.odu_voltage is None


@pytest.mark.parametrize("name", list(ATW_TEMPLATE_SENSOR_VARIANTS))
def test_atw_template_sensor_variants_are_unavailable(name):
    """Repeated ``03,<c>`` group templates are not telemetry (#123, #124, #132)."""
    sensors = decode_its_sensors(ATW_TEMPLATE_SENSOR_VARIANTS[name])
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["unit2"])

    out = get_adapter(ModelProfile.ATW).decode_sensors(sensors, status)

    assert out.th_temp == 58.0
    for field in ("t4_temp", "t2_temp", "twin_temp", "twout_temp", "t1_temp"):
        assert getattr(out, field) is None
    assert out.odu_current is None
    assert out.odu_voltage is None


@pytest.mark.parametrize(
    "body",
    [
        # Group constants differ.
        _bytes(
            "02,00,03,1e,00,00,00,00,03,28,00,00,00,00,03,1e,00,00,00,00,"
            "03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00"
        ),
        # Idle slot tag (0x05) is not an allowed idle mode tag (0x02/0x03).
        _bytes(
            "02,00,03,1e,00,00,00,00,05,1e,00,00,00,00,03,1e,00,00,00,00,"
            "03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00"
        ),
        # More than two distinct idle headers even with an active slot.
        _bytes(
            "02,00,03,19,00,00,00,00,03,28,07,1e,00,00,02,0c,00,00,00,00,"
            "03,20,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00"
        ),
        # Disallowed tag byte (0x04) in a schedule slot.
        _bytes(
            "02,00,04,19,00,00,00,00,03,28,07,1e,00,00,02,0c,00,00,00,00,"
            "02,0c,00,00,00,00,02,0c,00,00,00,00,02,0c,00,00,00,00"
        ),
        # Fewer than six groups.
        _bytes("02,00,03,1e,00,00,00,00,03,1e,00,00,00,00,03,1e,00,00,00,00"),
        # Trailing bytes that are not a whole group.
        ATW_PLACEHOLDER_SENSORS_BODY + bytes([0x03, 0x1E, 0x00]),
        # Active schedule shape with out-of-range hour (25:00) is not a schedule.
        _bytes(
            "02,0a,03,28,19,00,0c,00,05,36,16,00,06,00,03,28,0c,00,10,00,"
            "05,34,11,00,14,00,03,28,16,00,05,00,03,28,00,00,00,00"
        ),
        # Slot mask byte above the 6-slot range (0x40).
        _bytes(
            "02,40,03,28,05,00,0c,00,05,36,16,00,06,00,03,28,0c,00,10,00,"
            "05,34,11,00,14,00,03,28,16,00,05,00,03,28,00,00,00,00"
        ),
    ],
)
def test_atw_sensor_frames_outside_template_shape_are_preserved(body):
    """Only frames made entirely of the repeated group are treated as templates."""
    assert atw_sensors_are_placeholder(body) is False


def test_atw_non_placeholder_sensors_are_preserved():
    """Only the confirmed non-telemetry template is suppressed (issue #38)."""
    sensors = decode_its_sensors(bytes([0x02]) + bytes([0x23] * 48))
    status = decode_atw_status(ATW_FRAMES["state1"])

    out = get_adapter(ModelProfile.ATW).decode_sensors(sensors, status)

    assert out.t4_temp == 0.0
    assert out.twin_temp == 0.0
    assert out.odu_voltage == 35
    assert out.th_temp == 46.0


def test_atw_placeholder_keeps_raw_evidence_but_suppresses_all_sensor_entities():
    """Diagnostics retain the frame while every template-derived entity is honest."""
    sensors = decode_its_sensors(ATW_PLACEHOLDER_SENSORS_BODY)
    out = get_adapter(ModelProfile.ATW).decode_sensors(
        sensors,
        decode_atw_status(ATW_FRAMES["state1"]),
    )

    assert out.raw_body == ATW_PLACEHOLDER_SENSORS_BODY
    assert out.tf_temp is None
    assert out.tp_temp is None
    assert out.t3_temp is None
    assert out.t2_temp is None
    assert out.t2b_temp is None
    assert out.t1_temp is None


@pytest.mark.parametrize("name", list(ATW_FRAMES))
def test_atw_dhw_tank_sensor_value_fn_returns_tank_temp(name):
    """The ``dhw_tank`` sensor ``value_fn`` (th_temp) returns the tank temp."""
    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS

    _, _, tank = ATW_EXPECTED[name]
    sensors = decode_its_sensors(bytes([0x02]) + bytes(48))
    status = decode_atw_status(ATW_FRAMES[name])
    out = get_adapter(ModelProfile.ATW).decode_sensors(sensors, status)

    state = make_state(status, out, sn8=ATW_SN8)
    dhw = next(d for d in SENSOR_DESCRIPTIONS if d.key == "dhw_tank")
    assert dhw.value_fn(state) == tank
    # The Water Inlet sensor must not carry the tank value.
    water_inlet = next(d for d in SENSOR_DESCRIPTIONS if d.key == "water_inlet")
    assert water_inlet.value_fn(state) != tank


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


def test_aquapura_tank_temp_from_box_bottom_temp_on_th_temp():
    """AQUAPURA tank temp must source box_bottom_temp and surface on th_temp.

    The "DHW Tank Temperature" sensor reads ``th_temp``; ``twin_temp`` (Water
    Inlet) must stay honest — this model has no real inlet reading.
    """
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())

    # Baseline: twin_temp decodes to 0 while box_bottom_temp holds 40 °C.
    assert sensors.twin_temp == 0.0
    assert status.box_bottom_temp == 40.0

    out = get_adapter(ModelProfile.AQUAPURA).decode_sensors(sensors, status)
    assert out.th_temp == 40.0
    # Water Inlet left honest (not populated with the tank value).
    assert out.twin_temp == sensors.twin_temp
    assert out.twin_temp != 40.0


def test_aquapura_ambient_unchanged():
    """The AQUAPURA override must not disturb the (already-correct) ambient temp."""
    status = decode_its_status(_aquapura_status_body())
    sensors = decode_its_sensors(_aquapura_sensors_body())
    out = get_adapter(ModelProfile.AQUAPURA).decode_sensors(sensors, status)
    assert out.t4_temp == 19.0


# ---------------------------------------------------------------------------
# KJRH-120L decode (sn8 17100003, issues #21 / #5)
# ---------------------------------------------------------------------------
#
# Real STATUS (0x01) frame — device Off, app shows DHW setpoint 60 °C. The
# STANDARD decoder misreads this 95-byte frame and emits garbage:
#   set_temperature=66, t5s_def=-35, error_code=1 (fake fault),
#   comp_running=True (it's Off), total_kwh=1340, comp_frq=6425, sensors -35.
# decode_kjrh120l_status surfaces only the CONFIRMED fields:
#   - power/mode from body[10] (0x00 → Off; non-zero → On/Heat). body[2] stays
#     0x00 even when the unit is on, so it is NOT the power byte (issue #35).
#   - DHW setpoint from body[15] (direct °C; 0x3c → 60), surfaced via t5s_def so
#     the climate target_temperature reads it.
#   - error_code = 0, comp_running = False; everything else left at defaults.
#
# This is the OFF capture: body[10] = 0x00, setpoint body[15] = 0x3c (60 °C).
KJRH120L_STATUS_BODY = _bytes(
    "01,fe,00,00,00,42,00,56,00,00,00,03,41,1e,30,3c,00,00,00,00,00,00,01,"
    "00,01,00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,13,00,01,01,02,03,01,01,e7,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,17,07,14,0f,20,00,00,00,"
    "00,00,ff"
)

# Real ON capture — body[10] = 0x01 (the only state byte that flips vs the OFF
# frame besides the setpoint and timestamp tail), setpoint body[15] = 0x41 (65).
KJRH120L_STATUS_BODY_ON = _bytes(
    "01,fe,00,00,00,42,00,56,00,00,01,03,41,1e,30,41,00,00,00,00,00,00,01,"
    "00,01,00,00,00,01,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,13,00,01,01,02,03,01,01,e7,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,17,07,14,0f,20,00,00,00,"
    "00,00,ff"
)

# Real SENSORS (0x02) frame — static/cached; carries NO live temps. Water and
# outdoor temps never appear in any byte across states, so they are not
# decodable for this model and must be suppressed (left None / unavailable).
KJRH120L_SENSORS_BODY = _bytes(
    "02,fe,00,46,00,9f,00,5a,00,00,00,00,00,00,00,00,00,00,00,00,00,00,00,"
    "00,00,00,00,00,00,00,00,32,00,00,00,00,00,00,00,00,01,41,13,00,00,00,"
    "32,14,00,ff"
)


def test_kjrh120l_status_surfaces_confirmed_setpoint_only():
    """The KJRH-120L status decode surfaces only confirmed fields (setpoint 60)."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)

    # body[10] = power (0x00 → Off → mode 0).
    assert status.mode == 0
    assert status.mode_name == "Off"
    # body[15] = DHW setpoint, direct °C (0x3c = 60). Surfaced via t5s_def so the
    # climate target_temperature (t5s_def if not None) shows 60.0.
    assert status.t5s_def == 60.0
    assert status.set_temperature == 60
    # No fault (STANDARD's error_code=1 is a misread). No compressor-running
    # signal is confirmed in this frame, so it is unknown, not False.
    assert status.error_code == 0
    assert status.comp_running is None
    # raw_body preserved.
    assert status.raw_body == bytes(KJRH120L_STATUS_BODY)
    # The bogus fields the STANDARD decode produced must NOT be populated.
    assert status.total_kwh is None
    assert status.comp_frq is None
    assert status.box_bottom_temp is None
    assert status.tr_temperature is None
    assert status.ptc_temperature is None
    assert status.exv_drg is None
    assert status.comp_total_run_hours is None
    assert status.pressure_h is None
    assert status.pressure_l is None


def test_kjrh120l_status_setpoint_tracks_body15():
    """body[15] is THE setpoint: 0x41 → 65 °C in an earlier captured state."""
    body = bytearray(KJRH120L_STATUS_BODY)
    body[15] = 0x41
    status = decode_kjrh120l_status(bytes(body))
    assert status.t5s_def == 65.0
    assert status.set_temperature == 65


def test_kjrh120l_dual_variant_splits_zone1_and_dhw_setpoints():
    """Validated dual-unit frame maps body[12] to Zone-1 and body[15] to DHW."""
    body = bytearray(KJRH120L_STATUS_BODY_ON)
    body[8] = 0x01
    body[9] = 0x01
    body[12] = 19
    body[15] = 51

    status = decode_kjrh120l_status(body)

    assert status.t5s_def == 19.0
    assert status.set_temperature == 19
    assert status.kjrh120l.dhw_setpoint == 51.0


def test_kjrh120l_status_power_off_from_body10():
    """OFF capture: body[10] == 0x00 → mode 0 / "Off" (climate hvac OFF)."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    assert status.mode == 0
    assert status.mode_name == "Off"


def test_kjrh120l_status_power_on_from_body10():
    """ON capture: body[10] == 0x01 → non-off heating mode so HA shows it on.

    mode 1 maps to HVACMode.HEAT in climate.py (_QUERY_MODE_TO_HVAC), which is a
    non-off state — this is what makes the climate card read ON and the Off
    button usable. body[15] = 0x41 → 65 °C setpoint in this capture.
    """
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY_ON)
    assert status.mode == 1
    assert status.mode != 0  # explicitly NOT off
    assert status.mode_name == "Heat"
    assert status.t5s_def == 65.0
    assert status.set_temperature == 65
    # Power-on is not a compressor-running signal; none is confirmed, so unknown.
    assert status.comp_running is None
    assert status.error_code == 0


def test_kjrh120l_status_power_byte_is_body10_not_body2():
    """body[2] stays 0x00 in both captures; only body[10] flips with power."""
    assert KJRH120L_STATUS_BODY[2] == 0x00
    assert KJRH120L_STATUS_BODY_ON[2] == 0x00
    assert KJRH120L_STATUS_BODY[10] == 0x00
    assert KJRH120L_STATUS_BODY_ON[10] == 0x01
    assert decode_kjrh120l_status(KJRH120L_STATUS_BODY).mode == 0
    assert decode_kjrh120l_status(KJRH120L_STATUS_BODY_ON).mode == 1


def test_kjrh120l_status_short_frame_is_safe():
    """A frame too short to carry body[15]/body[10] decodes safe, no crash."""
    status = decode_kjrh120l_status(bytes([0x01, 0x00]))
    assert status.error_code == 0
    assert status.t5s_def is None
    # Defaults to Off when the power byte is absent.
    assert status.mode == 0


def test_kjrh120l_temp_range_is_20_to_70():
    """Setpoint clamp widened to the app-allowed 20–70 °C (issue #35)."""
    assert KJRH120L_TEMP_MIN == 20
    assert KJRH120L_TEMP_MAX == 70


def test_kjrh120l_profile_applied_to_status_object():
    """The KJRH-120L adapter decode_status re-decodes the frame via the KJRH layout."""
    std = decode_its_status(KJRH120L_STATUS_BODY)
    # Confirm the STANDARD decode misreads this frame (the garbage being fixed).
    assert std.set_temperature == 66
    assert std.t5s_def == -35.0
    assert std.error_code == 1
    assert std.comp_running is True
    assert std.total_kwh == 1340
    assert std.comp_frq == 6425

    kjrh = get_adapter(ModelProfile.KJRH120L).decode_status(std)
    assert kjrh.mode_name == "Off"
    assert kjrh.t5s_def == 60.0
    assert kjrh.set_temperature == 60
    assert kjrh.error_code == 0
    assert kjrh.comp_running is None
    assert kjrh.total_kwh is None
    assert kjrh.comp_frq is None
    assert kjrh.box_bottom_temp is None


def test_kjrh120l_climate_target_temperature_shows_setpoint():
    """climate target_temperature (t5s_def if not None) shows the DHW setpoint."""
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    target = status.t5s_def if status.t5s_def is not None else float(status.set_temperature)
    assert target == 60.0


def test_kjrh120l_sensors_temps_suppressed_except_confirmed_outdoor():
    """Unmapped sensors-frame temps stay None while body[57] surfaces t4_temp.

    The 0x02 sensors frame is static/cached, so its fields are suppressed; the
    confirmed outdoor ambient temperature from status body[57] (raw - 196;
    #148/#142/#136/#119) is routed into ``t4_temp``.
    """
    sensors = decode_its_sensors(KJRH120L_SENSORS_BODY)
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY)
    out = get_adapter(ModelProfile.KJRH120L).decode_sensors(sensors, status)

    assert out.t3_temp is None
    # KJRH120L_STATUS_BODY[57] = 0xe7 (231) → 231 - 196 = 35.0 °C
    assert out.t4_temp == 35.0
    assert out.t2_temp is None
    assert out.t2b_temp is None
    assert out.twin_temp is None
    assert out.twout_temp is None
    assert out.t1_temp is None
    # raw_body preserved.
    assert out.raw_body == sensors.raw_body


@pytest.mark.parametrize(
    ("raw_t4", "expected"),
    [
        (0xD0, 12.0),  # #148 Snapshot A (fullQuery t4=12)
        (0xCC, 8.0),   # #148 Snapshot B (fullQuery t4=8)
        (0xCE, 10.0),  # #142 Inventor MX290-10S & #136 Mundoclima (app 10 °C)
        (0xCD, 9.0),   # #119 Airwell Wellea 07:12
        (0xD9, 21.0),  # #119 Airwell Wellea 10:25
        (0xBF, -5.0),  # sub-zero outdoor temp (-5 °C)
        (0xFF, None),  # disconnected/fill sentinel
        (0x00, None),  # zero-fill outside plausible range
    ],
)
def test_kjrh120l_outdoor_temp_from_status_body57(raw_t4, expected):
    body = bytearray(KJRH120L_STATUS_BODY)
    body[57] = raw_t4
    status = decode_kjrh120l_status(bytes(body))
    sensors = get_adapter(ModelProfile.KJRH120L).decode_sensors(
        decode_its_sensors(KJRH120L_SENSORS_BODY),
        status,
    )

    assert status.kjrh120l.outdoor_temp == expected
    assert sensors.t4_temp == expected


def test_kjrh120l_short_status_leaves_outdoor_temp_none():
    status = decode_kjrh120l_status(KJRH120L_STATUS_BODY[:57])
    sensors = get_adapter(ModelProfile.KJRH120L).decode_sensors(
        decode_its_sensors(KJRH120L_SENSORS_BODY),
        status,
    )

    assert kjrh120l_status(status).outdoor_temp is None
    assert sensors.t4_temp is None


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
    out = get_adapter(ModelProfile.STANDARD).decode_status(legacy)
    assert out is legacy  # no copy, no mutation for the default path


@pytest.mark.parametrize("sn8", [None, "", "UNKNOWN8", "00000000"])
def test_standard_profile_sensors_unchanged_for_unknown_sn8(sn8):
    """An unknown/None sn8 resolves to STANDARD and does not touch the sensors."""
    profile = resolve_profile(sn8)
    status = decode_its_status(ISSUE_11_STATUS_BODY)
    sensors = decode_its_sensors(ISSUE_11_SENSORS_BODY)
    out = get_adapter(profile).decode_sensors(sensors, status)
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


# ---------------------------------------------------------------------------
# KJRH-120L Zone 1 setpoint, run mode and Zone 1 power (app capture 2026-11-12
# and 2026-11-14)
# ---------------------------------------------------------------------------

from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B  # noqa: E402


def _with(body: bytes, **index_values: int) -> bytes:
    out = bytearray(body)
    for key, value in index_values.items():
        out[int(key.lstrip("b"))] = value
    return bytes(out)


def test_kjrh120l_zone1_builders_match_app_capture():
    from custom_components.iletcomfort.protocols.kjrh120l import (
        build_kjrh120l_run_mode,
        build_kjrh120l_zone1_power,
        build_kjrh120l_zone1_setpoint,
        KJRH120L_RUN_MODE_COOL,
        KJRH120L_RUN_MODE_HEAT,
    )

    assert build_kjrh120l_zone1_setpoint(51) == "00040133ff"
    assert build_kjrh120l_zone1_setpoint(63) == "0004013fff"
    assert build_kjrh120l_zone1_setpoint(50) == "00040132ff"
    assert build_kjrh120l_run_mode(KJRH120L_RUN_MODE_COOL, 50) == "00030102ff00040132ff"
    assert build_kjrh120l_run_mode(KJRH120L_RUN_MODE_HEAT, 50) == "00030103ff00040132ff"
    assert build_kjrh120l_zone1_power(True) == "00000101ff"
    assert build_kjrh120l_zone1_power(False) == "00000100ff"


@pytest.mark.parametrize("value", [-1, 256, 1000, 50.5, True, None, "50"])
def test_kjrh120l_zone1_setpoint_builder_rejects_values_outside_one_byte(value):
    from custom_components.iletcomfort.protocols.kjrh120l import build_kjrh120l_zone1_setpoint

    with pytest.raises(ValueError):
        build_kjrh120l_zone1_setpoint(value)


@pytest.mark.parametrize(
    ("run_mode", "setpoint"),
    [(1, 50), (0, 50), (4, 50), (256, 50), (2, 256), (3, -1), (3, None)],
)
def test_kjrh120l_run_mode_builder_rejects_uncaptured_modes_and_bad_setpoints(
    run_mode, setpoint
):
    from custom_components.iletcomfort.protocols.kjrh120l import build_kjrh120l_run_mode

    with pytest.raises(ValueError):
        build_kjrh120l_run_mode(run_mode, setpoint)


@pytest.mark.parametrize("value", [None, 1, 0, "on"])
def test_kjrh120l_zone1_power_builder_requires_bool(value):
    from custom_components.iletcomfort.protocols.kjrh120l import build_kjrh120l_zone1_power

    with pytest.raises(ValueError):
        build_kjrh120l_zone1_power(value)


def test_kjrh120l_decode_snapshot_a_zone1_fields():
    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_zone1_range

    status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A)

    assert status.kjrh120l.zone1_power is True
    assert status.kjrh120l.run_mode == 3
    assert status.kjrh120l.zone1_setpoint == 50.0
    assert status.kjrh120l.zone1_heat_max == 75
    assert status.kjrh120l.zone1_heat_min == 35
    assert status.kjrh120l.zone1_cool_max == 25
    assert status.kjrh120l.zone1_cool_min == 5
    assert kjrh120l_zone1_range(status) == (35, 75)
    assert kjrh120l_zone1_range(status, 2) == (5, 25)
    # Existing fields keep their behaviour: dual gate holds, DHW is body[15].
    assert status.t5s_def == 50.0
    assert status.kjrh120l.dhw_setpoint == 40.0


def test_kjrh120l_decode_snapshot_b_zone_off_still_decodes_zone1_fields():
    from custom_components.iletcomfort.protocols.kjrh120l import (
        kjrh120l_has_zone1,
        kjrh120l_zone1_range,
    )

    status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_B)

    assert not kjrh120l_has_zone1(KJRH120L_SNAPSHOT_B)
    assert status.kjrh120l.zone1_power is False
    assert status.kjrh120l.run_mode == 3
    assert status.kjrh120l.zone1_setpoint == 37.0
    assert kjrh120l_zone1_range(status) == (35, 75)
    # Existing fields unchanged: with the dual gate false, body[15] stays the
    # climate setpoint and the DHW number field stays empty.
    assert status.t5s_def == 35.0
    assert kjrh120l_status(status).dhw_setpoint is None


def test_kjrh120l_decode_cool_mode_uses_cool_range():
    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_zone1_range

    status = decode_kjrh120l_status(_with(KJRH120L_SNAPSHOT_A, b11=2, b12=20))

    assert status.kjrh120l.run_mode == 2
    assert kjrh120l_zone1_range(status) == (5, 25)


def test_kjrh120l_zone1_fields_absent_on_short_frame():
    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_zone1_range

    status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A[:40])

    assert status.kjrh120l.zone1_setpoint == 50.0
    assert kjrh120l_status(status).zone1_cool_min is None
    assert kjrh120l_zone1_range(status) is None
    assert kjrh120l_status(decode_kjrh120l_status(bytes([0x01, 0x00]))).run_mode is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"b11": 1},  # run mode not heat or cool
        {"b11": 0},
        {"b11": 4},
        {"b37": 35, "b38": 35},  # heat min == max
        {"b37": 30, "b38": 35},  # heat min > max
    ],
)
def test_kjrh120l_zone1_range_missing_for_malformed_current_mode(overrides):
    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_zone1_range

    status = decode_kjrh120l_status(_with(KJRH120L_SNAPSHOT_A, **overrides))

    assert kjrh120l_zone1_range(status) is None


@pytest.mark.parametrize(
    ("low", "high"),
    [(float("nan"), 75), (35, float("inf")), (-1, 75), (35, 256), (None, 75)],
)
def test_kjrh120l_zone1_range_rejects_non_finite_and_non_byte_limits(low, high):
    import dataclasses

    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_zone1_range

    decoded = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A)
    status = dataclasses.replace(
        decoded,
        kjrh120l=dataclasses.replace(
            decoded.kjrh120l, zone1_heat_min=low, zone1_heat_max=high
        ),
    )

    assert kjrh120l_zone1_range(status) is None


def test_kjrh120l_mode_change_heat_to_cool_clamps_into_cool_range():
    """Reviewer P1: Heat 50 with cool range 5-25 must send 25, never 50."""
    from custom_components.iletcomfort.protocols.kjrh120l import (
        build_kjrh120l_run_mode,
        kjrh120l_mode_change_setpoint,
        KJRH120L_RUN_MODE_COOL,
    )

    status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A)
    target = kjrh120l_mode_change_setpoint(status, KJRH120L_RUN_MODE_COOL)

    assert target == 25
    assert build_kjrh120l_run_mode(KJRH120L_RUN_MODE_COOL, target) == "00030102ff00040119ff"


def test_kjrh120l_mode_change_cool_to_heat_clamps_up_into_heat_range():
    from custom_components.iletcomfort.protocols.kjrh120l import (
        kjrh120l_mode_change_setpoint,
        KJRH120L_RUN_MODE_HEAT,
    )

    status = decode_kjrh120l_status(_with(KJRH120L_SNAPSHOT_A, b11=2, b12=20))

    assert kjrh120l_mode_change_setpoint(status, KJRH120L_RUN_MODE_HEAT) == 35


def test_kjrh120l_mode_change_keeps_setpoint_inside_destination_range():
    from custom_components.iletcomfort.protocols.kjrh120l import (
        kjrh120l_mode_change_setpoint,
        KJRH120L_RUN_MODE_HEAT,
    )

    status = decode_kjrh120l_status(KJRH120L_SNAPSHOT_A)

    assert kjrh120l_mode_change_setpoint(status, KJRH120L_RUN_MODE_HEAT) == 50


@pytest.mark.parametrize(
    "body",
    [
        _with(KJRH120L_SNAPSHOT_A, b39=5, b40=25),  # cool range inverted
        _with(KJRH120L_SNAPSHOT_A, b39=5, b40=5),  # cool min == max
        KJRH120L_SNAPSHOT_A[:12],  # setpoint byte missing
        KJRH120L_SNAPSHOT_A[:39],  # cool limits missing
    ],
)
def test_kjrh120l_mode_change_refused_without_setpoint_or_destination_range(body):
    from custom_components.iletcomfort.protocols.kjrh120l import (
        kjrh120l_mode_change_setpoint,
        KJRH120L_RUN_MODE_COOL,
    )

    with pytest.raises(ValueError):
        kjrh120l_mode_change_setpoint(
            decode_kjrh120l_status(body), KJRH120L_RUN_MODE_COOL
        )


def test_kjrh120l_mode_change_refuses_uncaptured_destination_mode():
    from custom_components.iletcomfort.protocols.kjrh120l import kjrh120l_mode_change_setpoint

    with pytest.raises(ValueError):
        kjrh120l_mode_change_setpoint(decode_kjrh120l_status(KJRH120L_SNAPSHOT_A), 1)
