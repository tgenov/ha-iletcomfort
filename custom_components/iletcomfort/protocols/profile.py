"""Model profile enum and sn8-to-profile resolution (#110)."""

from __future__ import annotations

import re
from enum import Enum

# sn8 model codes (8-char serial prefixes) → profile.
ATW_SN8 = "171H120F"
AQUAPURA_SN8 = "171000AU"
KJRH120L_SN8 = "17100003"
KJRH120L2_SN8 = "17100007"  # Known catalogue entry; no validated profile yet.
KJR86T3_SN8 = "17186T3A"  # KJR-86T3 table controller (AQUAPURA variant, #126).


class ModelProfile(Enum):
    """Decode profile selected from a device's sn8 model code."""

    STANDARD = "standard"
    ATW = "atw"
    AQUAPURA = "aquapura"
    KJRH120L = "kjrh120l"
    KJR86T3 = "kjr86t3"


_SN8_PROFILES: dict[str, ModelProfile] = {
    ATW_SN8: ModelProfile.ATW,
    AQUAPURA_SN8: ModelProfile.AQUAPURA,
    KJRH120L_SN8: ModelProfile.KJRH120L,
    KJR86T3_SN8: ModelProfile.KJR86T3,
}


_SERIAL_MIN_LEN = 18  # sn[9:17] needs more than 17 characters (midea-local rule).
_SERIAL_HEAD = re.compile(r"[0-9A-Z]{4}C3")  # <manufacturer code><device type C3>
_SN8_CODE = re.compile(r"[0-9A-Z]{8}")


def sn8_from_serial(serial: object) -> str | None:
    """Return the sn8 model code inside a C3 unit serial, or None.

    A Midea serial is ``<4 manufacturer><2 device type>…``, and characters
    9..16 (``sn[9:17]``) are the sn8. midea-local uses the same rule when the
    cloud ``sn8`` field is empty, and the serial embedded in a ``171H120F``
    ``0x10`` frame captured on #118 agrees with it. Only a serial that starts
    with an upper-case ``<4 manufacturer chars>C3`` and has an upper-case
    alphanumeric 8-character code is accepted.
    """
    if not isinstance(serial, str):
        return None
    serial = serial.strip()
    # Exact upper-case match: an encrypted serial (lower-case hex in other
    # Midea clouds) must not pass by chance.
    if len(serial) < _SERIAL_MIN_LEN or not _SERIAL_HEAD.fullmatch(serial[:6]):
        return None
    candidate = serial[9:17]
    if not _SN8_CODE.fullmatch(candidate):
        return None
    return candidate


def appliance_sn8(appliance: object) -> tuple[str | None, str]:
    """Return ``(sn8, source)`` for cloud appliance metadata.

    ``source`` is ``"cloud"`` (the ``sn8`` field), ``"serial"`` (derived from
    the ``sn`` field because ``sn8`` is empty), or ``"unknown"``.
    """
    if not isinstance(appliance, dict):
        return None, "unknown"
    field = appliance.get("sn8")
    if field:
        return str(field), "cloud"
    derived = sn8_from_serial(appliance.get("sn"))
    if derived is not None:
        return derived, "serial"
    return None, "unknown"


def resolve_profile(sn8: str | None) -> ModelProfile:
    """Map an sn8 model code to a decode profile.

    Unknown or missing sn8 → STANDARD (the safe, unchanged default).
    """
    if not sn8:
        return ModelProfile.STANDARD
    return _SN8_PROFILES.get(sn8, ModelProfile.STANDARD)
