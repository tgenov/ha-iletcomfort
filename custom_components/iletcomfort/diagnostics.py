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

from .api import SENSOR_DISCONNECTED, TEMP_OFFSET, mask_identifier
from .const import CONF_APPLIANCE_CODE, DOMAIN
from .coordinator import ILetComfortCoordinator

# Account credentials are the only secrets in the entry. Region is kept as-is.
# appliance_code is a device-unique identifier, so it is suffix-masked (see
# mask_identifier) rather than left intact — sn8 already covers model
# correlation, so the maintainer loses nothing by not seeing the full code.
TO_REDACT = {CONF_EMAIL, CONF_PASSWORD}

# Appliance metadata is surfaced to help maintainers identify the device class
# for model-specific decoding (issue #22). Redact the account/device-identifying
# fields; keep applianceType/modelNumber/sn8/online/etc. which are the
# discriminators a maintainer needs.
APPLIANCE_TO_REDACT = {"owner", "sn", "name"}


def _sensors_temperature_scan(raw_body: bytes) -> dict[int, float | None]:
    """Decode every byte of the sensors body as a ``_temp_offset`` temperature.

    Returns a ``{body_index: decoded_celsius}`` mapping.  ``body[0]`` is the
    subtype byte; data offsets begin at ``body[1]`` (``d+0`` in the decoder).
    A ``None`` value means the byte encodes ``SENSOR_DISCONNECTED``.

    When a maintainer receives a diagnostics file for a device model whose
    water-temperature reading is wrong (e.g. always 0 °C), they can cross-
    reference this scan against the real value shown in the official app to
    quickly identify which byte position carries that temperature.
    """
    return {
        i: (
            None
            if (raw_byte - TEMP_OFFSET) == SENSOR_DISCONNECTED
            else float(raw_byte - TEMP_OFFSET)
        )
        for i, raw_byte in enumerate(raw_body)
    }


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

    # async_redact_data leaves appliance_code intact (it is not a credential);
    # suffix-mask the device-unique id so a shared diagnostics file can be
    # loosely correlated without exposing the full identifier.
    entry_data = async_redact_data(dict(entry.data), TO_REDACT)
    if CONF_APPLIANCE_CODE in entry_data:
        entry_data[CONF_APPLIANCE_CODE] = mask_identifier(
            entry_data[CONF_APPLIANCE_CODE]
        )

    appliance = (
        async_redact_data(dict(coordinator.appliance_meta), APPLIANCE_TO_REDACT)
        if coordinator.appliance_meta is not None
        else None
    )
    if appliance is not None and "applianceCode" in appliance:
        appliance["applianceCode"] = mask_identifier(appliance["applianceCode"])

    return {
        "entry": {
            "version": entry.version,
            "data": entry_data,
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
        "appliance": appliance,
        "status": _serialize_frame(data.get("status")),
        "sensors": _serialize_frame(data.get("sensors")),
        "sensors_temperature_scan": _sensors_temperature_scan(
            sensors_obj.raw_body
            if (sensors_obj := data.get("sensors")) is not None
            else b""
        ),
    }
