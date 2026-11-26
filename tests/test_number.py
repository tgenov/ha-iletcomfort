"""Tests for KJRH-120L dual-variant DHW setpoint control (issue #5)."""

from unittest.mock import MagicMock

from custom_components.iletcomfort.api import ITSSensors
from custom_components.iletcomfort.model_profiles import (
    KJRH120L_SN8,
    decode_kjrh120l_status,
)
from custom_components.iletcomfort.number import (
    ILetComfortKjrh120lDhwSetpoint,
    _is_kjrh120l_dual,
)
from tests.write_helpers import bind_write_path, sent_commands


def _entity() -> ILetComfortKjrh120lDhwSetpoint:
    body = bytearray(20)
    body[0] = 0x01
    body[8] = body[9] = body[10] = 0x01
    body[12] = 19
    body[15] = 51
    coordinator = MagicMock()
    coordinator.appliance_code = "APPL1"
    coordinator.sn8 = KJRH120L_SN8
    coordinator.appliance_meta = {"sn8": KJRH120L_SN8}
    coordinator.data = {
        "status": decode_kjrh120l_status(body),
        "sensors": ITSSensors(),
    }
    coordinator.c3_basic_controllable = None
    return ILetComfortKjrh120lDhwSetpoint(bind_write_path(coordinator))


async def test_dhw_number_reads_and_writes_confirmed_dhw_setpoint():
    entity = _entity()

    assert entity.native_value == 51.0
    await entity.async_set_native_value(55)

    assert sent_commands(entity.coordinator) == ["00070137ff"]


async def test_dhw_number_clamps_to_validated_range():
    entity = _entity()

    await entity.async_set_native_value(99)

    assert sent_commands(entity.coordinator) == ["0007013cff"]


def test_pure_dhw_kjrh_does_not_create_separate_number():
    entity = _entity()
    body = bytearray(entity.coordinator.data["status"].raw_body)
    body[8] = body[9] = 0
    entity.coordinator.data["status"] = decode_kjrh120l_status(body)

    assert not _is_kjrh120l_dual(entity.coordinator)
