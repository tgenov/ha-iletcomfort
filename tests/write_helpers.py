"""Helpers for entity tests that drive the real write path on a stub coordinator.

``bind_write_path`` gives a ``MagicMock`` coordinator the real
``async_send_command`` (phone-app guard + dialect ``plan_write``) and replaces
only the transaction (``async_execute_plan``) with an ``AsyncMock``. Tests then
assert on the planned wire bytes with ``sent_commands`` instead of on internal
keyword arguments (#113).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from custom_components.iletcomfort.const import OPERATION_MODE_HA_PRIMARY
from custom_components.iletcomfort.coordinator import ILetComfortCoordinator


def bind_write_path(
    coordinator: MagicMock,
    *,
    c3_basic_control: bool = False,
    kjrh120l_zone1_latched: bool = False,
    last_on_state: tuple[int, int] | None = None,
) -> MagicMock:
    """Wire ``coordinator`` so writes are planned for real but never executed."""
    coordinator._operation_mode = OPERATION_MODE_HA_PRIMARY
    coordinator._last_on_state = last_on_state
    coordinator._c3_basic_control = c3_basic_control
    coordinator.kjrh120l_zone1_capable = kjrh120l_zone1_latched
    coordinator.async_execute_plan = AsyncMock()
    for name in ("async_send_command", "_require_writable", "_write_context"):
        setattr(
            coordinator, name, getattr(ILetComfortCoordinator, name).__get__(coordinator)
        )
    return coordinator


def sent_commands(
    coordinator: MagicMock, fresh: bytes | dict[int, bytes] | None = None
) -> list[str]:
    """Return the SET wire command of every executed plan.

    Static plans (KJRH-120L) need no ``fresh`` body. Read-modify-write plans
    are encoded from ``fresh`` as if the device had just returned it; pass a
    ``{subtype: body}`` mapping when plans read different subtypes.
    """
    commands = []
    for call in coordinator.async_execute_plan.await_args_list:
        plan = call.args[0]
        body = None
        if plan.fresh_read is not None:
            body = fresh.get(plan.fresh_read.subtype) if isinstance(fresh, dict) else fresh
        commands.append(plan.resolve(body))
    return commands
