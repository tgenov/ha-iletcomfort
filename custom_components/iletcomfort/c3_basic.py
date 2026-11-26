"""Standard Midea C3 "basic" status layout (sn8 171H120F).

The ATW status frame (query body type ``0x01``) of every ``171H120F`` unit seen
so far — Italtherm (#22), Galmet Prima 8GT (#38) and Rotenso Aquami (#124) —
is laid out like the standard Midea C3 basic body (midea-local
``C3BasicBody``, ``data_offset=1``). Indexes below are into the body
*including* the leading body-type byte::

    [1]  power/flags: 0x01 zone1 power, 0x02 zone2 power, 0x04 DHW power,
         0x08 zone1 curve, 0x10 zone2 curve, 0x20 TBH, 0x40 fast DHW
    [2]  capabilities: 0x01 heat, 0x02 cool, 0x04 DHW, 0x08 double zone,
         0x10 zone1 water-temperature control, 0x20 zone2 water-temp control
    [3]  0x02 silent mode, 0x04 holiday, 0x08 ECO
    [4]  mode (2 cool, 3 heat)        [5] mode under auto
    [6]  zone1 water target °C        [7] zone2 water target °C
    [8]  DHW target °C                [9] room target °C × 2
    [10..13] zone1 heat max/min, cool max/min
    [14..17] zone2 heat max/min, cool max/min
    [18..19] room max/min × 2         [20..21] DHW max/min
    [22] DHW tank actual °C           [23] error code

Which byte holds the setpoint the official app displays is **not** settled
for every unit (see #124), so the decoder only exposes the raw fields; the
profile decoder in ``model_profiles`` decides what entities show.

Writes (hardware-validated on a Rotenso Aquami, EU cloud, 2026-11-17; each
change sent once, verified by re-query and reverted) use the standard Midea
C3 SET with the same AA header as the status query (``c3_frame.build_c3_frame``):

* basic SET — message type ``0x02``, body ``01`` + 7 bytes
  ``[power, mode, zone1, zone2, dhw, room×2, curve flags]``; direct °C, no
  +35 offset. Validated in Heat mode with water-temperature control: DHW
  target, DHW power, Zone-1 water target, Zone-1 curve and Zone-1 power.
  The builders refuse anything outside that envelope (mode changes, Cool,
  room-temperature setpoints).
* silence SET — body ``05`` + 9 bytes, ECO SET — body ``07`` + 6 bytes. Bit 0
  of the first byte is on/off; the rest is the device's timer schedule,
  copied from a fresh query of the same body type so the owner's schedule is
  preserved.

Every basic SET is built from a fresh status, so only the requested field
changes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .c3_frame import build_c3_frame

MSG_SET = 0x02

BODY_BASIC = 0x01
BODY_SILENCE = 0x05
BODY_ECO = 0x07

# Switchable features written with their own SET body type.
FEATURE_SILENT = "silent"
FEATURE_ECO = "eco"
FEATURE_BODY_TYPES: dict[str, int] = {
    FEATURE_SILENT: BODY_SILENCE,
    FEATURE_ECO: BODY_ECO,
}

# Feature SET payload sizes after the body-type byte (midea-local
# MessageSetSilent / MessageSetECO), and how many of those bytes a query
# reply must carry before a SET may be built from it. The validated ECO reply
# is one byte shorter than its SET payload; that trailing byte was sent as
# 0x00 and accepted by the hardware. Everything else is schedule data and
# must come from the device, never from zero-fill.
_FEATURE_SET_LEN: dict[int, int] = {BODY_SILENCE: 9, BODY_ECO: 6}
_FEATURE_QUERY_MIN_LEN: dict[int, int] = {BODY_SILENCE: 9, BODY_ECO: 5}

# Status byte [4] (midea-local ``C3DeviceMode``). Auto (1) is not decoded.
C3_MODE_COOL = 2
C3_MODE_HEAT = 3

BASIC_MIN_LEN = 24  # body type byte + 23 data bytes up to the error code


@dataclass(frozen=True)
class C3BasicStatus:
    """Decoded C3 basic status (body type 0x01)."""

    zone1_power: bool
    zone2_power: bool
    dhw_power: bool
    zone1_curve: bool
    zone2_curve: bool
    tbh: bool
    fast_dhw: bool
    cap_heat: bool
    cap_cool: bool
    cap_dhw: bool
    double_zone: bool
    zone1_water_temp: bool
    silent_mode: bool
    holiday: bool
    eco_mode: bool
    mode: int
    zone1_target: int
    zone2_target: int
    dhw_target: int
    room_target: float
    zone1_heat_max: int
    zone1_heat_min: int
    zone1_cool_max: int
    zone1_cool_min: int
    room_max: float
    room_min: float
    dhw_max: int
    dhw_min: int
    dhw_tank_temp: int
    error_code: int

    @property
    def is_cooling_mode(self) -> bool:
        return self.mode == C3_MODE_COOL

    @property
    def zone1_water_range(self) -> tuple[float, float]:
        """Zone-1 water-target limits for the current operating mode."""
        if self.is_cooling_mode:
            return float(self.zone1_cool_min), float(self.zone1_cool_max)
        return float(self.zone1_heat_min), float(self.zone1_heat_max)


def decode_c3_basic(body: bytes | bytearray) -> C3BasicStatus | None:
    """Decode a C3 basic status body, or None if it is not one."""
    if len(body) < BASIC_MIN_LEN or body[0] != BODY_BASIC:
        return None
    if body[4] not in (C3_MODE_COOL, C3_MODE_HEAT):
        return None
    b1, b2, b3 = body[1], body[2], body[3]
    return C3BasicStatus(
        zone1_power=bool(b1 & 0x01),
        zone2_power=bool(b1 & 0x02),
        dhw_power=bool(b1 & 0x04),
        zone1_curve=bool(b1 & 0x08),
        zone2_curve=bool(b1 & 0x10),
        tbh=bool(b1 & 0x20),
        fast_dhw=bool(b1 & 0x40),
        cap_heat=bool(b2 & 0x01),
        cap_cool=bool(b2 & 0x02),
        cap_dhw=bool(b2 & 0x04),
        double_zone=bool(b2 & 0x08),
        zone1_water_temp=bool(b2 & 0x10),
        silent_mode=bool(b3 & 0x02),
        holiday=bool(b3 & 0x04),
        eco_mode=bool(b3 & 0x08),
        mode=body[4],
        zone1_target=body[6],
        zone2_target=body[7],
        dhw_target=body[8],
        room_target=body[9] / 2,
        zone1_heat_max=body[10],
        zone1_heat_min=body[11],
        zone1_cool_max=body[12],
        zone1_cool_min=body[13],
        room_max=body[18] / 2,
        room_min=body[19] / 2,
        dhw_max=body[20],
        dhw_min=body[21],
        dhw_tank_temp=body[22],
        error_code=body[23],
    )


class UnsupportedC3Write(ValueError):
    """The requested change is outside the hardware-validated write envelope."""


def apply_basic_changes(
    status: C3BasicStatus,
    *,
    zone1_power: bool | None = None,
    dhw_power: bool | None = None,
    zone1_curve: bool | None = None,
    zone1_water_target: float | None = None,
    dhw_target: float | None = None,
) -> C3BasicStatus:
    """Return ``status`` with the requested changes, or raise.

    Only changes validated on hardware are accepted. The operating mode byte
    is always written back unchanged, so a Zone-1 power-on or setpoint change
    is refused unless the unit is already in Heat mode: powering the zone on
    must never start cooling.
    """
    zone1_change = (
        bool(zone1_power)
        or zone1_water_target is not None
        or zone1_curve is not None
    )
    if zone1_change and status.mode != C3_MODE_HEAT:
        raise UnsupportedC3Write(
            "Zone-1 control is only supported in Heat mode; switching the "
            "operating mode has not been validated"
        )
    new_zone1_power = status.zone1_power if zone1_power is None else zone1_power
    new_zone1_curve = status.zone1_curve if zone1_curve is None else zone1_curve
    new_dhw_power = status.dhw_power if dhw_power is None else dhw_power
    new_dhw_target = status.dhw_target
    if dhw_target is not None:
        if not (0 < status.dhw_min < status.dhw_max) or not (
            status.dhw_min <= dhw_target <= status.dhw_max
        ):
            raise ValueError(
                f"DHW target {dhw_target} °C outside "
                f"{status.dhw_min}-{status.dhw_max} °C"
            )
        new_dhw_target = int(round(dhw_target))
    new_zone1_target = status.zone1_target
    if zone1_water_target is not None:
        if not status.zone1_water_temp:
            raise UnsupportedC3Write(
                "Zone-1 setpoint writes are only supported with "
                "water-temperature control"
            )
        low, high = status.zone1_water_range
        if not (0 < low < high) or not (low <= zone1_water_target <= high):
            raise ValueError(
                f"Zone-1 target {zone1_water_target} °C outside {low:g}-{high:g} °C"
            )
        new_zone1_target = int(round(zone1_water_target))
    return replace(
        status,
        zone1_power=new_zone1_power,
        zone1_curve=new_zone1_curve,
        dhw_power=new_dhw_power,
        dhw_target=new_dhw_target,
        zone1_target=new_zone1_target,
    )


def build_basic_set(status: C3BasicStatus) -> str:
    """Build the basic SET frame that writes ``status`` back to the device."""
    power = (
        (0x01 if status.zone1_power else 0)
        | (0x02 if status.zone2_power else 0)
        | (0x04 if status.dhw_power else 0)
    )
    flags = (
        (0x01 if status.zone1_curve else 0)
        | (0x02 if status.zone2_curve else 0)
        | (0x04 if status.tbh else 0)
        | (0x08 if status.fast_dhw else 0)
    )
    body = bytes([
        BODY_BASIC,
        power,
        status.mode,
        status.zone1_target,
        status.zone2_target,
        status.dhw_target,
        int(round(status.room_target * 2)),
        flags,
    ])
    return build_c3_frame(MSG_SET, body)


def build_feature_set(query_body: bytes | bytearray, on: bool) -> str:
    """Build a silence/ECO SET from that feature's query body, flipping bit 0.

    The device's timer bytes are copied from ``query_body`` (unlike
    midea-local, which zeroes them) so the owner's schedules survive. A reply
    too short to carry the whole schedule is rejected rather than zero-filled.
    """
    if not query_body or query_body[0] not in _FEATURE_SET_LEN:
        raise ValueError("not a silence/ECO query body")
    body_type = query_body[0]
    payload = bytearray(query_body[1:])
    if len(payload) < _FEATURE_QUERY_MIN_LEN[body_type]:
        raise ValueError(
            f"incomplete 0x{body_type:02x} reply ({len(payload)} data bytes); "
            "refusing to overwrite the device's schedule"
        )
    size = _FEATURE_SET_LEN[body_type]
    payload = (payload + bytearray(size))[:size]
    payload[0] = (payload[0] & ~0x01) | (0x01 if on else 0)
    return build_c3_frame(MSG_SET, bytes([body_type]) + bytes(payload))


__all__ = [
    "BODY_BASIC",
    "BODY_ECO",
    "BODY_SILENCE",
    "C3BasicStatus",
    "C3_MODE_COOL",
    "C3_MODE_HEAT",
    "FEATURE_BODY_TYPES",
    "FEATURE_ECO",
    "FEATURE_SILENT",
    "UnsupportedC3Write",
    "apply_basic_changes",
    "build_basic_set",
    "build_feature_set",
    "decode_c3_basic",
]
