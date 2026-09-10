"""Vendor C3 fault codes shown on the heat pump control panel.

Extracted from the vendor's 0xC3 plugin and contributed in issue #48. The
numeric key is the unchanged value decoded into ``ITSStatus.error_code``.
"""

from __future__ import annotations

ERROR_CODES: dict[int, dict[str, str]] = {
    31: {
        "panel": "bA",
        "description": "Outdoor temperature exceeds the allowable operating range.",
    },
    48: {"panel": "C7", "description": "Heat sink over-temperature protection"},
    50: {"panel": "C9", "description": "Abnormal operating frequency protection"},
    61: {
        "panel": "E0",
        "description": "Water flow fault (after 3 consecutive flow faults)",
    },
    62: {
        "panel": "E1",
        "description": "Phase loss, or live and neutral swapped (three-phase models only)",
    },
    63: {
        "panel": "E2",
        "description": "Communication fault between wired controller and hydraulic module",
    },
    64: {"panel": "E3", "description": "T1 leaving water temperature sensor fault"},
    65: {"panel": "E4", "description": "T5 tank temperature sensor fault"},
    66: {"panel": "E5", "description": "T3 sensor fault"},
    67: {"panel": "E6", "description": "T4 sensor fault"},
    68: {"panel": "E7", "description": "Tbtu sensor fault"},
    69: {
        "panel": "E8",
        "description": "Water flow fault (shown as E8 for the first three occurrences, self-recovers after 5 min)",
    },
    70: {"panel": "E9", "description": "Th sensor fault"},
    71: {"panel": "EA", "description": "Tp sensor fault"},
    72: {
        "panel": "Eb",
        "description": "Hydraulic module solar panel temperature sensor fault",
    },
    73: {"panel": "EC", "description": "Tbtl fault"},
    74: {
        "panel": "Ed",
        "description": "Twin plate heat exchanger inlet water temperature sensor fault",
    },
    75: {"panel": "EE", "description": "Hydraulic module EEPROM fault"},
    79: {"panel": "EP", "description": "Tank heater leakage fault"},
    82: {"panel": "F1", "description": "DC bus under-voltage protection"},
    87: {
        "panel": "F6",
        "description": "Electronic expansion valve (EXV) not connected",
    },
    101: {"panel": "H0", "description": "Hydraulic module communication fault"},
    102: {
        "panel": "H1",
        "description": "Compressor drive module communication fault",
    },
    103: {
        "panel": "H2",
        "description": "T2 refrigerant gas side temperature sensor fault",
    },
    104: {
        "panel": "H3",
        "description": "T2B refrigerant liquid side temperature sensor fault",
    },
    105: {
        "panel": "H4",
        "description": "Three L faults (L0/L1) within one hour; not self-recoverable",
    },
    106: {"panel": "H5", "description": "Ta temperature sensor fault"},
    107: {"panel": "H6", "description": "Fan stall fault"},
    108: {"panel": "H7", "description": "Input voltage protection fault"},
    109: {"panel": "H8", "description": "Pressure sensor fault"},
    110: {"panel": "H9", "description": "Tw2 sensor fault"},
    111: {
        "panel": "HA",
        "description": "Twout plate heat exchanger outlet water temperature sensor fault",
    },
    112: {
        "panel": "Hb",
        "description": "Three consecutive PP protections with Twout below 7 C; clears on power cycle",
    },
    113: {"panel": "HC", "description": "Hydraulic module current fault"},
    114: {
        "panel": "Hd",
        "description": "Master to slave unit communication error",
    },
    115: {"panel": "HE", "description": "Fan in zone A for 5 minutes, protection"},
    116: {
        "panel": "HF",
        "description": "Outdoor unit E-party fault or E-party data error",
    },
    117: {"panel": "HH", "description": "Ten E6 events within 2 hours"},
    118: {"panel": "HL", "description": "PFC module fault"},
    119: {
        "panel": "HP",
        "description": "Low pressure protection in cooling mode",
    },
    121: {"panel": "L0", "description": "DC compressor module fault"},
    122: {"panel": "L1", "description": "DC bus low voltage protection"},
    123: {"panel": "L2", "description": "DC bus high voltage protection"},
    125: {
        "panel": "L4",
        "description": "MCE fault, synchronisation or closed loop",
    },
    126: {"panel": "L5", "description": "Zero speed protection"},
    128: {"panel": "L7", "description": "Phase sequence error protection"},
    129: {
        "panel": "L8",
        "description": "Speed change greater than 15 Hz between consecutive moments, protection",
    },
    130: {
        "panel": "L9",
        "description": "Difference between set and actual speed greater than 15 Hz, protection",
    },
    181: {"panel": "P0", "description": "Low pressure sensor protection"},
    182: {"panel": "P1", "description": "High pressure sensor protection"},
    184: {"panel": "P3", "description": "Compressor over-current protection"},
    185: {
        "panel": "P4",
        "description": "Tp discharge temperature too high, protection",
    },
    186: {
        "panel": "P5",
        "description": "Twin to Twout difference too large, protection",
    },
    187: {"panel": "P6", "description": "Compressor drive module fault"},
    190: {"panel": "P9", "description": "DC fan protection"},
    192: {
        "panel": "Pb",
        "description": "Anti-freeze (not a protection, alarm lamp does not flash; wired controller shows an anti-freeze icon rather than Pb)",
    },
    194: {
        "panel": "Pd",
        "description": "Outdoor unit T3 over-temperature protection",
    },
    199: {
        "panel": "PP",
        "description": "Abnormal inlet to outlet water temperature difference protection",
    },
}
