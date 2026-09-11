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
import re
import ssl
import tempfile
from asyncio import TimerHandle
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt
from cryptography import x509

from .api import (
    ITSStatus,
    AppCert,
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
CertificateProvider = Callable[[], Awaitable[AppCert]]

CERT_RENEW_BEFORE = timedelta(days=7)
CERT_RENEW_RETRY = timedelta(hours=1)
_HEX_FRAME = re.compile(r"(?:[0-9a-fA-F]{2}){1,512}")


def certificate_expiry(certificate_pem: str) -> datetime:
    """Return the UTC expiry embedded in an app-issued X.509 certificate."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
    expires_utc = getattr(cert, "not_valid_after_utc", None)
    if expires_utc is not None:
        return expires_utc
    return cert.not_valid_after.replace(tzinfo=timezone.utc)


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
        get_certificate: CertificateProvider | None = None,
    ) -> None:
        self._hass = hass
        self._client = client
        self._region = region
        self._appliance_code = appliance_code
        self._on_status = on_status
        self._on_connected_change = on_connected_change
        self._get_certificate = get_certificate or self._async_create_certificate
        self._mqtt: mqtt.Client | None = None
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self._connected = False
        self._renewal_handle: TimerHandle | None = None
        self._stopped = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def async_start(self) -> None:
        """Mint a certificate, then connect and subscribe (off the event loop)."""
        self._stopped = False
        cert = await self._get_certificate()
        await self._hass.async_add_executor_job(self._connect, cert)
        self._schedule_renewal(cert.certificate_pem)

    async def _async_create_certificate(self) -> AppCert:
        return await self._hass.async_add_executor_job(self._client.create_app_cert)

    def _schedule_renewal(
        self,
        certificate_pem: str | None = None,
        *,
        retry: bool = False,
    ) -> None:
        """Schedule rotation from the certificate validity or a short retry."""
        if self._stopped:
            return
        if self._renewal_handle is not None:
            self._renewal_handle.cancel()
        if retry:
            delay = CERT_RENEW_RETRY.total_seconds()
        else:
            assert certificate_pem is not None
            try:
                expires = certificate_expiry(certificate_pem)
            except ValueError:
                _LOGGER.warning(
                    "MQTT certificate validity could not be read; automatic "
                    "renewal is disabled for this connection"
                )
                self._renewal_handle = None
                return
            remaining = expires - datetime.now(timezone.utc)
            # Renew at 90% of short certificate lifetimes, capped at seven
            # days early for long-lived certificates. This avoids a tight
            # rotation loop if the vendor ever issues certificates shorter
            # than the normal renewal lead time.
            renew_before = min(CERT_RENEW_BEFORE, remaining / 10)
            delay = max(1.0, (remaining - renew_before).total_seconds())
        self._renewal_handle = self._hass.loop.call_later(
            delay,
            lambda: self._hass.async_create_task(
                self.async_renew_certificate(),
                "iLetComfort MQTT certificate renewal",
            ),
        )

    async def async_renew_certificate(self) -> None:
        """Rotate credentials while retaining the old connection on mint failure."""
        if self._stopped:
            return
        if self._renewal_handle is not None:
            self._renewal_handle.cancel()
        self._renewal_handle = None
        try:
            cert = await self._get_certificate()
        except Exception as err:  # noqa: BLE001 — retry while old cert remains live
            _LOGGER.warning("MQTT certificate renewal failed; retrying later: %s", err)
            self._schedule_renewal(retry=True)
            return

        if self._stopped:
            return

        old_mqtt = self._mqtt
        old_tmpdir = self._tmpdir
        try:
            await self._hass.async_add_executor_job(self._connect, cert)
        except Exception as err:  # noqa: BLE001 — keep retrying after connect failure
            failed_mqtt = self._mqtt
            failed_tmpdir = self._tmpdir
            self._mqtt = old_mqtt
            self._tmpdir = old_tmpdir
            if failed_mqtt is not None and failed_mqtt is not old_mqtt:
                failed_mqtt.on_disconnect = None
                await self._hass.async_add_executor_job(
                    self._teardown_client, failed_mqtt
                )
            if failed_tmpdir is not None and failed_tmpdir is not old_tmpdir:
                failed_tmpdir.cleanup()
            _LOGGER.warning(
                "MQTT certificate replacement connection failed; retrying later: %s",
                err,
            )
            self._schedule_renewal(retry=True)
            return

        # The replacement has started before the old transport and key files
        # are retired, avoiding a gap caused by certificate issuance itself.
        if old_mqtt is not None:
            # A planned retirement is not a loss of push liveness. Suppress the
            # old client's disconnect callback while the replacement takes over.
            old_mqtt.on_disconnect = None
            await self._hass.async_add_executor_job(self._teardown_client, old_mqtt)
        if old_tmpdir is not None:
            old_tmpdir.cleanup()
        self._schedule_renewal(cert.certificate_pem)

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
            # The phone app may publish control frames on this same appliance-
            # scoped topic. Keep a deliberately narrow debug trail so the
            # write protocol can be established from hardware evidence. Never
            # dump the full JSON envelope: it can contain appliance metadata.
            try:
                message = json.loads(payload)
                data = message.get("data") if isinstance(message, dict) else None
                command_hex = data.get("commandHex") if isinstance(data, dict) else None
            except (TypeError, ValueError):
                command_hex = None
                message = None
            if isinstance(command_hex, str) and _HEX_FRAME.fullmatch(command_hex):
                _LOGGER.debug(
                    "MQTT unhandled scoped frame message_type=%r command_hex=%s",
                    message.get("messageType"),
                    command_hex.lower(),
                )
            return
        self._on_status(status)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._on_connected_change(connected)

    async def async_stop(self) -> None:
        """Disconnect and clean up the certificate files."""
        self._stopped = True
        if self._renewal_handle is not None:
            self._renewal_handle.cancel()
            self._renewal_handle = None
        if self._mqtt is not None:
            await self._hass.async_add_executor_job(self._teardown)
            self._mqtt = None
        if self._tmpdir is not None:
            self._tmpdir.cleanup()
            self._tmpdir = None
        self._connected = False

    def _teardown(self) -> None:
        assert self._mqtt is not None
        self._teardown_client(self._mqtt)

    @staticmethod
    def _teardown_client(client: mqtt.Client) -> None:
        client.loop_stop()
        client.disconnect()
