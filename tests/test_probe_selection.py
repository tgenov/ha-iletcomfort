"""Characterization tests for model selection side effects outside the dialects (#110).

These pin, per corpus capture, everything the coordinator / options flow /
diagnostics decide from the resolved dialect, so moving those decisions behind
``ProtocolAdapter`` cannot change behaviour:

- which optional C3 subtypes a poll queries, in which order, under which
  conditions (0x10 then 0x0A on an ATW 0x02 placeholder, 0x05 / 0x07 on a C3
  basic ATW status), including session-disable after a first ``ApiError``;
- whether a KJRH-120L Zone-1 observation is latched;
- whether the options flow offers C3 basic control;
- the diagnostics ``atw_0x02_placeholder`` flag;
- whether an MQTT 0x04 energy push changes the snapshot.

The tests drive only public entry points (``_poll``, ``_apply_push_energy``,
the options flow helper and diagnostics), never the adapter API under test.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import (
    ApiError,
    decode_its_sensors,
    decode_its_status,
)
from custom_components.iletcomfort.config_flow import ILetComfortOptionsFlow
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_KJRH120L_ZONE1_CAPABLE,
    CONF_REGION,
    DOMAIN,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.iletcomfort.protocols import get_adapter

CORPUS_DIR = Path(__file__).parent / "corpus"
CASES: list[dict[str, Any]] = [
    case
    for path in sorted(CORPUS_DIR.glob("*.json"))
    for case in json.loads(path.read_text())
]
CASE_IDS = [case["id"] for case in CASES]

# Optional-subtype fetchers on the client, keyed by the subtype they query.
FETCHERS = {
    0x10: "query_atw_unit_params",
    0x0A: "query_atw_hmi_params",
    0x05: "query_atw_silent",
    0x07: "query_atw_eco",
}


def _hx(csv: str) -> bytes:
    return bytes(int(part, 16) for part in csv.split(","))


def _frames(case: dict[str, Any]) -> dict[int, bytes]:
    return {int(key, 16): _hx(value) for key, value in case["frames"].items()}


def _first_frame(subtype: int) -> bytes:
    for case in CASES:
        frame = _frames(case).get(subtype)
        if frame is not None:
            return frame
    raise AssertionError(f"no corpus frame for subtype 0x{subtype:02x}")


# Valid replies for every optional subtype, taken from real captures.
REPLIES = {subtype: _first_frame(subtype) for subtype in FETCHERS}
ENERGY_0X04 = bytes([0x04, 0x00, 0, 0, 0x03, 0xE8, 0, 0, 0x0B, 0xB8, 0x0A])

# Probe call order per poll with every optional subtype answering, captured on
# main @ 67082de before the #110 close-out refactor.
EXPECTED_ALL_OK: dict[str, list[str]] = {
    "atw_italtherm_25byte_issue22": ["silent", "eco"],
    "atw_galmet_27byte_placeholder_issue38": ["unit", "hmi", "silent", "eco"],
    "atw_nordis_27byte_with_0x10_and_0x0a_issue120_132": [
        "unit", "hmi", "silent", "eco",
    ],
    # 75-byte active-schedule 0x02 template (#120): 0x10/0x0A are probed.
    "atw_nordis_75byte_active_schedule_issue120": [
        "unit", "hmi", "silent", "eco",
    ],
    "atw_rotenso_25byte_issue124": ["unit", "hmi", "silent", "eco"],
    "atw_airwell_active_schedule_issue128": ["unit", "hmi", "silent", "eco"],
    "atw_airwell_split_silent_eco_issue139": ["silent", "eco"],
    "atw_italtherm_hydrablock_no_dhw_issue138": ["unit", "hmi", "silent", "eco"],
    "atw_inventor_room_mode_issue125": ["unit", "hmi", "silent", "eco"],
    "atw_kosner_water_mode_issue134": ["unit", "hmi", "silent", "eco"],
    "atw_kaisai_dual_zone_water_mode_issue151": ["unit", "hmi", "silent", "eco"],
    "atw_galmet_prima_8gt_0x10_issue118": ["unit", "hmi", "silent", "eco"],
    "standard_issue11_msc70d2n8a": [],
    "aquapura_issue12_tank_temp_in_box_bottom": [],
    "aquapura_null_fill_tank_live_inlet_outlet": [],
    "kjrh120l_pure_dhw_off_issue5": [],
    "kjrh120l_dual_snapshot_a_issue148": [],
    "kjrh120l_dual_snapshot_b_zone1_off_issue148": [],
    "kjrh120l_dual_issue142_keramys_v0183": [],
}

_SHORT = {
    "query_atw_unit_params": "unit",
    "query_atw_hmi_params": "hmi",
    "query_atw_silent": "silent",
    "query_atw_eco": "eco",
}


def _make_coordinator(
    hass: HomeAssistant, case: dict[str, Any], sn8: str | None = "__case__"
) -> tuple[ILetComfortCoordinator, MagicMock, MagicMock]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="HP",
        unique_id=f"user@example.com:{case['id']}",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient") as cls:
        coord = ILetComfortCoordinator(hass, entry)
    case_sn8 = case["sn8"] if sn8 == "__case__" else sn8
    coord.appliance_meta = {"applianceCode": "APPL1", "sn8": case_sn8}
    frames = _frames(case)
    adapter = get_adapter(case_sn8)
    client = cls.return_value
    client.query_status.return_value = adapter.decode_status(
        decode_its_status(bytearray(frames[0x01]))
    )
    client.query_sensors.return_value = (
        decode_its_sensors(bytearray(frames[0x02]))
        if 0x02 in frames
        else decode_its_sensors(bytearray())
    )
    manager = MagicMock()
    for subtype, name in FETCHERS.items():
        fetcher = getattr(client, name)
        fetcher.return_value = REPLIES[subtype]
        manager.attach_mock(fetcher, name)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coord
    return coord, client, manager


def _probe_calls(manager: MagicMock) -> list[str]:
    return [_SHORT[call[0]] for call in manager.mock_calls if call[0] in _SHORT]


async def _poll(coord: ILetComfortCoordinator) -> Any:
    with patch(
        "custom_components.iletcomfort.coordinator.asyncio.sleep", new=AsyncMock()
    ):
        return await coord._poll()


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_probe_sequence_with_every_subtype_answering(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, _client, manager = _make_coordinator(hass, case)

    state = await _poll(coord)
    first = _probe_calls(manager)
    manager.reset_mock()
    await _poll(coord)
    second = _probe_calls(manager)

    expected = EXPECTED_ALL_OK[case["id"]]
    assert first == expected
    # Supported probes keep being queried every poll.
    assert second == expected
    probed = {
        "unit": 0x10, "hmi": 0x0A, "silent": 0x05, "eco": 0x07,
    }
    for name in expected:
        assert probed[name] in state.raw_frames


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_rejected_0x10_disables_it_and_never_queries_0x0a(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, client, manager = _make_coordinator(hass, case)
    client.query_atw_unit_params.side_effect = ApiError("code=1214")

    await _poll(coord)
    first = _probe_calls(manager)
    manager.reset_mock()
    await _poll(coord)
    second = _probe_calls(manager)

    expected = EXPECTED_ALL_OK[case["id"]]
    assert "hmi" not in first + second
    assert first == [name for name in expected if name != "hmi"]
    assert second == [name for name in expected if name not in ("unit", "hmi")]
    if "unit" in expected:
        assert coord.capabilities.atw_unit_params.supported is False
    else:
        assert coord.capabilities.atw_unit_params.supported is None


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_every_probe_rejected_disables_all_for_the_session(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, client, manager = _make_coordinator(hass, case)
    for name in FETCHERS.values():
        getattr(client, name).side_effect = ApiError("code=1214")

    await _poll(coord)
    first = _probe_calls(manager)
    manager.reset_mock()
    await _poll(coord)

    expected = EXPECTED_ALL_OK[case["id"]]
    assert first == [name for name in expected if name != "hmi"]
    assert _probe_calls(manager) == []
    probes = coord.capabilities.probes_as_dict()
    assert probes["0x0a_hmi_params"]["supported"] is None
    for key, name in (
        ("0x10_unit_params", "unit"),
        ("0x05_silent", "silent"),
        ("0x07_eco", "eco"),
    ):
        assert probes[key]["supported"] is (False if name in expected else None)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_transient_error_after_support_keeps_cached_frame(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, client, manager = _make_coordinator(hass, case)
    first = await _poll(coord)
    coord.data = first
    for name in FETCHERS.values():
        getattr(client, name).side_effect = ApiError("transient")
    manager.reset_mock()

    second = await _poll(coord)

    expected = EXPECTED_ALL_OK[case["id"]]
    # A transient error on a confirmed probe falls back to the last frame, so
    # 0x0A (gated on a 0x10 frame in this poll) is still queried.
    assert _probe_calls(manager) == expected
    for name, subtype in (
        ("unit", 0x10), ("hmi", 0x0A), ("silent", 0x05), ("eco", 0x07),
    ):
        if name in expected:
            assert second.raw_frames[subtype] == first.raw_frames[subtype]
        else:
            assert subtype not in second.raw_frames
    probes = coord.capabilities.probes_as_dict()
    for key, name in (
        ("0x10_unit_params", "unit"),
        ("0x0a_hmi_params", "hmi"),
        ("0x05_silent", "silent"),
        ("0x07_eco", "eco"),
    ):
        assert probes[key]["supported"] is (True if name in expected else None)


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_unknown_sn8_never_probes(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, _client, manager = _make_coordinator(hass, case, sn8=None)
    await _poll(coord)
    assert _probe_calls(manager) == []


# Latched KJRH-120L Zone-1 capability after one poll, per corpus capture.
EXPECTED_LATCH = {
    "kjrh120l_dual_snapshot_a_issue148": True,
    "kjrh120l_dual_issue142_keramys_v0183": True,
}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_kjrh120l_zone1_latch(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, _client, _manager = _make_coordinator(hass, case)
    assert coord.kjrh120l_zone1_capable is False
    await _poll(coord)
    expected = EXPECTED_LATCH.get(case["id"], False)
    assert (coord.entry.data.get(CONF_KJRH120L_ZONE1_CAPABLE) is True) is expected
    assert coord.kjrh120l_zone1_capable is expected


async def test_kjrh120l_latch_flag_ignored_on_other_models(
    hass: HomeAssistant,
) -> None:
    for case in CASES:
        coord, _client, _manager = _make_coordinator(hass, case)
        hass.config_entries.async_update_entry(
            coord.entry,
            data={**coord.entry.data, CONF_KJRH120L_ZONE1_CAPABLE: True},
        )
        assert coord.kjrh120l_zone1_capable is (case["sn8"] == "17100003")


@pytest.mark.parametrize(
    ("sn8", "offered"),
    [
        ("171H120F", True),
        ("17100003", False),
        ("17100007", False),
        ("171000AU", False),
        ("XXXXXXXX", False),
        (None, False),
    ],
)
def test_options_flow_offers_c3_basic_control(
    sn8: str | None, offered: bool
) -> None:
    entry = SimpleNamespace(entry_id="e")
    hass = SimpleNamespace(data={DOMAIN: {"e": SimpleNamespace(sn8=sn8)}})
    fake = SimpleNamespace(hass=hass, config_entry=entry)
    assert ILetComfortOptionsFlow._offers_c3_basic_control(fake) is offered  # type: ignore[arg-type]


def test_options_flow_without_loaded_coordinator_does_not_offer() -> None:
    fake = SimpleNamespace(
        hass=SimpleNamespace(data={}), config_entry=SimpleNamespace(entry_id="e")
    )
    assert ILetComfortOptionsFlow._offers_c3_basic_control(fake) is False  # type: ignore[arg-type]


# Diagnostics atw_0x02_placeholder per corpus capture (None = not applicable).
EXPECTED_PLACEHOLDER_FLAG: dict[str, bool | None] = {
    "atw_italtherm_25byte_issue22": None,
    "atw_galmet_27byte_placeholder_issue38": True,
    "atw_nordis_27byte_with_0x10_and_0x0a_issue120_132": True,
    "atw_nordis_75byte_active_schedule_issue120": True,
    "atw_rotenso_25byte_issue124": True,
    "atw_airwell_active_schedule_issue128": True,
    "atw_airwell_split_silent_eco_issue139": None,
    "atw_italtherm_hydrablock_no_dhw_issue138": True,
    "atw_inventor_room_mode_issue125": True,
    "atw_kosner_water_mode_issue134": True,
    "atw_kaisai_dual_zone_water_mode_issue151": True,
    "atw_galmet_prima_8gt_0x10_issue118": True,
    "standard_issue11_msc70d2n8a": None,
    "aquapura_issue12_tank_temp_in_box_bottom": None,
    "aquapura_null_fill_tank_live_inlet_outlet": None,
    "kjrh120l_pure_dhw_off_issue5": None,
    "kjrh120l_dual_snapshot_a_issue148": None,
    "kjrh120l_dual_snapshot_b_zone1_off_issue148": None,
    "kjrh120l_dual_issue142_keramys_v0183": None,
}


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_diagnostics_placeholder_flag(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, _client, _manager = _make_coordinator(hass, case)
    coord.data = await _poll(coord)
    diag = await async_get_config_entry_diagnostics(hass, coord.entry)
    assert (
        diag["protocol"]["atw_0x02_placeholder"]
        is EXPECTED_PLACEHOLDER_FLAG[case["id"]]
    )


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
async def test_energy_push_only_overlays_on_energy_notify_dialects(
    hass: HomeAssistant, case: dict[str, Any]
) -> None:
    coord, _client, _manager = _make_coordinator(hass, case)
    coord.data = await _poll(coord)
    before = coord.data

    coord._apply_push_energy(ENERGY_0X04)

    accepts = case["sn8"] == "171H120F"
    assert coord._last_push_status_at is not None
    if accepts:
        assert coord.data is not before
        assert coord.data.raw_frames[0x04] == ENERGY_0X04
        assert coord._atw_energy_notify == ENERGY_0X04
    else:
        assert coord.data is before
        assert coord._atw_energy_notify is None


@pytest.mark.parametrize(
    ("before", "after", "invalidated"),
    [
        (None, "171H120F", True),
        ("171H120F", None, True),
        (None, "XXXXXXXX", False),  # both resolve to the default dialect
        ("171H120F", "171H120F", False),
        (None, "17100003", True),
    ],
)
async def test_dialect_change_after_metadata_fetch_invalidates_caches(
    hass: HomeAssistant, before: str | None, after: str | None, invalidated: bool
) -> None:
    case = next(
        c for c in CASES if c["id"] == "atw_galmet_27byte_placeholder_issue38"
    )
    coord, client, _manager = _make_coordinator(hass, case, sn8=before)
    sentinel = MagicMock()
    coord.data = sentinel
    coord.capabilities.atw_unit_params.mark_unsupported("earlier")
    coord._last_atw_subtypes[0x10] = b"\x10stale"

    async def _fetch_meta() -> None:
        coord.appliance_meta = {"applianceCode": "APPL1", "sn8": after}

    seen: dict[str, Any] = {}
    status = client.query_status.return_value

    def _query_status(*_args: Any) -> Any:
        seen["data_at_query"] = coord.data
        return status

    client.query_status.side_effect = _query_status
    with patch.object(coord, "_ensure_appliance_meta", new=_fetch_meta):
        await _poll(coord)

    if invalidated:
        assert seen["data_at_query"] is None
        assert coord._last_atw_subtypes.get(0x10) != b"\x10stale"
        assert coord.capabilities.atw_unit_params.error != "earlier"
    else:
        assert seen["data_at_query"] is sentinel
        assert coord._last_atw_subtypes[0x10] == b"\x10stale"
        assert coord.capabilities.atw_unit_params.supported is False


async def test_every_optional_probe_subtype_has_a_coordinator_target(
    hass: HomeAssistant,
) -> None:
    """Guard the coordinator's subtype → (ProbeState, fetcher) map (#110 review).

    A dialect decides *which* optional subtypes to probe; the coordinator owns
    the ProbeState and client fetcher per subtype. An unmapped subtype would
    raise KeyError in ``_poll`` and fail every poll, so every subtype any
    registered adapter can return for any corpus capture must be mapped, and
    every ``requires`` must name a probe returned earlier in the same tuple.
    """
    from custom_components.iletcomfort.protocols import _ADAPTERS

    coord, _, _ = _make_coordinator(hass, CASES[0])
    seen: set[int] = set()
    for adapter in _ADAPTERS.values():
        for case in CASES:
            frames = _frames(case)
            status = adapter.decode_status(decode_its_status(bytearray(frames[0x01])))
            probes = adapter.optional_probes(status, frames)
            earlier: set[int] = set()
            for spec in probes:
                coord._probe_target(spec.subtype)  # raises KeyError if unmapped
                if spec.requires is not None:
                    assert spec.requires in earlier, (
                        f"{adapter.profile}: 0x{spec.subtype:02x} requires "
                        f"0x{spec.requires:02x}, which is not probed before it"
                    )
                earlier.add(spec.subtype)
                seen.add(spec.subtype)
    # The corpus exercises every optional subtype the coordinator maps.
    assert seen == set(FETCHERS)
