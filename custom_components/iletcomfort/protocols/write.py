"""Pure write planning shared by every protocol dialect (#113).

A dialect turns one typed ``HeatPumpCommand`` into a ``WritePlan`` without any
I/O. A plan is either *static* (the full wire command is known at planning
time, e.g. KJRH-120L short writes) or *read-modify-write*: it names the query
the transport must run **immediately before** the SET and a pure ``encode``
step that turns that fresh reply body into the SET hex. The coordinator runs
the read and the SET inside one write-lock transaction (``run_write_plan``),
so concurrent writes cannot undo each other and a schedule copied from a
fresh 0x05/0x07 reply is never stale.

Refusals:

* ``WriteRefused`` — an HA-side gating decision (opt-in disabled, status not
  reported yet, intent unsupported by the selected entity semantics). The
  coordinator surfaces its message verbatim.
* ``ApiError`` / ``ValueError`` (including ``c3_basic.UnsupportedC3Write``) —
  the dialect/device write envelope rejected the command. The coordinator
  surfaces these as ``"Heat pump command failed: ..."``, exactly like errors
  raised by the transport.

Nothing here imports Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..api import ITSStatus
from ..c3_frame import extract_c3_body, parse_hex_response
from ..cloud import ApiError
from .views import ViewContext

# Messages kept identical to the pre-#113 coordinator/client refusals.
C3_CONTROL_DISABLED = "C3 basic control is not enabled or not supported for this unit"
KJRH120L_ONLY_ZONE1 = (
    "Zone 1 setpoint, run mode and Zone 1 power writes are only "
    "supported on the KJRH-120L (sn8 17100003)"
)
STATUS_NOT_REPORTED = "The heat pump has not reported its status yet"


class WriteRefused(ValueError):
    """The dialect refused a command before anything was sent.

    The message is user-facing and surfaced verbatim by the coordinator.
    """


@dataclass(frozen=True)
class WriteContext:
    """Everything a dialect needs to plan a write, captured before the lock.

    ``status`` is the last decoded status snapshot (``coordinator.data``) and is
    only used to *route* a command (e.g. water vs room target) and for
    HA-side gating. Read-modify-write plans re-validate against the fresh
    reply in ``encode``. ``kjrh120l_zone1_latched`` selects the dual KJRH-120L
    climate view (and so its bounds); no write is gated on it today (the
    Zone 1 entities are, which matches pre-#113 behavior).
    """

    status: ITSStatus | None = None
    last_on_state: tuple[int, int] | None = None
    c3_basic_control: bool = False
    kjrh120l_zone1_latched: bool = False

    @property
    def view_context(self) -> ViewContext:
        """The projection context, so a plan clamps to the bounds entities show."""
        return ViewContext(
            c3_basic_control=self.c3_basic_control,
            kjrh120l_zone1_latched=self.kjrh120l_zone1_latched,
        )


@dataclass(frozen=True)
class FreshRead:
    """A query the transport runs immediately before a read-modify-write SET.

    ``command`` is the dialect's wire query. When ``require_echo`` is set, the
    reply body must start with ``subtype`` or the write is aborted before any
    SET is sent.
    """

    subtype: int
    command: str
    require_echo: bool = True


@dataclass(frozen=True)
class WritePlan:
    """A planned write: either a static command, or fresh read → pure encode."""

    command: str | None = None
    fresh_read: FreshRead | None = None
    encode: Callable[[bytes], str] | None = None
    description: str = ""
    # Optional check of the SET reply, ``verify(sent_hex, response_hex)``; it
    # raises ``ApiError`` when the device did not apply the write (e.g. an
    # echo of the command on table-based controllers).
    verify: Callable[[str, str], None] | None = None

    def __post_init__(self) -> None:
        if self.command is not None:
            valid = self.fresh_read is None and self.encode is None
        else:
            valid = self.fresh_read is not None and self.encode is not None
        if not valid:
            raise ValueError(
                "WritePlan needs either a static command or a fresh_read + encode"
            )

    @classmethod
    def static(
        cls,
        command: str,
        description: str = "",
        *,
        verify: Callable[[str, str], None] | None = None,
    ) -> WritePlan:
        """Return a plan whose wire command is fully known at planning time."""
        return cls(command=command, description=description, verify=verify)

    @classmethod
    def read_modify_write(
        cls,
        fresh_read: FreshRead,
        encode: Callable[[bytes], str],
        description: str = "",
    ) -> WritePlan:
        """Return a plan encoded from a fresh reply to ``fresh_read``."""
        return cls(fresh_read=fresh_read, encode=encode, description=description)

    def resolve(self, fresh_body: bytes | None = None) -> str:
        """Return the wire SET hex, encoding from ``fresh_body`` when required."""
        if self.command is not None:
            return self.command
        assert self.encode is not None
        if fresh_body is None:
            raise ValueError("This write needs a fresh device read before encoding")
        return self.encode(fresh_body)


def read_fresh_body(fresh_read: FreshRead, response_hex: str) -> bytes:
    """Extract and validate the reply body for a plan's fresh read."""
    _body_type, body = extract_c3_body(parse_hex_response(response_hex))
    if fresh_read.require_echo and (not body or body[0] != fresh_read.subtype):
        raise ApiError(
            f"Unexpected response to C3 query 0x{fresh_read.subtype:02x}: "
            f"{response_hex}"
        )
    return bytes(body)


def run_write_plan(plan: WritePlan, send: Callable[[str], str]) -> dict[str, Any]:
    """Execute ``plan`` over ``send`` (hex in → response hex out): read → encode → send.

    The caller holds the per-appliance write lock for the whole call. Any
    refusal raised by ``encode`` happens after the read and before the SET,
    so an invalid or unsafe change never reaches the device.
    """
    fresh_body: bytes | None = None
    if plan.fresh_read is not None:
        fresh_body = read_fresh_body(plan.fresh_read, send(plan.fresh_read.command))
    command = plan.resolve(fresh_body)
    response_hex = send(command)
    if plan.verify is not None:
        plan.verify(command, response_hex)
    return {"sent": command, "response": response_hex}


__all__ = [
    "C3_CONTROL_DISABLED",
    "KJRH120L_ONLY_ZONE1",
    "STATUS_NOT_REPORTED",
    "FreshRead",
    "WriteContext",
    "WritePlan",
    "WriteRefused",
    "read_fresh_body",
    "run_write_plan",
]
