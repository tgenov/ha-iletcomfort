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
for every unit (see #124), so this module only decodes the raw fields; the
profile decoder in ``model_profiles`` decides what entities show.
"""

from __future__ import annotations

from dataclasses import dataclass

BODY_BASIC = 0x01

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


__all__ = [
    "BODY_BASIC",
    "C3BasicStatus",
    "C3_MODE_COOL",
    "C3_MODE_HEAT",
    "decode_c3_basic",
]
