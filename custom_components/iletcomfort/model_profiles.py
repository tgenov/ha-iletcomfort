"""sn8-gated model decode profiles.

Different C3 heat-pump *models* pack their status/sensor frames differently, but
the Dollin cloud exposes no device-class field: both an air-to-water (ATW) unit
and an air-to-air unit report ``applianceType="0xC3"`` and ``modelNumber="0"``.
The only differentiator is ``sn8`` — an 8-char model-code serial prefix carried
in the appliance metadata (cached on the coordinator as
``appliance_meta["sn8"]``).

Model-specific decoding is therefore gated on an ``sn8 -> profile`` lookup table.
Any UNKNOWN or missing sn8 falls back to the STANDARD decode unchanged, so
existing/working models cannot be corrupted by a new profile.

Profiles
--------
STANDARD
    Default. Byte-for-byte identical to ``decode_its_status`` /
    ``decode_its_sensors``. ``apply_profile_to_*`` return the input untouched.

ATW (sn8 ``171H120F``, Italtherm air-to-water, issue #22)
    A 25-byte status frame with a different field layout. Decoded by
    ``decode_atw_status``; the values are surfaced through the *existing*
    ITSStatus/ITSSensors fields so no entity wiring changes are needed (see the
    field map below).

AQUAPURA (sn8 ``171000AU``, AQS Energie AQUAPURA split HPWH, issue #12)
    Standard status/sensor decode, except the tank temperature must read
    ``status.box_bottom_temp`` (status byte[17], offset-decoded) and is surfaced
    on ``th_temp`` (the "DHW Tank Temperature" sensor) instead of
    ``sensors.twin_temp`` (which is null-filled to 0 on this model).

For both ATW and AQUAPURA the tank temperature is routed to ``th_temp`` and the
"Water Inlet Temperature" sensor (``twin_temp``) is left honest (no real inlet
reading). Climate ``current_temperature`` is profile-aware and returns
``th_temp`` for these profiles (``twin_temp`` for STANDARD).
"""

from __future__ import annotations

import dataclasses
from enum import Enum

from .api import ITSSensors, ITSStatus

# sn8 model codes (8-char serial prefixes) → profile.
ATW_SN8 = "171H120F"
AQUAPURA_SN8 = "171000AU"
KJRH120L_SN8 = "17100003"


class ModelProfile(Enum):
    """Decode profile selected from a device's sn8 model code."""

    STANDARD = "standard"
    ATW = "atw"
    AQUAPURA = "aquapura"
    KJRH120L = "kjrh120l"


_SN8_PROFILES: dict[str, ModelProfile] = {
    ATW_SN8: ModelProfile.ATW,
    AQUAPURA_SN8: ModelProfile.AQUAPURA,
    KJRH120L_SN8: ModelProfile.KJRH120L,
}


def resolve_profile(sn8: str | None) -> ModelProfile:
    """Map an sn8 model code to a decode profile.

    Unknown or missing sn8 → STANDARD (the safe, unchanged default).
    """
    if not sn8:
        return ModelProfile.STANDARD
    return _SN8_PROFILES.get(sn8, ModelProfile.STANDARD)


def build_query_command(profile: ModelProfile, subtype: int) -> str:
    """Return the cloud query command for a status/sensors subtype.

    KJRH-120L's cloud rejects the standard long C3 query frame (1214); its
    app uses a short ``ffff<ss><ss><ss>ff`` form. STANDARD/other profiles use
    the normal build_c3_query frame, unchanged.
    """
    if profile is ModelProfile.KJRH120L:
        return "ffff%02x%02x%02xff" % (subtype, subtype, subtype)
    # Lazy import: api.py imports model_profiles, so importing build_c3_query at
    # module level would risk a circular import.
    from .api import build_c3_query

    return build_c3_query(subtype)


# ---------------------------------------------------------------------------
# ATW profile (issue #22) — validated against five real frames from yoavaviram
# ---------------------------------------------------------------------------
#
# status raw_body, 0-indexed ([0] = 0x01 subtype byte):
#   byte[1]  flags: bit0 (0x01) = space-heat demand; the 0x04 bit tracks DHW
#            activity. Treated as FLAGS, not a scalar/mode.
#   byte[8]  DHW setpoint in °C — DIRECT value (not +35-offset encoded).
#   byte[9]  Zone-1 setpoint × 2 (0.5° resolution) → zone1 = byte[9] / 2.
#   byte[22] DHW tank current temp in °C — DIRECT value.
#   byte[24] constant 0x80 across all captured states → a flags/MSB byte,
#            NOT an error code. The app shows no fault, so error_code = 0.
#
# The STANDARD decoder misreads this 25-byte frame (set_temperature=3,
# error_code=128, spurious comp_running, DHW tank=0, mode=Unknown(7), …);
# decode_atw_status fixes those by surfacing the confirmed values through the
# existing ITSStatus fields the entities already read:
#   - set_temperature  ← byte[8]   (no DHW-setpoint entity today; kept for SET
#                                    echo / future use)
#   - t5s_def          ← byte[9]/2 (climate target_temperature reads t5s_def)
#   - box_bottom_temp  ← byte[22]  (routed to th_temp / "DHW Tank Temperature";
#                                    see apply_profile_to_sensors. Climate
#                                    current_temperature is profile-aware and
#                                    reads th_temp for ATW.)
#
# CONSERVATIVE / ASSUMED (await hardware validation by the reporter):
#   - comp_running is forced False: none of the five frames carries a confirmed
#     compressor-running signal, and there is no comp_frq field in a 25-byte
#     frame, so we do not derive "running" from byte[14] (which the STANDARD
#     path misread as a comp flag).
#   - HVAC mode/action semantics are left at the dataclass defaults (mode 0 /
#     "Off"); the byte[1] flags are recorded raw in status_flags_raw with only
#     the space-heat-demand bit (0x01) interpreted. We do not invent an HVAC
#     mode/action mapping until validated.

ATW_DHW_SETPOINT_INDEX = 8
ATW_ZONE1_SETPOINT_X2_INDEX = 9
ATW_DHW_TANK_TEMP_INDEX = 22
ATW_FLAGS_INDEX = 1
ATW_SPACE_HEAT_DEMAND_BIT = 0x01


def decode_atw_status(body: bytearray | bytes) -> ITSStatus:
    """Decode an ATW (sn8 171H120F) status frame into an ITSStatus.

    Routes the confirmed ATW byte values into the existing ITSStatus fields the
    entities consume. See the module-level field map for the full layout and the
    conservative choices made for unvalidated fields.
    """
    status = ITSStatus()
    status.raw_body = bytes(body)
    body_len = len(body)

    # Defensive: a frame too short to carry the confirmed fields decodes to an
    # all-defaults ITSStatus (the caller's truncated-frame guard handles the
    # query path; this keeps direct decode calls safe).
    if body_len <= ATW_DHW_TANK_TEMP_INDEX:
        return status

    flags = body[ATW_FLAGS_INDEX]
    status.status_flags_raw = flags
    # Only the space-heat-demand bit is interpreted; see module notes.
    status.pump_system = bool(flags & ATW_SPACE_HEAT_DEMAND_BIT)

    # DHW setpoint — direct °C value.
    status.set_temperature = body[ATW_DHW_SETPOINT_INDEX]
    # Zone-1 / climate target setpoint — 0.5° resolution. Surfaced via t5s_def
    # so the climate entity's target_temperature reads it without changes.
    status.t5s_def = body[ATW_ZONE1_SETPOINT_X2_INDEX] / 2
    # DHW tank current temp — direct °C value. Surfaced via box_bottom_temp,
    # which apply_profile_to_sensors routes to th_temp ("DHW Tank Temperature").
    status.box_bottom_temp = float(body[ATW_DHW_TANK_TEMP_INDEX])

    # byte[24] is a flags/MSB byte, not a fault → no error. Conservative: no
    # confirmed running signal in these frames.
    status.error_code = 0
    status.comp_running = False

    return status


# ---------------------------------------------------------------------------
# KJRH-120L profile (issues #21 / #5) — clean decode from real diagnostics
# ---------------------------------------------------------------------------
#
# The KJRH-120L (sn8 17100003) connects via the short ffff query form
# (build_query_command above), but its 95-byte status frame does NOT match the
# STANDARD C3 layout. The STANDARD decoder misreads it and emits garbage — from
# the reporter's real frame (device Off, app DHW setpoint 60 °C):
#   set_temperature=66, t5s_def=-35, error_code=1 (fake fault),
#   comp_running=True (it's Off), total_kwh=1340, comp_frq=6425, every temp -35.
#
# Only two status fields are confirmed across multiple captured states:
#   body[2]  = mode (0x00 = Off; the standard modes map applies).
#   body[15] = DHW setpoint in °C — DIRECT value (0x3c=60 here, 0x41=65 earlier).
# This is THE setpoint. error_code is 0 (no fault) and the unit is Off
# (comp_running False). Everything else in the frame is unmapped garbage for
# this model and is left at the dataclass defaults (None/0).
#
# Water tank temp and outdoor temp are NOT decodable for this model — they do
# not appear in the status frame and the sensors frame is static/cached (three
# different water readings and two outdoor readings never appear in any byte).
# We deliberately do not guess a mapping; apply_profile_to_sensors nulls every
# temperature field so HA shows them unavailable instead of a wrong constant.

KJRH120L_MODE_INDEX = 2
KJRH120L_DHW_SETPOINT_INDEX = 15
_KJRH120L_MODES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", 4: "Water Pump"}

# Temperature fields suppressed for the KJRH-120L (not exposed by its API).
_KJRH120L_SUPPRESSED_TEMPS = {
    "t3_temp": None,
    "t4_temp": None,
    "t2_temp": None,
    "t2b_temp": None,
    "twin_temp": None,
    "twout_temp": None,
    "t1_temp": None,
}


def decode_kjrh120l_status(body: bytearray | bytes) -> ITSStatus:
    """Decode a KJRH-120L (sn8 17100003) status frame into a clean ITSStatus.

    Surfaces only the confirmed fields (mode from body[2], DHW setpoint from
    body[15]) and suppresses the garbage the STANDARD decoder produces. See the
    module-level notes for the full rationale.
    """
    status = ITSStatus()
    status.raw_body = bytes(body)
    body_len = len(body)

    # No fault and not running are confirmed for every captured state.
    status.error_code = 0
    status.comp_running = False

    if body_len > KJRH120L_MODE_INDEX:
        status.mode = body[KJRH120L_MODE_INDEX]
        status.mode_name = _KJRH120L_MODES.get(
            status.mode, f"Unknown({status.mode})"
        )

    # DHW setpoint — direct °C value. Surfaced via t5s_def so the climate
    # entity's target_temperature (t5s_def if not None) shows it; set_temperature
    # is set too for the SET echo / future DHW-setpoint entity.
    if body_len > KJRH120L_DHW_SETPOINT_INDEX:
        setpoint = body[KJRH120L_DHW_SETPOINT_INDEX]
        status.t5s_def = float(setpoint)
        status.set_temperature = setpoint

    return status


def apply_profile_to_status(profile: ModelProfile, status: ITSStatus) -> ITSStatus:
    """Return the profile-canonical ITSStatus for a decoded status object.

    STANDARD and AQUAPURA return ``status`` untouched (AQUAPURA's only override
    is on the sensors side). ATW re-decodes the raw frame via the ATW layout;
    KJRH120L re-decodes it via the KJRH-120L layout (clean setpoint, no garbage).
    """
    if profile is ModelProfile.ATW:
        return decode_atw_status(status.raw_body)
    if profile is ModelProfile.KJRH120L:
        return decode_kjrh120l_status(status.raw_body)
    return status


def apply_profile_to_sensors(
    profile: ModelProfile,
    sensors: ITSSensors,
    status: ITSStatus,
) -> ITSSensors:
    """Return the profile-canonical ITSSensors for the decoded sensors object.

    STANDARD returns ``sensors`` untouched. ATW and AQUAPURA both route a tank
    temperature into ``th_temp`` — the field the "DHW Tank Temperature" sensor
    reads — so that entity shows the meaningful value without entity-wiring
    changes. ``twin_temp`` (the "Water Inlet Temperature" sensor) is deliberately
    left untouched: these units expose no real inlet reading, so it stays honest
    (None/0) rather than being mislabeled with the tank temperature.

    Climate ``current_temperature`` is made profile-aware separately (it returns
    ``th_temp`` for ATW/AQUAPURA) so the climate card still shows the tank temp.

    - ATW: ``status.box_bottom_temp`` carries the DHW tank temp (byte[22]).
    - AQUAPURA: ``status.box_bottom_temp`` (status byte[17], offset-decoded) is
      the water tank temperature the app shows; the STANDARD ``twin_temp`` source
      is null-filled to 0 on this model (issue #12).
    - KJRH120L: water/outdoor temps are NOT exposed by this model's API (the
      sensors frame is static/cached), so every temperature field is nulled — HA
      shows them unavailable rather than the misleading constant -35 the STANDARD
      decode would produce (issues #21 / #5).
    """
    if profile is ModelProfile.KJRH120L:
        return dataclasses.replace(sensors, **_KJRH120L_SUPPRESSED_TEMPS)
    if profile in (ModelProfile.ATW, ModelProfile.AQUAPURA):
        if status is not None and status.box_bottom_temp is not None:
            return dataclasses.replace(sensors, th_temp=status.box_bottom_temp)
    return sensors


__all__ = [
    "AQUAPURA_SN8",
    "ATW_SN8",
    "KJRH120L_SN8",
    "ModelProfile",
    "apply_profile_to_sensors",
    "apply_profile_to_status",
    "build_query_command",
    "decode_atw_status",
    "decode_kjrh120l_status",
    "resolve_profile",
]
