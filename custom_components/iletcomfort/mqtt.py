"""MQTT real-time status push for iLetComfort C3 heat pumps (issue #55).

The vendor's cloud pushes decoded status over an AWS IoT MQTT broker. A client
certificate minted via :meth:`ILetComfortClient.create_app_cert` authenticates
**independently of the account session**, so push keeps flowing while the phone
app holds the single active account login (the "login war"). This is opt-in and
disabled by default. In phone-app coexistence mode, a push outage marks entities
unavailable rather than falling back to account polling and evicting the app.

Hardware-confirmed shape (see issue #55):

* C3 devices publish on ``<region>/midea/dev/<applianceCode>``.
* The payload is JSON: ``{"messageType": "status", "data": {"commandHex": ...}}``
  where ``commandHex`` is a standard C3 subtype-``0x01`` frame — so it decodes
  through the same pipeline as a polled status response, no new decoder.
* There is **no heartbeat** on the C3 topic, so liveness is derived from the
  MQTT connection state, not from a heartbeat message.

The broker has no per-appliance topic ACL, so this module only ever subscribes
to the configured appliance's own topics — never a wildcard.
"""

from __future__ import annotations

import json
import logging
import ssl
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt

from .api import (
    ITSStatus,
    decode_its_status,
    extract_c3_body,
    mask_identifier,
    parse_hex_response,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import ILetComfortClient

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[ITSStatus], None]
ConnectedCallback = Callable[[bool], None]


def push_topics(region: str, appliance_code: str) -> list[str]:
    """Return the push topics to subscribe to for one appliance.

    Scoped strictly to ``appliance_code`` — the broker grants wildcard
    subscriptions that would leak other appliances' data, so we never use one.
    ``midea/dev`` is the topic C3 heat pumps publish status on (hardware-
    confirmed, issue #55); the ``<region>_<code>`` shape #48 described carries
    other appliance families and was silent for a C3 unit in testing, so it is
    not subscribed.
    """
    return [f"{region}/midea/dev/{appliance_code}"]


def decode_push_payload(payload: str) -> ITSStatus | None:
    """Decode a ``dev`` push payload into an :class:`ITSStatus`, or None.

    Returns None (rather than raising) for anything that isn't a decodable C3
    status message, so a malformed or non-status push can't break the listener.
    Profile-specific re-decoding (ATW/KJRH-120L/AQUAPURA) is applied by the
    caller, which knows the appliance's sn8.
    """
    try:
        message = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(message, dict):
        return None
    if message.get("messageType") not in (None, "status"):
        return None
    data = message.get("data")
    if not isinstance(data, dict):
        return None
    command_hex = data.get("commandHex")
    if not command_hex or not isinstance(command_hex, str):
        return None
    try:
        raw = parse_hex_response(command_hex)
        subtype, body = extract_c3_body(raw)
    except (ValueError, IndexError):
        return None
    if subtype != 0x01:
        # Only status frames update entity state here; other subtypes are
        # ignored (the push topic carries status frames only).
        return None
    try:
        return decode_its_status(body)
    except (ValueError, IndexError):
        return None


class ILetComfortPushClient:
    """Manages an MQTT push subscription for a single appliance."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: ILetComfortClient,
        *,
        region: str,
        appliance_code: str,
        on_status: StatusCallback,
        on_connected_change: ConnectedCallback,
    ) -> None:
        self._hass = hass
        self._client = client
        self._region = region
        self._appliance_code = appliance_code
        self._on_status = on_status
        self._on_connected_change = on_connected_change
        self._mqtt: mqtt.Client | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_start(self) -> None:
        """Mint a certificate, then connect and subscribe (off the event loop)."""
        cert = await self._hass.async_add_executor_job(self._client.create_app_cert)
        await self._hass.async_add_executor_job(self._connect, cert)

    def _connect(self, cert) -> None:
        # Materialize the cert/key into a private 0700 temp dir; paho's TLS
        # wants file paths. The dir is removed on stop. Never logged.
        self._tmpdir = tempfile.TemporaryDirectory(prefix="iletcomfort_mqtt_")
        base = Path(self._tmpdir.name)
        cert_path = base / "client.crt"
        key_path = base / "client.key"
        cert_path.write_text(cert.certificate_pem, encoding="utf-8")
        key_path.write_text(cert.private_key, encoding="utf-8")
        cert_path.chmod(0o600)
        key_path.chmod(0o600)

        c = mqtt.Client()
        c.tls_set(
            certfile=str(cert_path),
            keyfile=str(key_path),
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLSv1_2,
        )
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self._mqtt = c

        from .const import MQTT_DEFAULT_PORT, MQTT_KEEPALIVE

        _LOGGER.debug(
            "Connecting MQTT push for appliance %s to %s:%s",
            mask_identifier(self._appliance_code), cert.endpoint, cert.port,
        )
        c.connect(
            cert.endpoint, cert.port or MQTT_DEFAULT_PORT, keepalive=MQTT_KEEPALIVE,
        )
        c.loop_start()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            _LOGGER.warning(
                "MQTT push connect for appliance %s refused (rc=%s)",
                mask_identifier(self._appliance_code), rc,
            )
            return
        for topic in push_topics(self._region, self._appliance_code):
            client.subscribe(topic, qos=0)
        _LOGGER.debug(
            "MQTT push subscribed for appliance %s",
            mask_identifier(self._appliance_code),
        )
        self._set_connected(True)

    def _on_disconnect(self, client, userdata, rc) -> None:
        _LOGGER.debug(
            "MQTT push disconnected for appliance %s (rc=%s)",
            mask_identifier(self._appliance_code), rc,
        )
        self._set_connected(False)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except AttributeError:
            return
        status = decode_push_payload(payload)
        if status is None:
            return
        self._on_status(status)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._on_connected_change(connected)

    async def async_stop(self) -> None:
        """Disconnect and clean up the certificate files."""
        if self._mqtt is not None:
            await self._hass.async_add_executor_job(self._teardown)
            self._mqtt = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
        self._connected = False

    def _teardown(self) -> None:
        assert self._mqtt is not None
        self._mqtt.loop_stop()
        self._mqtt.disconnect()
