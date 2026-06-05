"""Tests for the iLetComfort climate entity (profile-aware current_temperature).

The climate card's ``current_temperature`` is profile-aware (issues #22, #12):
- STANDARD reads ``sensors.twin_temp`` (the real water-inlet reading), unchanged.
- ATW / AQUAPURA have no real inlet reading; the meaningful "current" value is
  the DHW tank temperature, which the model profiles surface on ``th_temp``. The
  climate entity returns ``th_temp`` for those profiles so the card still shows a
  useful number while the "Water Inlet Temperature" sensor stays honest.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.iletcomfort.api import ITSSensors, ITSStatus
from custom_components.iletcomfort.climate import ILetComfortClimate
from custom_components.iletcomfort.model_profiles import ATW_SN8, AQUAPURA_SN8


def _climate(sn8: str | None, sensors: ITSSensors, status: ITSStatus | None = None):
    """Build a climate entity backed by a stub coordinator."""
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.sn8 = sn8
    coordinator.appliance_meta = {"sn8": sn8} if sn8 else None
    coordinator.data = {"status": status, "sensors": sensors}
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
