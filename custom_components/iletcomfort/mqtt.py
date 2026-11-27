"""MQTT real-time status push for iLetComfort C3 heat pumps (issue #55, #108).

The vendor's cloud pushes decoded status over an AWS IoT MQTT broker. A client
certificate minted via :meth:`ILetComfortClient.create_app_cert` authenticates
**independently of the account session**, so push keeps flowing while the phone
app holds the single active account login (the "login war"). This is opt-in and
disabled by default. In phone-app coexistence mode, a push outage marks entities
unavailable rather than falling back to account polling and evicting the app.

Internal seams (#108):
* :mod:`mqtt_credentials` owns X.509 expiry calculation, renewal scheduling,
  hourly retry, private temporary key files, and transactional replacement.
* :class:`MqttBrokerTransport` owns paho client lifecycle, appliance-scoped
  topic subscription, connection callbacks, and scoped payload delivery.
* :class:`ILetComfortPushClient` composes both behind a small public interface.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
import tempfile
from asyncio import TimerHandle
from collections.abc import Callable
from typing import TYPE_CHECKING

import paho.mqtt.client as mqtt

from .api import (
    ITSStatus,
    AppCert,
    decode_its_status,
    extract_c3_body,
    mask_identifier,
    parse_hex_response,
)
from .protocols.atw import ATW_ENERGY_NOTIFY_MIN_LEN, ATW_ENERGY_NOTIFY_SUBTYPE
from .mqtt_credentials import (
    CERT_RENEW_BEFORE,
    CERT_RENEW_RETRY,
    CertificateProvider,
    MaterializedCredentials,
    MqttCredentialRotator,
    certificate_expiry,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .api import ILetComfortClient

_LOGGER = logging.getLogger(__name__)

StatusCallback = Callable[[ITSStatus], None]
EnergyCallback = Callable[[bytes], None]
ConnectedCallback = Callable[[bool], None]

_HEX_FRAME = re.compile(r"(?:[0-9a-fA-F]{2}){1,512}")


def push_topics(region: str, appliance_code: str) -> list[str]:
    """Return the push topics to subscribe to for one appliance.

    Scoped strictly to ``appliance_code`` — the broker grants wildcard
    subscriptions that would leak other appliances' data, so we never use one.
    """
    return [f"{region}/midea/dev/{appliance_code}"]


def _extract_push_frame(payload: str) -> tuple[int, bytearray] | None:
    """Parse a ``dev`` JSON push payload into ``(subtype, body)``, or None."""
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
        return extract_c3_body(raw)
    except (ValueError, IndexError):
        return None


def decode_push_payload(payload: str) -> ITSStatus | None:
    """Decode a ``dev`` push payload into an :class:`ITSStatus`, or None."""
    extracted = _extract_push_frame(payload)
    if extracted is None:
        return None
    subtype, body = extracted
    if subtype != 0x01:
        return None
    try:
        return decode_its_status(body)
    except (ValueError, IndexError):
        return None


def decode_push_energy_payload(payload: str) -> bytes | None:
    """Extract a C3 subtype-0x04 (C3EnergyBody) push body, or None (#129)."""
    extracted = _extract_push_frame(payload)
    if extracted is None:
        return None
    subtype, body = extracted
    if subtype != ATW_ENERGY_NOTIFY_SUBTYPE or len(body) < ATW_ENERGY_NOTIFY_MIN_LEN:
        return None
    return bytes(body)


def build_tls_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """Build the client TLS context for the broker connection.

    Blocking (reads the CA store and the credential files): call it from an
    executor thread only.

    ``ssl.PROTOCOL_TLSv1_2`` is deprecated and some Python builds do not have
    it, so the context uses ``PROTOCOL_TLS_CLIENT`` pinned to TLS 1.2 with the
    minimum and maximum version. The server certificate is verified against
    the system CA store and the hostname is checked, as before.

    Keep the TLS 1.2 maximum. With TLS 1.3 the client handshake completes
    before the broker rejects a client certificate, so ``connect`` returns
    without error and the rejection arrives later in the paho loop thread.
    Certificate rotation uses a ``connect`` error to keep the old connection,
    and it must not lose that signal.
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(cert_path, key_path)
    context.load_default_certs()
    return context


class MqttBrokerTransport:
    """Owns one paho MQTT connection, appliance-scoped subscription, and decoding (#108)."""

    def __init__(
        self,
        *,
        region: str,
        appliance_code: str,
        on_status: StatusCallback,
        on_connected_change: ConnectedCallback,
        on_energy: EnergyCallback | None = None,
    ) -> None:
        self._region = region
        self._appliance_code = appliance_code
        self._on_status = on_status
        self._on_energy = on_energy
        self._on_connected_change = on_connected_change
        self.client: mqtt.Client | None = None

    def connect(self, cert: AppCert, credentials: MaterializedCredentials) -> None:
        """Configure TLS with materialized credentials, connect, and start loop."""
        c = mqtt.Client()
        c.tls_set_context(
            build_tls_context(
                str(credentials.cert_path), str(credentials.key_path)
            )
        )
        c.on_connect = self._on_connect
        c.on_disconnect = self._on_disconnect
        c.on_message = self._on_message
        self.client = c

        from .const import MQTT_DEFAULT_PORT, MQTT_KEEPALIVE

        _LOGGER.debug(
            "Connecting MQTT push for appliance %s to %s:%s",
            mask_identifier(self._appliance_code),
            cert.endpoint,
            cert.port,
        )
        try:
            c.connect(
                cert.endpoint,
                cert.port or MQTT_DEFAULT_PORT,
                keepalive=MQTT_KEEPALIVE,
            )
            c.loop_start()
        except Exception:
            c.on_disconnect = None
            try:
                c.loop_stop()
                c.disconnect()
            except Exception:  # noqa: BLE001 — best-effort cleanup on failed connect
                pass
            self.client = None
            raise

    def teardown(self, *, suppress_disconnect: bool = True) -> None:
        """Stop the paho loop and disconnect this transport."""
        client = self.client
        self.client = None
        if client is None:
            return
        if suppress_disconnect:
            client.on_disconnect = None
        client.loop_stop()
        client.disconnect()

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            _LOGGER.warning(
                "MQTT push connect for appliance %s refused (rc=%s)",
                mask_identifier(self._appliance_code),
                rc,
            )
            return
        for topic in push_topics(self._region, self._appliance_code):
            client.subscribe(topic, qos=0)
        _LOGGER.debug(
            "MQTT push subscribed for appliance %s",
            mask_identifier(self._appliance_code),
        )
        self._on_connected_change(True)

    def _on_disconnect(self, client, userdata, rc) -> None:
        _LOGGER.debug(
            "MQTT push disconnected for appliance %s (rc=%s)",
            mask_identifier(self._appliance_code),
            rc,
        )
        self._on_connected_change(False)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            payload = msg.payload.decode("utf-8", "replace")
        except AttributeError:
            return
        status = decode_push_payload(payload)
        if status is None:
            energy_body = decode_push_energy_payload(payload)
            if energy_body is not None and self._on_energy is not None:
                self._on_energy(energy_body)
                return
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
        on_energy: EnergyCallback | None = None,
        get_certificate: CertificateProvider | None = None,
    ) -> None:
        self._hass = hass
        self._client = client
        self._region = region
        self._appliance_code = appliance_code
        self._on_status = on_status
        self._on_energy = on_energy
        self._on_connected_change = on_connected_change
        self._connected = False
        self._renewal_task: asyncio.Task[None] | None = None
        self._rotator: MqttCredentialRotator[MqttBrokerTransport] = MqttCredentialRotator(
            get_certificate=get_certificate or self._async_create_certificate,
            connect=self._async_connect_transport,
            retire=self._async_retire_transport,
            materialize=self._async_materialize_credentials,
            cleanup_credentials=self._async_cleanup_credentials,
            schedule_later=self._hass.loop.call_later,
            trigger_renewal=self._schedule_renewal_task,
            expiry_fn=lambda pem: certificate_expiry(pem),
        )

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def _mqtt(self) -> mqtt.Client | None:
        transport = self._rotator.active_transport
        return transport.client if transport is not None else None

    @property
    def _tmpdir(self) -> tempfile.TemporaryDirectory[str] | None:
        creds = self._rotator.active_credentials
        return creds.tmpdir if creds is not None else None

    @property
    def _renewal_handle(self) -> TimerHandle | None:
        return self._rotator.renewal_handle

    async def async_start(self) -> None:
        """Mint a certificate, then connect and subscribe (off the event loop)."""
        await self._rotator.async_start()

    async def async_renew_certificate(self) -> None:
        """Rotate credentials while retaining the old connection on mint failure."""
        await self._rotator.async_renew()

    async def async_stop(self) -> None:
        """Disconnect and clean up the certificate files."""
        task = self._renewal_task
        self._renewal_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._rotator.async_stop()
        self._connected = False

    def _schedule_renewal_task(self) -> None:
        if self._renewal_task is not None and not self._renewal_task.done():
            return
        task = self._hass.async_create_task(
            self.async_renew_certificate(),
            "iLetComfort MQTT certificate renewal",
        )
        self._renewal_task = task

        def _clear(finished: asyncio.Task[None]) -> None:
            if self._renewal_task is finished:
                self._renewal_task = None

        task.add_done_callback(_clear)

    async def _async_create_certificate(self) -> AppCert:
        return await self._hass.async_add_executor_job(self._client.create_app_cert)

    async def _async_materialize_credentials(
        self, cert: AppCert
    ) -> MaterializedCredentials:
        return await self._hass.async_add_executor_job(MaterializedCredentials, cert)

    async def _async_cleanup_credentials(
        self, credentials: MaterializedCredentials
    ) -> None:
        await self._hass.async_add_executor_job(credentials.cleanup)

    async def _async_connect_transport(
        self, cert: AppCert, credentials: MaterializedCredentials
    ) -> MqttBrokerTransport:
        transport = MqttBrokerTransport(
            region=self._region,
            appliance_code=self._appliance_code,
            on_status=self._on_status,
            on_energy=self._on_energy,
            on_connected_change=self._set_connected,
        )
        await self._hass.async_add_executor_job(transport.connect, cert, credentials)
        return transport

    async def _async_retire_transport(self, transport: MqttBrokerTransport) -> None:
        await self._hass.async_add_executor_job(
            lambda: transport.teardown(suppress_disconnect=True)
        )

    def _on_message(self, client, userdata, msg) -> None:
        """Forward a synthetic message to the active transport (test helper seam)."""
        transport = self._rotator.active_transport
        if transport is not None:
            transport._on_message(client, userdata, msg)

    def _set_connected(self, connected: bool) -> None:
        if connected == self._connected:
            return
        self._connected = connected
        self._on_connected_change(connected)


__all__ = [
    "CERT_RENEW_BEFORE",
    "CERT_RENEW_RETRY",
    "CertificateProvider",
    "ConnectedCallback",
    "EnergyCallback",
    "ILetComfortPushClient",
    "MqttBrokerTransport",
    "StatusCallback",
    "certificate_expiry",
    "decode_push_energy_payload",
    "decode_push_payload",
    "push_topics",
]
