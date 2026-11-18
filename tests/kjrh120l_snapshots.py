"""Real KJRH-120L (sn8 17100003) subtype-0x01 status bodies for tests.

Both come from one dual Zone 1 + DHW unit. The vendor fullQuery taken at the
same moment gives the expected values. ``body[0]`` is the subtype byte.
"""

from __future__ import annotations


def _bytes(csv: str) -> bytes:
    return bytes(int(x, 16) for x in csv.split(","))


# Snapshot A: zone1 on, zone2 on, heat, zone1 setpoint 50, DHW 40,
#   zone1 heat max/min 75/35, zone1 cool max/min 25/5.
KJRH120L_SNAPSHOT_A = _bytes(
    "01,fe,00,00,00,41,00,55,01,01,01,03,32,1e,30,28,00,00,00,00,00,00,01,"
    "01,01,00,00,00,00,00,01,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,ee,00,01,01,06,03,01,01,d0,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,1a,0b,0c,0d,35,00,00,00,"
    "00,ff"
)
# Snapshot B: zone1 off, zone2 off, heat, zone1 setpoint 37, DHW 35, limits
# unchanged. body[8:10] reads 00 00 here, so kjrh120l_has_zone1 is False.
KJRH120L_SNAPSHOT_B = _bytes(
    "01,fe,00,00,00,41,00,55,00,00,01,03,25,1e,30,23,00,00,00,00,00,00,01,"
    "01,01,00,00,00,00,00,00,00,00,00,01,02,02,4b,23,19,05,37,19,19,05,3c,"
    "22,46,14,f0,00,01,01,06,03,01,01,cc,2f,ff,ff,ff,ff,ff,ff,ff,ff,ff,ff,"
    "ff,30,ff,ff,ff,ff,00,00,00,01,00,00,00,00,00,1a,0b,12,08,39,00,00,00,"
    "00,ff"
)
