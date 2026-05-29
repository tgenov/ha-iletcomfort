"""Diagnostics support for the iLetComfort integration.

Produces a one-click, credential-redacted snapshot of everything a maintainer
needs to debug a wrong/empty-entity report: the device's raw C3 frames (the same
bytes the debug log emits as ``STATUS RAW:`` / ``SENSORS RAW:``), the fully
decoded status/sensors fields, and the entry/coordinator context. See
``docs/TROUBLESHOOTING.md``.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import ILetComfortCoordinator

# Account credentials are the only secrets in the entry. Region and
# appliance_code are kept: appliance_code is already shown to the user in the
# offline Repair card, and the maintainer needs it to correlate the model.
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD}


def _format_hex(raw: bytes) -> str:
    """Render raw frame bytes as comma-separated hex, matching the debug log.

    Mirrors the formatting in ``coordinator.py`` so a diagnostics dump and a
    ``STATUS RAW:`` / ``SENSORS RAW:`` log line are byte-for-byte comparable.
    """
    return ",".join(f"{b:02x}" for b in raw)


def _serialize_frame(obj: Any) -> Any:
    """Convert a decoded status/sensors dataclass into a JSON-safe dict.

    ``raw_body`` is bytes (not JSON-serializable), so it is replaced with its
    hex rendering; every other field is a primitive already.
    """
    if not is_dataclass(obj) or isinstance(obj, type):
        return obj
    data = asdict(obj)
    raw = data.pop("raw_body", b"")
    data["raw_body"] = _format_hex(raw)
    return data


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator: ILetComfortCoordinator = hass.data[DOMAIN][entry.entry_id]
    data = coordinator.data or {}

    return {
        "entry": {
            "version": entry.version,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "update_interval_seconds": (
                coordinator.update_interval.total_seconds()
                if coordinator.update_interval
                else None
            ),
            "status_degraded": coordinator._status_degraded,
            "sensors_degraded": coordinator._sensors_degraded,
            "repair_issued": coordinator._repair_issued,
        },
        "status": _serialize_frame(data.get("status")),
        "sensors": _serialize_frame(data.get("sensors")),
    }
