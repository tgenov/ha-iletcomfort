"""Tests for the iLetComfort climate entity (profile-aware current_temperature).

The climate card's ``current_temperature`` is profile-aware (issues #22, #12):
- STANDARD reads ``sensors.twin_temp`` (the real water-inlet reading), unchanged.
- ATW / AQUAPURA have no real inlet reading; the meaningful "current" value is
  the DHW tank temperature, which the model profiles surface on ``th_temp``. The
  climate entity returns ``th_temp`` for those profiles so the card still shows a
  useful number while the "Water Inlet Temperature" sensor stays honest.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.climate import ILetComfortClimate
from homeassistant.const import ATTR_TEMPERATURE

from custom_components.iletcomfort.model_profiles import (
    ATW_SN8,
    AQUAPURA_SN8,
    KJRH120L_SN8,
)


def _climate(sn8: str | None, sensors: ITSSensors, status: ITSStatus | None = None):
    """Build a climate entity backed by a stub coordinator."""
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.sn8 = sn8
    coordinator.appliance_meta = {"sn8": sn8} if sn8 else None
    coordinator.data = {"status": status, "sensors": sensors}
    coordinator.async_set_device = AsyncMock()
    return ILetComfortClimate(coordinator)


def test_current_temperature_standard_reads_twin_temp():
    """STANDARD (unknown/None sn8) reads twin_temp, byte-for-byte unchanged."""
    sensors = ITSSensors(twin_temp=21.0, th_temp=99)
    entity = _climate(None, sensors)
    assert entity.current_temperature == 21.0


def test_current_temperature_atw_reads_th_temp():
    """ATW returns the DHW tank temp (th_temp), not the (absent) inlet."""
    sensors = ITSSensors(twin_temp=None, th_temp=46.0)
    entity = _climate(ATW_SN8, sensors)
    assert entity.current_temperature == 46.0


def test_current_temperature_aquapura_reads_th_temp():
    """AQUAPURA returns the tank temp (th_temp) surfaced by the profile."""
    sensors = ITSSensors(twin_temp=0.0, th_temp=40.0)
    entity = _climate(AQUAPURA_SN8, sensors)
    assert entity.current_temperature == 40.0


def test_current_temperature_none_when_no_sensors():
    """No sensor data → None regardless of profile."""
    entity = _climate(ATW_SN8, None)
    assert entity.current_temperature is None


def test_water_inlet_attribute_still_sourced_from_twin_temp():
    """The water_inlet extra-state attribute stays sourced from twin_temp.

    For ATW/AQUAPURA twin_temp is None/0 (no real reading), so the attribute is
    correctly absent rather than relabeled.
    """
    # STANDARD: twin_temp present → attribute present.
    std = _climate(None, ITSSensors(twin_temp=21.0))
    assert std.extra_state_attributes.get("water_inlet") == 21.0

    # ATW: twin_temp absent → attribute absent (not relabeled to the tank temp).
    atw = _climate(ATW_SN8, ITSSensors(twin_temp=None, th_temp=46.0))
    assert "water_inlet" not in atw.extra_state_attributes


# --- KJRH-120L SET path clamping (issue #35) ------------------------------
# The KJRH-120L is a DHW heat-pump water heater; its setpoint range (captured
# 49/60/65 °C) sits above the air-side HEAT range, so min/max are profile-aware.
# The climate entity clamps the requested setpoint to its own min/max before
# handing it to the coordinator (which forwards sn8 so the client sends the
# short write command).


async def test_kjrh120l_set_temperature_within_range_passes_through():
    """A KJRH setpoint inside the DHW range is forwarded unchanged (as int)."""
    entity = _climate(KJRH120L_SN8, ITSSensors(), ITSStatus(mode=1))
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 60})
    entity.coordinator.async_set_device.assert_awaited_once_with(temperature=60)


async def test_kjrh120l_set_temperature_clamped_to_max():
    """A request above the KJRH max is clamped down to max_temp before sending."""
    entity = _climate(KJRH120L_SN8, ITSSensors(), ITSStatus(mode=1))
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 99})
    sent = entity.coordinator.async_set_device.call_args.kwargs["temperature"]
    assert sent == int(entity.max_temp)


async def test_kjrh120l_set_temperature_clamped_to_min():
    """A request below the KJRH min is clamped up to min_temp before sending."""
    entity = _climate(KJRH120L_SN8, ITSSensors(), ITSStatus(mode=1))
    await entity.async_set_temperature(**{ATTR_TEMPERATURE: 1})
    sent = entity.coordinator.async_set_device.call_args.kwargs["temperature"]
    assert sent == int(entity.min_temp)


def test_kjrh120l_min_max_cover_captured_setpoints():
    """KJRH min/max must allow the real captured setpoints (49–65 °C)."""
    entity = _climate(KJRH120L_SN8, ITSSensors(), ITSStatus(mode=1))
    assert entity.min_temp <= 49
    assert entity.max_temp >= 65


@pytest.mark.parametrize("sn8", [None, ATW_SN8, AQUAPURA_SN8])
def test_non_kjrh_min_max_unchanged(sn8):
    """STANDARD/ATW/AQUAPURA keep the legacy HEAT min/max (10–40)."""
    entity = _climate(sn8, ITSSensors(), ITSStatus(mode=1))  # mode 1 → Heat
    assert entity.min_temp == 10.0
    assert entity.max_temp == 40.0
