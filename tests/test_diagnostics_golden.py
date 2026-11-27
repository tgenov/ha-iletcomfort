"""Golden diagnostics output for every hardware corpus capture (#236).

Characterization test: the expected payloads in
``tests/fixtures/diagnostics_golden.json`` were captured from ``main`` before
``ITSStatus`` was split into per-dialect sub-structs. Diagnostics must keep the
exact same keys, key order, layout and values (including the flat
``kjrh120l_*`` / ``atw_*`` status keys and the nested ``c3_basic`` dict), and
redact no less than before.

Each corpus case is rendered for the poll path, the MQTT push path (retained
optional subtypes) and, where a 0x05 frame exists, a push whose 0x01 Silent bit
disagrees with the retained 0x05 frame (the silent fields reconcile to off or
unknown).

Regenerate only for a deliberate diagnostics change:
``ILET_UPDATE_DIAGNOSTICS_GOLDEN=1 uv run pytest tests/test_diagnostics_golden.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import decode_its_sensors, decode_its_status
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_KJRH120L_ZONE1_CAPABLE,
    CONF_REGION,
    DOMAIN,
    REGION_US,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.iletcomfort.protocols import build_snapshot, get_adapter

CORPUS_DIR = Path(__file__).parent / "corpus"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "diagnostics_golden.json"
UPDATE_ENV = "ILET_UPDATE_DIAGNOSTICS_GOLDEN"
APPLIANCE_CODE = "153931629126443"
SECRETS = ("user@example.com", "secret-password", APPLIANCE_CODE, "SECRET-SN", "Owner Name")


def _hex_csv(csv: str) -> bytes:
    return bytes(int(part, 16) for part in csv.split(","))


def _corpus_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(CORPUS_DIR.glob("*.json")):
        cases.extend(json.loads(path.read_text(encoding="utf-8")))
    return cases


def _scenarios() -> Iterator[tuple[str, dict[str, Any], str]]:
    for case in _corpus_cases():
        yield f"{case['id']}|poll", case, "poll"
        yield f"{case['id']}|push", case, "push"
        if "0x05" in case["frames"]:
            yield f"{case['id']}|push_silent_flipped", case, "push_silent_flipped"
            yield f"{case['id']}|push_silent_unknown", case, "push_silent_unknown"


SCENARIOS = list(_scenarios())


def _state(case: dict[str, Any], mode: str) -> Any:
    adapter = get_adapter(case["sn8"])
    raw_frames = {int(key, 16): _hex_csv(value) for key, value in case["frames"].items()}
    raw01 = bytearray(raw_frames[0x01])
    if mode in ("push_silent_flipped", "push_silent_unknown"):
        # Toggle the 0x01 binary Silent bit (body[3] & 0x02) so it disagrees
        # with the retained 0x05 frame.
        raw01[3] ^= 0x02
        raw_frames[0x01] = bytes(raw01)
    if mode == "push_silent_unknown":
        # 0x05 Silent off while 0x01 reports Silent on: level unknown.
        silent = bytearray(raw_frames[0x05])
        silent[1] &= 0xFE
        raw_frames[0x05] = bytes(silent)
    status = adapter.decode_status(decode_its_status(raw01))
    sensors = decode_its_sensors(raw_frames[0x02]) if 0x02 in raw_frames else None
    return build_snapshot(
        adapter,
        status,
        sensors,
        raw_frames,
        retained_subtypes=mode != "poll",
    )


async def _diagnostics(hass: HomeAssistant, case: dict[str, Any], mode: str) -> Any:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=f"user@example.com:{APPLIANCE_CODE}",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret-password",
            CONF_APPLIANCE_CODE: APPLIANCE_CODE,
            CONF_REGION: REGION_US,
            CONF_KJRH120L_ZONE1_CAPABLE: mode == "push",
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {
        "applianceCode": APPLIANCE_CODE,
        "applianceType": "0xC3",
        "modelNumber": "0",
        "sn8": case["sn8"],
        "sn": "SECRET-SN",
        "owner": "Owner Name",
        "name": "Owner Name",
    }
    coord.data = _state(case, mode)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    result = await async_get_config_entry_diagnostics(hass, entry)
    # Round-trip through JSON exactly as Home Assistant serves the download.
    return json.loads(json.dumps(result))


def _load_golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("key", "case", "mode"), SCENARIOS, ids=[key for key, _, _ in SCENARIOS]
)
async def test_diagnostics_match_golden(
    hass: HomeAssistant, key: str, case: dict[str, Any], mode: str
) -> None:
    """Diagnostics keep their keys, key order and values for every corpus case."""
    result = await _diagnostics(hass, case, mode)
    dumped = json.dumps(result, indent=1)

    if os.environ.get(UPDATE_ENV):
        golden = _load_golden() if GOLDEN_PATH.exists() else {}
        golden[key] = result
        GOLDEN_PATH.write_text(
            json.dumps(dict(sorted(golden.items())), indent=1) + "\n",
            encoding="utf-8",
        )

    # json.dumps without sort_keys also pins the key order of every mapping.
    assert dumped == json.dumps(_load_golden()[key], indent=1)
    for secret in SECRETS:
        assert secret not in dumped


def test_diagnostics_golden_covers_every_scenario() -> None:
    """The golden file has exactly one payload per corpus scenario."""
    assert sorted(_load_golden()) == sorted(key for key, _, _ in SCENARIOS)
