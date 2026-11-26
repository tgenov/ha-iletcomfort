"""Tests for DeviceCapabilities and async_add_when_capable (#171)."""

from __future__ import annotations

from unittest.mock import MagicMock

from custom_components.iletcomfort.capabilities import DeviceCapabilities, ProbeState
from custom_components.iletcomfort.entity import async_add_when_capable


def test_probe_state_lifecycle() -> None:
    probe = ProbeState()
    assert probe.supported is None
    assert probe.should_probe is True

    probe.mark_unsupported("code=1214")
    assert probe.supported is False
    assert probe.error == "code=1214"
    assert probe.should_probe is False

    probe.mark_supported()
    assert probe.supported is True
    assert probe.error is None
    assert probe.should_probe is True

    probe.reset()
    assert probe.supported is None
    assert probe.error is None


def test_device_capabilities_reset_and_diagnostics() -> None:
    caps = DeviceCapabilities()
    caps.atw_unit_params.mark_supported()
    caps.atw_hmi_params.mark_unsupported("code=1214")

    assert caps.probes_as_dict() == {
        "0x10_unit_params": {"supported": True, "error": None},
        "0x0a_hmi_params": {"supported": False, "error": "code=1214"},
    }

    caps.reset_probes()
    assert caps.probes_as_dict() == {
        "0x10_unit_params": {"supported": None, "error": None},
        "0x0a_hmi_params": {"supported": None, "error": None},
    }


def test_async_add_when_capable_skips_listener_when_cannot_become_capable() -> None:
    coord = MagicMock()
    entry = MagicMock()
    added: list[object] = []

    async_add_when_capable(
        coord,
        entry,
        added.extend,
        is_capable=lambda: False,
        can_become_capable=lambda: False,
        build=lambda: [object()],
    )

    assert added == []
    coord.async_add_listener.assert_not_called()
    entry.async_on_unload.assert_not_called()


def test_async_add_when_capable_unsubscribes_after_late_discovery() -> None:
    coord = MagicMock()
    unsub = MagicMock()
    coord.async_add_listener.return_value = unsub
    entry = MagicMock()
    added: list[object] = []
    ready = False

    async_add_when_capable(
        coord,
        entry,
        added.extend,
        is_capable=lambda: ready,
        can_become_capable=lambda: True,
        build=lambda: ["entity"],
    )

    assert added == []
    listener = coord.async_add_listener.call_args.args[0]

    # Update while still not ready does nothing.
    listener()
    assert added == []
    unsub.assert_not_called()

    # Once ready, entities are added once and the listener unsubscribes.
    ready = True
    listener()
    assert added == ["entity"]
    unsub.assert_called_once()

    # Subsequent calls are no-ops.
    listener()
    assert added == ["entity"]


def test_async_add_when_capable_unsubscribes_when_profile_becomes_incompatible() -> None:
    coord = MagicMock()
    unsub = MagicMock()
    coord.async_add_listener.return_value = unsub
    entry = MagicMock()
    added: list[object] = []
    compatible = True

    async_add_when_capable(
        coord,
        entry,
        added.extend,
        is_capable=lambda: False,
        can_become_capable=lambda: compatible,
        build=lambda: ["entity"],
    )

    listener = coord.async_add_listener.call_args.args[0]
    compatible = False
    listener()

    assert added == []
    unsub.assert_called_once()
    # Unload callback is idempotent after listener already unsubscribed.
    unload_cb = entry.async_on_unload.call_args.args[0]
    unload_cb()
    unsub.assert_called_once()
