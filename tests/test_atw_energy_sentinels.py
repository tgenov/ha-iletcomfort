"""ATW 0x10 energy counters use the same sentinel filter as 0x04 (#232).

``total_kwh`` / ``total_thermal_kwh`` back TOTAL_INCREASING sensors. A 0x10
reply with ``00 00 00 00`` (HA records a meter reset) or ``ff ff ff ff``
(a ~4.29e9 kWh spike) must leave the prior value in place, exactly like the
MQTT 0x04 path.
"""

from __future__ import annotations

import dataclasses

import pytest

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.protocols.atw import (
    apply_atw_unit_params,
    decode_atw_status,
    valid_energy_counter,
)
from custom_components.iletcomfort.protocols.profile import ATW_SN8
from custom_components.iletcomfort.protocols import build_snapshot, get_adapter
from tests.test_atw_unit_params import NORDIS_UNIT_PARAMS_0X10
from tests.test_dialect_profiles import (
    ATW_PLACEHOLDER_SENSORS_BODY,
    NORDIS_ATW_STATUS_FRAMES,
)

_KWH = slice(67, 71)
_THERMAL = slice(71, 75)
_SENTINELS = {
    "zero": b"\x00\x00\x00\x00",
    "all_ff": b"\xff\xff\xff\xff",
}
_PRIOR_KWH = 6000
_PRIOR_THERMAL = 24000


def _with_counters(kwh: bytes | None = None, thermal: bytes | None = None) -> bytes:
    """Return the real NØRDIS 0x10 frame with only the energy bytes mutated."""
    body = bytearray(NORDIS_UNIT_PARAMS_0X10)
    if kwh is not None:
        body[_KWH] = kwh
    if thermal is not None:
        body[_THERMAL] = thermal
    return bytes(body)


def _status(prior: bool) -> ITSStatus:
    status = decode_atw_status(NORDIS_ATW_STATUS_FRAMES["issue132"])
    if prior:
        status = dataclasses.replace(
            status, total_kwh=_PRIOR_KWH, total_thermal_kwh=_PRIOR_THERMAL
        )
    return status


def _energy_frame(kwh: int, thermal: int, outdoor: int) -> bytes:
    return (
        b"\x04\x00"
        + kwh.to_bytes(4, "big")
        + thermal.to_bytes(4, "big")
        + bytes([outdoor])
    )


def test_real_frame_counters_are_valid() -> None:
    """Sanity: the unmutated frame decodes 6463 / 24312 kWh."""
    out, _ = apply_atw_unit_params(NORDIS_UNIT_PARAMS_0X10, _status(False), ITSSensors())
    assert out.total_kwh == 6463
    assert out.total_thermal_kwh == 24312


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, False),
        (1, True),
        (6463, True),
        (0xFFFFFFFE, True),
        (0xFFFFFFFF, False),
    ],
)
def test_valid_energy_counter_bounds(value: int, expected: bool) -> None:
    assert valid_energy_counter(value) is expected


@pytest.mark.parametrize("sentinel", _SENTINELS.values(), ids=_SENTINELS.keys())
@pytest.mark.parametrize("prior", [True, False], ids=["prior", "no_prior"])
def test_unit_params_overlay_ignores_sentinel_counters(
    sentinel: bytes, prior: bool
) -> None:
    status = _status(prior)
    sensors = ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY)

    out_status, out_sensors = apply_atw_unit_params(
        _with_counters(sentinel, sentinel), status, sensors
    )

    assert out_status.total_kwh == (_PRIOR_KWH if prior else None)
    assert out_status.total_thermal_kwh == (_PRIOR_THERMAL if prior else None)
    # Every other 0x10 field is still applied.
    assert out_status.comp_frq == 0
    assert out_status.comp_running is False
    assert out_status.exv_drg == 480
    assert out_status.pressure_h == 1220
    assert out_sensors.t4_temp == 14.0
    assert out_sensors.water_flow == 61


@pytest.mark.parametrize("sentinel", _SENTINELS.values(), ids=_SENTINELS.keys())
def test_unit_params_overlay_filters_each_counter_independently(sentinel: bytes) -> None:
    status = _status(True)

    only_kwh_bad, _ = apply_atw_unit_params(
        _with_counters(kwh=sentinel), status, ITSSensors()
    )
    assert only_kwh_bad.total_kwh == _PRIOR_KWH
    assert only_kwh_bad.total_thermal_kwh == 24312

    only_thermal_bad, _ = apply_atw_unit_params(
        _with_counters(thermal=sentinel), status, ITSSensors()
    )
    assert only_thermal_bad.total_kwh == 6463
    assert only_thermal_bad.total_thermal_kwh == _PRIOR_THERMAL


@pytest.mark.parametrize("sentinel", _SENTINELS.values(), ids=_SENTINELS.keys())
@pytest.mark.parametrize("prior", [True, False], ids=["prior", "no_prior"])
def test_build_snapshot_poll_ignores_sentinel_0x10_counters(
    sentinel: bytes, prior: bool
) -> None:
    state = build_snapshot(
        get_adapter(ATW_SN8),
        _status(prior),
        ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY),
        {0x10: _with_counters(sentinel, sentinel)},
    )
    assert state.status is not None
    assert state.status.total_kwh == (_PRIOR_KWH if prior else None)
    assert state.status.total_thermal_kwh == (_PRIOR_THERMAL if prior else None)
    assert state.status.pressure_h == 1220


@pytest.mark.parametrize("sentinel", _SENTINELS.values(), ids=_SENTINELS.keys())
@pytest.mark.parametrize("prior", [True, False], ids=["prior", "no_prior"])
def test_build_snapshot_push_ignores_sentinel_retained_0x10_counters(
    sentinel: bytes, prior: bool
) -> None:
    state = build_snapshot(
        get_adapter(ATW_SN8),
        _status(prior),
        ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY),
        {0x10: _with_counters(sentinel, sentinel)},
        retained_subtypes=True,
    )
    assert state.status is not None
    assert state.status.total_kwh == (_PRIOR_KWH if prior else None)
    assert state.status.total_thermal_kwh == (_PRIOR_THERMAL if prior else None)


@pytest.mark.parametrize("sentinel", _SENTINELS.values(), ids=_SENTINELS.keys())
def test_build_snapshot_push_sentinel_0x10_keeps_valid_0x04_counters(
    sentinel: bytes,
) -> None:
    """A retained sentinel 0x10 must not clobber a valid 0x04 / energy_notify value.

    The merged ``energy_notify`` carries a valid kWh but a sentinel thermal
    counter (``merge_atw_energy_notify`` had no earlier valid thermal value),
    so the thermal counter must come from the status the push builds on.
    """
    status = dataclasses.replace(
        _status(False), total_kwh=14100, total_thermal_kwh=32181
    )
    state = build_snapshot(
        get_adapter(ATW_SN8),
        status,
        ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY),
        {0x10: _with_counters(sentinel, sentinel)},
        retained_subtypes=True,
        energy_notify=_energy_frame(14126, 0xFFFFFFFF, 0xFB),
    )
    assert state.status is not None
    assert state.status.total_kwh == 14126
    assert state.status.total_thermal_kwh == 32181


def test_build_snapshot_valid_0x10_counters_still_apply_in_both_modes() -> None:
    frame = _with_counters(kwh=(7000).to_bytes(4, "big"), thermal=(25000).to_bytes(4, "big"))
    for retained in (False, True):
        state = build_snapshot(
            get_adapter(ATW_SN8),
            _status(True),
            ITSSensors(raw_body=ATW_PLACEHOLDER_SENSORS_BODY),
            {0x10: frame},
            retained_subtypes=retained,
        )
        assert state.status is not None
        assert state.status.total_kwh == 7000
        assert state.status.total_thermal_kwh == 25000
