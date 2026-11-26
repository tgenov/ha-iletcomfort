"""Runtime capability & probe tracking for iLetComfort heat pumps (#171)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProbeState:
    """Session-scoped state of an optional C3 subtype probe.

    ``supported`` is ``None`` until probed, ``True`` once a valid response is
    received, and ``False`` after the first ``ApiError`` (e.g. ``code=1214``),
    preventing repeated cloud rejections on unsupported firmwares.
    """

    supported: bool | None = None
    error: str | None = None

    @property
    def should_probe(self) -> bool:
        """Return True unless this probe has already been disabled."""
        return self.supported is not False

    def mark_supported(self) -> None:
        self.supported = True
        self.error = None

    def mark_unsupported(self, error: str) -> None:
        self.supported = False
        self.error = error

    def reset(self) -> None:
        self.supported = None
        self.error = None

    def as_dict(self) -> dict[str, Any]:
        return {"supported": self.supported, "error": self.error}


@dataclass
class DeviceCapabilities:
    """Centralized capability & probe state owned by the coordinator."""

    atw_unit_params: ProbeState = field(default_factory=ProbeState)
    atw_hmi_params: ProbeState = field(default_factory=ProbeState)
    atw_silent: ProbeState = field(default_factory=ProbeState)
    atw_eco: ProbeState = field(default_factory=ProbeState)

    def reset_probes(self) -> None:
        """Reset all session-scoped probes (e.g. when the resolved profile changes)."""
        self.atw_unit_params.reset()
        self.atw_hmi_params.reset()
        self.atw_silent.reset()
        self.atw_eco.reset()

    def probes_as_dict(self) -> dict[str, dict[str, Any]]:
        """Return JSON-serializable probe diagnostics."""
        return {
            "0x10_unit_params": self.atw_unit_params.as_dict(),
            "0x0a_hmi_params": self.atw_hmi_params.as_dict(),
            "0x05_silent": self.atw_silent.as_dict(),
            "0x07_eco": self.atw_eco.as_dict(),
        }
