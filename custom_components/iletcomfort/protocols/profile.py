"""Model profile enum and sn8-to-profile resolution (#110)."""

from __future__ import annotations

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


def resolve_profile(sn8: str | None) -> ModelProfile:
    """Map an sn8 model code to a decode profile.

    Unknown or missing sn8 → STANDARD (the safe, unchanged default).
    """
    if not sn8:
        return ModelProfile.STANDARD
    return _SN8_PROFILES.get(sn8, ModelProfile.STANDARD)
