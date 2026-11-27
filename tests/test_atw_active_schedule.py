"""ATW 0x02 active-schedule template detection, 38- and 75-byte forms (#120).

A NØRDIS unit (#120) answers the 0x02 sensors query with a 75-byte frame:
``02``, a 6-bit enable mask, six ``[03|05, temp, sh, sm, eh, em]`` schedule
slots, a ``00`` separator and six idle ``03,<c>,00,00,00,00`` slots. It is
timer data, not telemetry, so it must be suppressed and must trigger the
0x10 / 0x0A probes like the 38-byte form (#128, #150).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.iletcomfort.api import decode_its_sensors, decode_its_status
from custom_components.iletcomfort.protocols import get_adapter
from custom_components.iletcomfort.protocols.atw import atw_sensors_are_placeholder
from custom_components.iletcomfort.protocols.profile import ATW_SN8

CORPUS_DIR = Path(__file__).parent / "corpus"


def _bytes(csv: str) -> bytes:
    return bytes(int(part, 16) for part in csv.split(","))


_IDLE_1E = "03,1e,00,00,00,00"

# Exact 0x02 body from the #120 reporter's diagnostics (NØRDIS, sn8 171H120F).
NORDIS_75_BYTE_SCHEDULE = _bytes(
    "02,05,"
    "05,2d,06,00,07,1e,03,1e,07,00,00,00,05,2d,14,00,16,00,"
    "03,1c,00,0a,06,32,03,1e,00,00,00,00,03,1e,00,00,00,00,"
    "00," + ",".join([_IDLE_1E] * 6)
)
NORDIS_STATUS = _bytes(
    "01,0d,17,a1,03,03,21,1e,28,30,37,19,19,05,37,19,19,05,3c,22,3c,14,27,00,60,05,00"
)


def _with(body: bytes, index: int, value: int) -> bytes:
    out = bytearray(body)
    out[index] = value
    return bytes(out)


def test_issue120_frame_shape() -> None:
    assert len(NORDIS_75_BYTE_SCHEDULE) == 75
    assert NORDIS_75_BYTE_SCHEDULE[38] == 0x00


def test_issue120_75_byte_active_schedule_is_placeholder() -> None:
    assert atw_sensors_are_placeholder(NORDIS_75_BYTE_SCHEDULE) is True
    assert get_adapter(ATW_SN8).sensors_placeholder(NORDIS_75_BYTE_SCHEDULE) is True


def test_issue120_template_sensors_are_unavailable() -> None:
    adapter = get_adapter(ATW_SN8)
    status = adapter.decode_status(decode_its_status(bytearray(NORDIS_STATUS)))
    out = adapter.decode_sensors(decode_its_sensors(NORDIS_75_BYTE_SCHEDULE), status)

    # Previously -35 / -32 / 15 / -25 / -7 / 0 from the schedule bytes.
    for field in (
        "t4_temp", "twout_temp", "twin_temp", "t2_temp", "t3_temp", "t2b_temp",
        "t1_temp", "odu_current", "odu_voltage",
    ):
        assert getattr(out, field) is None, field
    assert out.th_temp == 39.0  # Status-derived DHW tank temperature.
    assert adapter.climate_current_temperature(out) is None


def test_issue120_frame_requests_0x10_and_0x0a_probes() -> None:
    adapter = get_adapter(ATW_SN8)
    status = adapter.decode_status(decode_its_status(bytearray(NORDIS_STATUS)))
    probes = adapter.optional_probes(
        status, {0x01: NORDIS_STATUS, 0x02: NORDIS_75_BYTE_SCHEDULE}
    )
    assert [p.subtype for p in probes] == [0x10, 0x0A, 0x05, 0x07]


_FIRST_BLOCK_120 = NORDIS_75_BYTE_SCHEDULE[:38]


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # body[38] is the probable second-block enable mask: an active 05 slot
        # in block 2 (slot 1, 08:00-12:00) with a matching mask 0x01.
        (
            "second_block_active_05_slot",
            _FIRST_BLOCK_120
            + _bytes("01,05,2d,08,00,0c,00," + ",".join([_IDLE_1E] * 5)),
        ),
        # Block-2 mask 0x22 with 05 slots 2 and 6.
        (
            "second_block_mask_0x22",
            _FIRST_BLOCK_120
            + _bytes(
                "22," + _IDLE_1E + ",05,30,06,1e,09,00,"
                + ",".join([_IDLE_1E] * 3) + ",05,2d,11,00,15,00"
            ),
        ),
        # Different first-block mask (0x13, as on #125).
        ("first_block_mask_0x13", _with(NORDIS_75_BYTE_SCHEDULE, 1, 0x13)),
        # Two idle headers in block 1 (03,1e and 02,0c) plus a different
        # one in block 2 (03,19): the idle cap is per block.
        (
            "two_idle_headers_per_block",
            _bytes(
                "02,05,05,2d,06,00,07,1e,03,1e,00,00,00,00,02,0c,00,00,00,00,"
                "02,0c,00,00,00,00,03,1e,00,00,00,00,02,0c,00,00,00,00,"
                "00," + ",".join(["03,19,00,00,00,00"] * 6)
            ),
        ),
    ],
)
def test_75_byte_active_schedule_variants_are_placeholders(
    label: str, body: bytes
) -> None:
    assert len(body) == 75, label
    assert atw_sensors_are_placeholder(body) is True, label


@pytest.mark.parametrize(
    ("label", "body"),
    [
        # Out-of-range hour (25) in a first-block active slot.
        ("first_block_invalid_hour", _with(NORDIS_75_BYTE_SCHEDULE, 4, 0x19)),
        # Out-of-range minute (60) in a first-block active slot.
        ("first_block_invalid_minute", _with(NORDIS_75_BYTE_SCHEDULE, 5, 0x3C)),
        # Out-of-range hour (24) in a second-block slot.
        ("second_block_invalid_hour", _with(NORDIS_75_BYTE_SCHEDULE, 41, 0x18)),
        # Second-block mask byte above the 6-slot range.
        ("second_block_mask_0x40", _with(NORDIS_75_BYTE_SCHEDULE, 38, 0x40)),
        ("second_block_mask_0xff", _with(NORDIS_75_BYTE_SCHEDULE, 38, 0xFF)),
        # A third distinct idle header within block 1.
        (
            "third_idle_header_block1",
            _bytes(
                "02,05,05,2d,06,00,07,1e,03,1e,07,00,00,00,05,2d,14,00,16,00,"
                "03,1c,00,00,00,00,03,1e,00,00,00,00,03,20,00,00,00,00,"
                "00," + ",".join([_IDLE_1E] * 6)
            ),
        ),
        # Disallowed slot tag (0x04) in the second block.
        ("second_block_bad_tag", _with(NORDIS_75_BYTE_SCHEDULE, 39, 0x04)),
        # Idle 0x05 slot in the second block (idle slots are 02/03 only).
        ("second_block_idle_05", _with(NORDIS_75_BYTE_SCHEDULE, 45, 0x05)),
        # Slot temperature 0 in the second block.
        ("second_block_zero_temp", _with(NORDIS_75_BYTE_SCHEDULE, 40, 0x00)),
        # Enable mask above the 6-slot range.
        ("mask_out_of_range", _with(NORDIS_75_BYTE_SCHEDULE, 1, 0x40)),
        # A third distinct idle header in the second block.
        (
            "third_idle_header",
            _with(_with(NORDIS_75_BYTE_SCHEDULE, 40, 0x20), 46, 0x19),
        ),
        # Truncated / extended by one byte.
        ("truncated", NORDIS_75_BYTE_SCHEDULE[:-1]),
        ("extended", NORDIS_75_BYTE_SCHEDULE + b"\x00"),
        # Two-block form without the separator (74 bytes).
        (
            "no_separator",
            NORDIS_75_BYTE_SCHEDULE[:38] + NORDIS_75_BYTE_SCHEDULE[39:],
        ),
        # All slots idle with a non-zero mask: no active window at all.
        (
            "no_active_slot",
            _bytes("02,05," + ",".join([_IDLE_1E] * 6) + ",00," + ",".join([_IDLE_1E] * 6)),
        ),
    ],
)
def test_75_byte_frames_outside_schedule_shape_are_not_placeholders(
    label: str, body: bytes
) -> None:
    assert atw_sensors_are_placeholder(body) is False, label


def _corpus_0x02_frames() -> list[tuple[str, bytes, bool]]:
    frames = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        for case in json.loads(path.read_text(encoding="utf-8")):
            raw = case["frames"].get("0x02")
            if raw is not None:
                expected = case.get("expected_atw_0x02_placeholder") is True
                frames.append((case["id"], _bytes(raw), expected))
    return frames


@pytest.mark.parametrize(
    ("case_id", "body", "expected"),
    _corpus_0x02_frames(),
    ids=[case_id for case_id, _, _ in _corpus_0x02_frames()],
)
def test_corpus_0x02_placeholder_classification(
    case_id: str, body: bytes, expected: bool
) -> None:
    """Real telemetry 0x02 frames in the corpus never match a template shape."""
    assert atw_sensors_are_placeholder(body) is expected, case_id
