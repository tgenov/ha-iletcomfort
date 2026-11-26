"""Tests for ProtocolAdapter registry, HeatPumpState, and async_execute_command (#172)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.iletcomfort.api import ApiError, AuthError, ITSSensors, ITSStatus, MODE_COOL, MODE_HEAT
from custom_components.iletcomfort.const import (
    CONF_APPLIANCE_CODE,
    CONF_OPERATION_MODE,
    CONF_REGION,
    DOMAIN,
    OPERATION_MODE_PHONE_APP,
    REGION_EU,
)
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
from custom_components.iletcomfort.model_profiles import (
    AQUAPURA_SN8,
    ATW_SN8,
    KJRH120L_SN8,
    ModelProfile,
    decode_atw_status,
)
from custom_components.iletcomfort.protocols import (
    AquapuraAdapter,
    AtwAdapter,
    HeatPumpState,
    Kjrh120lAdapter,
    StandardAdapter,
    build_snapshot,
    get_adapter,
    is_kjrh120l_dual_state,
    merge_atw_energy_notify,
)
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_B
from tests.snapshot_helpers import make_state
from tests.test_atw_unit_params import NORDIS_UNIT_PARAMS_0X10
from tests.test_model_profiles import ATW_PLACEHOLDER_SENSORS_BODY, NORDIS_ATW_STATUS_FRAMES


def test_get_adapter_resolves_all_profiles_and_sn8() -> None:
    assert isinstance(get_adapter(None), StandardAdapter)
    assert isinstance(get_adapter("UNKNOWN8"), StandardAdapter)
    assert isinstance(get_adapter(AQUAPURA_SN8), AquapuraAdapter)
    assert isinstance(get_adapter(ATW_SN8), AtwAdapter)
    assert isinstance(get_adapter(KJRH120L_SN8), Kjrh120lAdapter)
    assert isinstance(get_adapter(ModelProfile.ATW), AtwAdapter)


def test_adapter_build_query_and_variant_description() -> None:
    assert get_adapter(KJRH120L_SN8).build_query(0x01) == "ffff010101ff"
    assert get_adapter(ATW_SN8).build_query(0x01).startswith("aa")

    atw = get_adapter(ATW_SN8)
    raw_status = MagicMock(raw_body=NORDIS_ATW_STATUS_FRAMES["issue132"])
    decoded = atw.decode_status(raw_status)
    assert atw.describe_variant(decoded) == "atw_27_byte"

    state = HeatPumpState(
        profile=ModelProfile.ATW,
        variant="atw_27_byte",
        status=decoded,
    )
    assert state.error_code == 0
    assert state.compressor_running is None


def _coordinator(
    hass: HomeAssistant, *, phone_app_mode: bool = False
) -> ILetComfortCoordinator:
    options = (
        {CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP} if phone_app_mode else {}
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="user@example.com:APPL1",
        data={
            CONF_EMAIL: "user@example.com",
            CONF_PASSWORD: "secret",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        options=options,
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        return ILetComfortCoordinator(hass, entry)


async def test_async_execute_command_blocks_phone_app_mode(hass: HomeAssistant) -> None:
    coord = _coordinator(hass, phone_app_mode=True)
    action = MagicMock()
    with pytest.raises(HomeAssistantError, match="phone app coexistence"):
        await coord.async_execute_command(action)
    action.assert_not_called()


async def test_async_execute_command_reauths_once_and_refreshes(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    coord._async_login = AsyncMock()  # type: ignore[method-assign]
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    calls = 0

    def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise AuthError("expired")
        return "ok"

    result = await coord.async_execute_command(flaky)
    assert result == "ok"
    assert calls == 2
    coord._async_login.assert_awaited_once()
    coord.async_request_refresh.assert_awaited_once()


@pytest.mark.parametrize(
    "exc",
    [
        ApiError("code=1214"),
        ValueError("out of range"),
        requests.ConnectionError("timeout"),
    ],
)
async def test_async_execute_command_translates_errors_without_refresh(
    hass: HomeAssistant, exc: Exception
) -> None:
    coord = _coordinator(hass)
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    def failing() -> None:
        raise exc

    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coord.async_execute_command(failing)
    coord.async_request_refresh.assert_not_awaited()


async def test_async_execute_command_propagates_reauth_failure(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    coord._async_login = AsyncMock(side_effect=AuthError("bad creds"))  # type: ignore[method-assign]
    coord.async_request_refresh = AsyncMock()  # type: ignore[method-assign]

    def failing() -> None:
        raise AuthError("expired")

    with pytest.raises(HomeAssistantError, match="Heat pump command failed"):
        await coord.async_execute_command(failing)
    coord.async_request_refresh.assert_not_awaited()


def test_apply_push_status_preserves_raw_frames_and_updates_state(
    hass: HomeAssistant,
) -> None:
    coord = _coordinator(hass)
    coord.appliance_meta = {"applianceCode": "APPL1", "sn8": ATW_SN8}
    coord.data = make_state(
        sensors=ITSSensors(th_temp=46.0),
        raw_frames={0x02: b"\x02\x00"},
        sn8=ATW_SN8,
    )
    pushed = MagicMock(raw_body=NORDIS_ATW_STATUS_FRAMES["issue132"])

    coord._apply_push_status(pushed)

    state = coord.data
    assert isinstance(state, HeatPumpState)
    assert state.raw_frames[0x02] == b"\x02\x00"
    assert state.raw_frames[0x01] == NORDIS_ATW_STATUS_FRAMES["issue132"]
    assert state.profile is ModelProfile.ATW
    assert state.variant == "atw_27_byte"


def test_make_state_builds_typed_snapshot_via_build_snapshot() -> None:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    state = make_state(
        status,
        ITSSensors(
            twin_temp=20.0,
            th_temp=46.0,
            raw_body=ATW_PLACEHOLDER_SENSORS_BODY,
        ),
        {0x02: ATW_PLACEHOLDER_SENSORS_BODY},
        sn8=ATW_SN8,
    )
    assert state.profile is ModelProfile.ATW
    assert state.variant == "atw_27_byte"
    assert state.has_atw_0x10 is False
    assert state.atw_c3_basic is not None
    assert state.climate_current_temperature is None


def test_heat_pump_state_has_atw_0x10_follows_raw_frames_only() -> None:
    sensors_with_0x10 = ITSSensors(twout_temp=24.0, raw_body=NORDIS_UNIT_PARAMS_0X10)

    # raw_frames is authoritative: a 0x10-prefixed sensors body alone does not count.
    missing = HeatPumpState(
        profile=ModelProfile.ATW,
        variant="atw_unclassified",
        status=ITSStatus(),
        sensors=sensors_with_0x10,
        raw_frames={0x02: ATW_PLACEHOLDER_SENSORS_BODY},
    )
    assert missing.has_atw_0x10 is False

    present = HeatPumpState(
        profile=ModelProfile.ATW,
        variant="atw_unclassified",
        status=ITSStatus(),
        sensors=sensors_with_0x10,
        raw_frames={0x10: NORDIS_UNIT_PARAMS_0X10},
    )
    assert present.has_atw_0x10 is True


def test_heat_pump_state_climate_helpers_and_kjrh120l_dual() -> None:
    aqua = HeatPumpState(
        profile=ModelProfile.AQUAPURA,
        variant="aquapura",
        status=ITSStatus(),
        sensors=ITSSensors(th_temp=0.0, twout_temp=31.0, twin_temp=28.0),
    )
    assert aqua.climate_current_temperature == 31.0
    assert aqua.climate_temp_bounds(MODE_HEAT) == (10.0, 40.0)
    assert aqua.climate_temp_bounds(MODE_COOL) == (12.0, 40.0)
    assert aqua.atw_c3_basic is None

    # KJRH-120L temp bounds & dual-variant check including None data.
    kjrh_state = HeatPumpState(
        profile=ModelProfile.KJRH120L, variant="kjrh120l_pure_dhw", status=ITSStatus()
    )
    assert kjrh_state.climate_temp_bounds(MODE_HEAT) == (20.0, 70.0)
    assert is_kjrh120l_dual_state(KJRH120L_SN8, None, True) is True
    assert is_kjrh120l_dual_state(KJRH120L_SN8, None, False) is False
    assert is_kjrh120l_dual_state(ATW_SN8, None, True) is False
    assert (
        is_kjrh120l_dual_state(
            KJRH120L_SN8,
            HeatPumpState(
                profile=ModelProfile.KJRH120L,
                variant="kjrh120l_pure_dhw",
                status=MagicMock(raw_body=KJRH120L_SNAPSHOT_B),
            ),
            False,
        )
        is False
    )


def test_all_dialect_adapters_implement_common_seam() -> None:
    """Every dialect owns its temperature routing, bounds, and variant rules (#110)."""
    std = get_adapter(None)
    assert std.climate_current_temperature(ITSSensors(twin_temp=22.0, th_temp=45.0)) == 22.0
    assert std.climate_temp_bounds(None, MODE_HEAT) == (10.0, 40.0)
    assert std.is_kjrh120l_dual(None, True) is False
    assert std.c3_basic(None) is None

    aqua = get_adapter(AQUAPURA_SN8)
    assert aqua.climate_current_temperature(ITSSensors(th_temp=42.0, twout_temp=30.0)) == 42.0
    assert aqua.climate_current_temperature(ITSSensors(th_temp=0.0, twout_temp=30.0)) == 30.0
    assert aqua.climate_temp_bounds(None, MODE_COOL) == (12.0, 40.0)
    assert aqua.is_kjrh120l_dual(None, True) is False
    assert aqua.c3_basic(None) is None

    atw = get_adapter(ATW_SN8)
    atw_status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    assert atw.climate_current_temperature(
        ITSSensors(twout_temp=34.0, raw_body=NORDIS_UNIT_PARAMS_0X10)
    ) == 34.0
    assert atw.climate_current_temperature(
        ITSSensors(th_temp=46.0, raw_body=ATW_PLACEHOLDER_SENSORS_BODY)
    ) is None
    assert atw.climate_temp_bounds(atw_status, MODE_HEAT) == (25.0, 55.0)
    assert atw.is_kjrh120l_dual(atw_status, True) is False
    assert atw.c3_basic(atw_status) is atw_status.c3_basic

    kjrh = get_adapter(KJRH120L_SN8)
    assert kjrh.climate_current_temperature(ITSSensors(twin_temp=19.0)) == 19.0
    assert kjrh.climate_temp_bounds(None, MODE_HEAT) == (20.0, 70.0)
    assert kjrh.is_kjrh120l_dual(None, True) is True
    assert kjrh.is_kjrh120l_dual(None, False) is False
    assert kjrh.c3_basic(atw_status) is None


# --- build_snapshot: the single poll/push snapshot assembly step (#225) -----


def _energy_frame(kwh: int, thermal: int, outdoor: int) -> bytes:
    return (
        b"\x04\x00"
        + kwh.to_bytes(4, "big")
        + thermal.to_bytes(4, "big")
        + bytes([outdoor])
    )


def test_build_snapshot_applies_atw_overlays_and_energy_last() -> None:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    assert status.c3_basic is not None
    raw_frames = {
        0x01: NORDIS_ATW_STATUS_FRAMES["issue132"],
        0x02: ATW_PLACEHOLDER_SENSORS_BODY,
        0x10: NORDIS_UNIT_PARAMS_0X10,
        0x0A: b"\x0a\x00\x00\x00\x09\x29",
        0x05: b"\x05\x09\x00\x00\x00\x00\x00\x00\x00\x00",
        0x07: b"\x07\x01\x00\x00\x00\x00\x00",
        0x04: _energy_frame(99999, 88888, 0xFB),
    }

    state = build_snapshot(
        get_adapter(ATW_SN8),
        status,
        ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY),
        raw_frames,
    )

    assert state.raw_frames == raw_frames
    assert state.raw_frames is not raw_frames
    assert state.status is not None and state.sensors is not None
    assert state.status.comp_frq == NORDIS_UNIT_PARAMS_0X10[1]
    assert state.status.comp_total_run_hours == 0x0929
    assert state.status.atw_silent_level == 2
    assert state.status.atw_eco_mode is True
    # 0x04 is applied after 0x10, so the push energy/outdoor values win.
    assert state.status.total_kwh == 99999
    assert state.status.total_thermal_kwh == 88888
    assert state.sensors.t4_temp == -5.0
    assert state.has_atw_0x10 is True

    # Re-building on the built snapshot is idempotent (status push path).
    again = build_snapshot(
        get_adapter(ATW_SN8), state.status, state.sensors, raw_frames
    )
    assert again.status == state.status
    assert again.sensors == state.sensors


def test_build_snapshot_skips_silent_eco_without_c3_basic() -> None:
    status = ITSStatus(raw_body=b"\x01\x00")
    assert status.c3_basic is None
    state = build_snapshot(
        get_adapter(ATW_SN8),
        status,
        None,
        {0x05: b"\x05\x01\x00\x00\x00\x00\x00", 0x07: b"\x07\x01\x00\x00"},
    )
    assert state.status is not None
    assert state.status.atw_silent_mode is None
    assert state.status.atw_eco_mode is None
    assert state.sensors is None


def test_build_snapshot_ignores_atw_frames_for_other_profiles() -> None:
    status = ITSStatus(mode=1)
    sensors = ITSSensors(twin_temp=21.0)
    state = build_snapshot(
        get_adapter(None), status, sensors, {0x10: NORDIS_UNIT_PARAMS_0X10}
    )
    assert state.profile is ModelProfile.STANDARD
    assert state.status is status
    assert state.sensors is sensors


def test_merge_atw_energy_notify_keeps_last_valid_fields() -> None:
    previous = _energy_frame(14126, 32181, 0xFB)
    sentinel = _energy_frame(0xFFFFFFFF, 0, 0x7F)
    assert merge_atw_energy_notify(previous, sentinel) == previous
    partial = _energy_frame(14130, 0, 0x02)
    assert merge_atw_energy_notify(previous, partial) == _energy_frame(
        14130, 32181, 0x02
    )
    assert merge_atw_energy_notify(None, sentinel) == sentinel



def _airwell_139_frames() -> dict[int, bytes]:
    import json
    from pathlib import Path

    path = Path(__file__).parent / "corpus" / "atw_171H120F.json"
    case = next(
        c
        for c in json.loads(path.read_text(encoding="utf-8"))
        if c["id"] == "atw_airwell_split_silent_eco_issue139"
    )
    return {
        int(k, 16): bytes(int(p, 16) for p in v.split(","))
        for k, v in case["frames"].items()
    }


def test_build_snapshot_retained_mode_reconciles_airwell_silent_with_0x01() -> None:
    """Issue #139 corpus pair: 0x01 Silent bit clear, 0x05 Silent L1 on.

    Poll mode trusts the freshly queried 0x05 (corpus contract: L1 on). In
    retained mode (MQTT push, 0x05 from an earlier poll) the current 0x01 bit
    wins for on/off, so the same pair reads Off / level 0.
    """
    frames = _airwell_139_frames()
    status = decode_atw_status(frames[0x01])
    assert status.c3_basic is not None and status.c3_basic.silent_mode is False
    adapter = get_adapter(ATW_SN8)

    polled = build_snapshot(adapter, status, None, frames)
    assert polled.status is not None
    assert polled.status.atw_silent_mode is True
    assert polled.status.atw_silent_level == 1

    pushed = build_snapshot(adapter, status, None, frames, retained_subtypes=True)
    assert pushed.status is not None
    assert pushed.status.atw_silent_mode is False
    assert pushed.status.atw_silent_level == 0
    assert pushed.status.atw_silent_super_selected is False
    # ECO and run hours are re-applied unchanged in retained mode.
    assert pushed.status.atw_eco_mode is True
    assert pushed.status.comp_total_run_hours == 2432


def test_build_snapshot_retained_mode_applies_only_0x10_counters() -> None:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    sensors = ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY)
    frames = {0x10: NORDIS_UNIT_PARAMS_0X10}
    adapter = get_adapter(ATW_SN8)

    full = build_snapshot(adapter, status, sensors, frames)
    retained = build_snapshot(adapter, status, sensors, frames, retained_subtypes=True)

    assert full.status is not None and retained.status is not None
    assert full.status.comp_frq is not None
    assert retained.status.comp_frq is None
    assert retained.status.comp_running is None
    assert retained.status.pressure_h is None
    # Instantaneous fields stay as the 0x01 decode leaves them (exv_drg is a
    # non-optional int defaulting to 0 in ITSStatus).
    assert retained.status.exv_drg == status.exv_drg
    assert full.status.exv_drg != status.exv_drg
    assert retained.status.total_kwh == full.status.total_kwh
    assert retained.status.total_thermal_kwh == full.status.total_thermal_kwh
    # Sensor-side 0x10 values are not re-applied in retained mode; the
    # retained sensors already carry them from the poll.
    assert retained.sensors is not None
    assert retained.sensors.raw_body == ATW_PLACEHOLDER_SENSORS_BODY


def test_build_snapshot_energy_notify_overrides_literal_0x04() -> None:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    literal = _energy_frame(0xFFFFFFFF, 0, 0x7F)
    merged = _energy_frame(14126, 32181, 0xFB)
    state = build_snapshot(
        get_adapter(ATW_SN8),
        status,
        ITSSensors(),
        {0x04: literal},
        energy_notify=merged,
    )
    assert state.raw_frames[0x04] == literal
    assert state.status is not None and state.sensors is not None
    assert state.status.total_kwh == 14126
    assert state.sensors.t4_temp == -5.0
