"""Dialect-projected ClimateView per regime (#192).

The climate entities are a thin mapping of these views, so the per-model
mode / target / step / bounds / feature / refusal rules are pinned here.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.iletcomfort.api import ITSStatus
from custom_components.iletcomfort.commands import (
    SetDhwTargetCommand,
    SetTargetTemperatureCommand,
    SetZoneTargetCommand,
)
from custom_components.iletcomfort.protocols import (
    ClimateView,
    WriteContext,
    get_adapter,
)
from custom_components.iletcomfort.protocols.atw import decode_atw_status
from custom_components.iletcomfort.protocols.kjrh120l import decode_kjrh120l_status
from custom_components.iletcomfort.protocols.views import (
    ATW_CONTROL_DISABLED,
    FAN_ONLY_TEMPERATURE_REFUSED,
    KJRH120L_ZONE1_READ_ONLY,
    NO_ZONE2_VIEW,
    ViewContext,
)
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A, KJRH120L_SNAPSHOT_B

ATW_SN8 = "171H120F"
KJRH_SN8 = "17100003"
AQUAPURA_SN8 = "171000AU"

# Rotenso Aquami (tests/test_c3_basic.py ROTENSO_BASELINE): Zone 1 off, water
# control, heat range 25-55 °C, room range 17-30 °C, heat-only.
ROTENSO = bytes.fromhex(
    "01 08 15 a0 03 03 1d 1e 37 30 37 19 19 05 37 19 19 05 3c 22 3c 14 10 00 80"
)
# Kaisai #151 (tests/test_c3_basic.py KAISAI_DUAL_ZONE): both zones on, heat
# and cool capable, Zone 2 water target 25 °C.
KAISAI = bytes.fromhex(
    "01 1f 3f 00 03 03 23 19 2c 30 37 19 19 05 37 19 19 05 3c 22 3c 14 2c 00 80"
)

READ_ONLY = ViewContext()
CONTROL = ViewContext(c3_basic_control=True)
LATCHED = ViewContext(kjrh120l_zone1_latched=True)


def _with(body: bytes, **index_values: int) -> bytes:
    out = bytearray(body)
    for key, value in index_values.items():
        out[int(key.removeprefix("b"))] = value
    return bytes(out)


def _view(sn8, status, ctx=READ_ONLY) -> ClimateView:
    return get_adapter(sn8).climate_view(status, ctx)


# -- legacy (STANDARD / AQUAPURA) ---------------------------------------------------


@pytest.mark.parametrize("sn8", [None, AQUAPURA_SN8])
def test_legacy_heat_view(sn8):
    view = _view(sn8, ITSStatus(mode=1, t5s_def=31.0))

    assert view == ClimateView(
        hvac_modes=("off", "heat", "cool", "fan_only"),
        hvac_mode="heat",
        target_temperature=31.0,
        target_temperature_step=1.0,
        min_temp=10.0,
        max_temp=40.0,
        supports_target_temperature=True,
        supports_turn_on_off=True,
    )


@pytest.mark.parametrize(
    ("mode", "hvac_mode", "bounds"),
    [(0, "off", (10.0, 40.0)), (2, "cool", (12.0, 40.0)), (3, "off", (10.0, 40.0))],
)
def test_legacy_mode_selects_bounds(mode, hvac_mode, bounds):
    view = _view(None, ITSStatus(mode=mode, set_temperature=30))

    assert view.hvac_mode == hvac_mode
    assert (view.min_temp, view.max_temp) == bounds
    # No t5s_def: the direct target byte is the setpoint.
    assert view.target_temperature == 30.0


def test_legacy_fan_only_hides_target_and_refuses_temperature():
    view = _view(None, ITSStatus(mode=4, t5s_def=31.0))

    assert view.hvac_mode == "fan_only"
    assert view.target_temperature is None
    assert (view.min_temp, view.max_temp) == (15.0, 40.0)
    assert not view.supports_target_temperature
    assert view.supports_turn_on_off
    assert view.temperature_refusal == FAN_ONLY_TEMPERATURE_REFUSED
    assert view.read_only_reason is None


def test_view_without_status():
    view = _view(None, None)

    assert view.hvac_mode == "off"
    assert view.target_temperature is None
    assert (view.min_temp, view.max_temp) == (10.0, 40.0)


@pytest.mark.parametrize("sn8", [None, AQUAPURA_SN8, KJRH_SN8])
def test_non_atw_dialects_have_no_zone2(sn8):
    assert get_adapter(sn8).zone2_climate_view(ITSStatus(mode=1), CONTROL) == NO_ZONE2_VIEW
    assert not NO_ZONE2_VIEW.available
    assert NO_ZONE2_VIEW.read_only_reason == ATW_CONTROL_DISABLED


# -- KJRH-120L ---------------------------------------------------------------------


def test_kjrh120l_pure_dhw_view_controls_dhw_with_20_70_bounds():
    view = _view(KJRH_SN8, decode_kjrh120l_status(KJRH120L_SNAPSHOT_B))

    assert view.read_only_reason is None
    assert (view.min_temp, view.max_temp) == (20.0, 70.0)
    assert view.supports_target_temperature and view.supports_turn_on_off


@pytest.mark.parametrize(
    ("body", "ctx"),
    [(KJRH120L_SNAPSHOT_A, READ_ONLY), (KJRH120L_SNAPSHOT_B, LATCHED)],
)
def test_kjrh120l_dual_view_is_a_read_only_zone1_card(body, ctx):
    status = decode_kjrh120l_status(body)
    view = _view(KJRH_SN8, status, ctx)

    assert view.read_only_reason == KJRH120L_ZONE1_READ_ONLY
    assert not view.supports_target_temperature
    assert not view.supports_turn_on_off
    # The Zone 1 setpoint, even while Zone 1 is off (snapshot B).
    assert view.target_temperature == status.kjrh120l.zone1_setpoint


def test_kjrh120l_latched_without_status_is_read_only():
    view = _view(KJRH_SN8, None, LATCHED)

    assert view.read_only_reason == KJRH120L_ZONE1_READ_ONLY
    assert view.target_temperature is None


# -- ATW -----------------------------------------------------------------------------


def test_atw_without_c3_layout_uses_the_legacy_view():
    view = _view(ATW_SN8, ITSStatus(mode=1, t5s_def=30.0), CONTROL)

    assert view.hvac_modes == ("off", "heat", "cool", "fan_only")
    assert view.read_only_reason is None


def test_atw_read_only_view():
    view = _view(ATW_SN8, decode_atw_status(ROTENSO))

    assert view == ClimateView(
        hvac_modes=("off", "heat"),
        hvac_mode="off",
        target_temperature=29.0,
        target_temperature_step=1.0,
        min_temp=25.0,
        max_temp=55.0,
        supports_target_temperature=True,
        supports_turn_on_off=True,
        read_only_reason=ATW_CONTROL_DISABLED,
    )


def test_atw_read_only_room_mode_uses_half_degree_room_range():
    body = _with(ROTENSO, b2=ROTENSO[2] & ~0x10)
    view = _view(ATW_SN8, decode_atw_status(body))

    assert view.target_temperature_step == 0.5
    assert (view.min_temp, view.max_temp) == (17.0, 30.0)
    assert view.target_temperature == 24.0


def test_atw_read_only_lists_cool_for_cool_capable_unit():
    view = _view(ATW_SN8, decode_atw_status(KAISAI))

    assert view.hvac_modes == ("off", "heat", "cool")
    assert view.hvac_mode == "heat"


def test_atw_controlled_view_selects_heat_off_only():
    view = _view(ATW_SN8, decode_atw_status(KAISAI), CONTROL)

    assert view.hvac_modes == ("off", "heat")
    assert view.read_only_reason is None
    assert view.settable_hvac_modes is None  # plan_write refuses Cool
    assert view.target_temperature == 35.0
    assert (view.min_temp, view.max_temp) == (25.0, 55.0)


def test_atw_controlled_view_reports_cooling_unit():
    body = _with(KAISAI, b4=2)
    view = _view(ATW_SN8, decode_atw_status(body), CONTROL)

    assert view.hvac_modes == ("off", "heat", "cool")
    assert view.hvac_mode == "cool"


def test_atw_controlled_view_falls_back_when_range_is_zeroed():
    body = _with(ROTENSO, b10=0, b11=0, b12=0, b13=0)
    view = _view(ATW_SN8, decode_atw_status(body), CONTROL)

    assert (view.min_temp, view.max_temp) == (10.0, 40.0)


def test_atw_zone2_views():
    adapter = get_adapter(ATW_SN8)
    single = decode_atw_status(ROTENSO)
    dual = decode_atw_status(KAISAI)

    assert not adapter.zone2_climate_view(single, CONTROL).available
    read_only = adapter.zone2_climate_view(dual, READ_ONLY)
    assert read_only.available
    assert read_only.read_only_reason == ATW_CONTROL_DISABLED
    assert not read_only.supports_turn_on_off
    assert read_only.hvac_modes == ("off", "heat", "cool")
    controlled = adapter.zone2_climate_view(dual, CONTROL)
    assert controlled.read_only_reason is None
    assert controlled.supports_target_temperature and controlled.supports_turn_on_off
    assert controlled.target_temperature == 25.0
    assert controlled.settable_hvac_modes == ("off", "heat")


# -- the dialect clamps target writes to the view ---------------------------------------


def test_clamp():
    view = dataclasses.replace(NO_ZONE2_VIEW, min_temp=25.0, max_temp=55.0)
    assert view.clamp(5) == 25.0
    assert view.clamp(60) == 55.0
    assert view.clamp(30.5) == 30.5


@pytest.mark.parametrize(("requested", "sent"), [(90, "00070146ff"), (5, "00070114ff")])
def test_kjrh120l_climate_target_is_clamped_to_20_70(requested, sent):
    plan = get_adapter(KJRH_SN8).plan_write(
        SetTargetTemperatureCommand(requested),
        WriteContext(status=decode_kjrh120l_status(KJRH120L_SNAPSHOT_B)),
    )
    assert plan.resolve() == sent


@pytest.mark.parametrize(("requested", "sent"), [(90, "0007013cff"), (5, "00070123ff")])
def test_kjrh120l_dhw_target_is_clamped_to_35_60(requested, sent):
    plan = get_adapter(KJRH_SN8).plan_write(SetDhwTargetCommand(requested), WriteContext())
    assert plan.resolve() == sent


def test_atw_zone2_target_is_clamped_to_reported_range():
    ctx = WriteContext(status=decode_atw_status(KAISAI), c3_basic_control=True)
    adapter = get_adapter(ATW_SN8)

    high = adapter.plan_write(SetZoneTargetCommand(2, 80), ctx).resolve(KAISAI)
    at_max = adapter.plan_write(SetZoneTargetCommand(2, 55), ctx).resolve(KAISAI)

    assert high == at_max
