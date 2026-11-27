"""Pure declarative read planning shared by every protocol dialect.

Mirrors ``protocols/write.py``: a dialect turns a status or sensors read into a
``ReadPlan`` without performing any I/O. The transport/client executes the
plan's wire queries via ``run_read_plan(plan, send)`` and hands the raw hex
replies back to the plan's pure ``decode`` step.

Nothing here imports Home Assistant or performs network I/O.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Generic, TypeVar

from ..api import (
    SENSORS_MIN_BODY_LEN,
    STATUS_MIN_BODY_LEN,
    ITSSensors,
    ITSStatus,
    decode_its_sensors,
    decode_its_status,
)
from ..c3_frame import extract_c3_body, parse_hex_response
from ..cloud import ApiError

if TYPE_CHECKING:
    from .base import ProtocolAdapter

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass(frozen=True)
class ReadPlan(Generic[T]):
    """A planned read: ordered wire hex queries followed by a pure decode step."""

    commands: tuple[str, ...]
    decode: Callable[[tuple[str, ...]], T]
    description: str = ""

    def __post_init__(self) -> None:
        if not self.commands:
            raise ValueError("ReadPlan requires at least one query command")


def run_read_plan(plan: ReadPlan[T], send: Callable[[str], str]) -> T:
    """Execute every query in ``plan`` over ``send`` and return the decoded value."""
    replies = tuple(send(command) for command in plan.commands)
    return plan.decode(replies)


def c3_status_read_plan(adapter: ProtocolAdapter) -> ReadPlan[ITSStatus]:
    """Standard single-query C3 subtype 0x01 status read plan."""
    command = adapter.build_query(0x01)

    def _decode(replies: tuple[str, ...]) -> ITSStatus:
        response_hex = replies[0]
        _LOGGER.debug("query_status raw response: %s", response_hex)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        if len(body) < STATUS_MIN_BODY_LEN:
            raise ApiError(
                f"Status query returned a truncated frame "
                f"({len(body)} body bytes); device may be offline or "
                f"unsupported. Raw: {response_hex}"
            )
        return adapter.decode_status(decode_its_status(body))

    return ReadPlan(commands=(command,), decode=_decode, description="c3_status_0x01")


def c3_sensors_read_plan(adapter: ProtocolAdapter) -> ReadPlan[ITSSensors]:
    """Standard single-query C3 subtype 0x02 sensors read plan."""
    command = adapter.build_query(0x02)

    def _decode(replies: tuple[str, ...]) -> ITSSensors:
        response_hex = replies[0]
        _LOGGER.debug("query_sensors raw response: %s", response_hex)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        if len(body) < SENSORS_MIN_BODY_LEN:
            raise ApiError(
                f"Sensors query returned a truncated frame "
                f"({len(body)} body bytes); device may be offline or "
                f"unsupported. Raw: {response_hex}"
            )
        return decode_its_sensors(body)

    return ReadPlan(commands=(command,), decode=_decode, description="c3_sensors_0x02")


__all__ = [
    "ReadPlan",
    "c3_sensors_read_plan",
    "c3_status_read_plan",
    "run_read_plan",
]
