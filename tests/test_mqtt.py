"""Tests for the MQTT real-time push transport (issue #55)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from custom_components.iletcomfort.api import AppCert, ITSStatus
from custom_components.iletcomfort.mqtt import (
    certificate_expiry,
    decode_push_payload,
    push_topics,
    ILetComfortPushClient,
)

# A real C3 subtype-0x01 status push captured on hardware (issue #55). The
# device publishes on us/midea/dev/<code>; the payload wraps the C3 frame hex
# in a `commandHex` field.
LIVE_C3_COMMAND_HEX = (
    "aa3dc300000000000004010104556e3255370f23372528c00000002332553700233700"
    "550000000000000000000001e005be05be0000000001c400000142"
)


def _push_payload(command_hex: str = LIVE_C3_COMMAND_HEX, code: str = "APPL1") -> str:
    return json.dumps({
        "messageType": "status",
        "data": {
            "applianceCode": code,
            "commandHex": command_hex,
            "msgType": "64",
            "msgVersion": "v1",
            "timeStamp": "1788900837716",
        },
    })


# --- topic construction ------------------------------------------------------

def test_push_topics_scoped_to_own_code_only():
    """Never a wildcard across appliances -- the broker has no tenant ACL."""
    topics = push_topics("us", "APPL1")

    assert "us/midea/dev/APPL1" in topics
    for t in topics:
        assert "APPL1" in t
        assert not t.endswith("/#/#")
        assert "+" not in t


def test_push_topics_respects_region():
    assert all(t.startswith("eu/") for t in push_topics("eu", "APPL1"))


# --- payload decode ----------------------------------------------------------

def test_decode_push_payload_returns_status_from_command_hex():
    status = decode_push_payload(_push_payload())

    assert isinstance(status, ITSStatus)
    # The captured frame is mode 4 (Water Pump), set_temperature 50.
    assert status.mode == 4
    assert status.set_temperature == 50


def test_decode_push_payload_ignores_non_status_messages():
    control = json.dumps({"messageType": "control", "data": {"foo": "bar"}})

    assert decode_push_payload(control) is None


def test_decode_push_payload_tolerates_missing_command_hex():
    assert decode_push_payload(json.dumps({"messageType": "status", "data": {}})) is None


def test_decode_push_payload_tolerates_garbage():
    assert decode_push_payload("not json") is None
    assert decode_push_payload(json.dumps({"data": {"commandHex": "zzzz"}})) is None


# --- lifecycle with an injected paho client ----------------------------------


def test_certificate_expiry_reads_x509_not_after():
    """The rotation deadline comes from the issued certificate itself."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    expected = now + timedelta(days=30)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(expected)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()

    assert certificate_expiry(pem) == expected

def _cert() -> AppCert:
    return AppCert(
        private_key="-----BEGIN RSA PRIVATE KEY-----\nk\n-----END RSA PRIVATE KEY-----",
        certificate_pem="-----BEGIN CERTIFICATE-----\nc\n-----END CERTIFICATE-----",
        endpoint="broker.invalid",
        port=8883,
    )


@pytest.fixture
def fake_paho(monkeypatch):
    """Replace paho's Client with a fake that records calls and drives callbacks."""
    created = {}

    class FakeClient:
        def __init__(self, *a, **k):
            self.subscriptions = []
            self.tls_args = None
            self.connected = False
            self.loop_started = False
            self.on_connect = self.on_disconnect = self.on_message = None
            created["client"] = self

        def tls_set_context(self, context):
            self.tls_args = context

        def tls_set(self, **kw):
            self.tls_args = kw

        def connect_async(self, host, port, keepalive):
            self.host, self.port, self.keepalive = host, port, keepalive

        def connect(self, host, port, keepalive):
            self.host, self.port, self.keepalive = host, port, keepalive

        def loop_start(self):
            self.loop_started = True

        def loop_stop(self):
            self.loop_started = False

        def subscribe(self, topic, qos=0):
            self.subscriptions.append(topic)
            return (0, len(self.subscriptions))

        def disconnect(self):
            self.connected = False

        # helpers for the test to simulate broker events
        def fire_connect(self, rc=0):
            self.connected = rc == 0
            self.on_connect(self, None, {}, rc)

        def fire_message(self, topic, payload):
            msg = MagicMock()
            msg.topic = topic
            msg.payload = payload.encode()
            self.on_message(self, None, msg)

        def fire_disconnect(self, rc=1):
            self.connected = False
            self.on_disconnect(self, None, rc)

    import custom_components.iletcomfort.mqtt as mod
    monkeypatch.setattr(mod.mqtt, "Client", FakeClient)
    return created


async def test_push_client_subscribes_to_own_topics_on_connect(hass, fake_paho):
    statuses, conn = [], []
    api = MagicMock()
    api.create_app_cert.return_value = _cert()

    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=statuses.append, on_connected_change=conn.append,
    )
    await client.async_start()
    fake = fake_paho["client"]
    assert fake.loop_started

    fake.fire_connect(rc=0)
    assert "us/midea/dev/APPL1" in fake.subscriptions
    assert conn == [True]


async def test_push_client_delivers_decoded_status(hass, fake_paho):
    statuses = []
    api = MagicMock()
    api.create_app_cert.return_value = _cert()

    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=statuses.append, on_connected_change=lambda *_: None,
    )
    await client.async_start()
    fake = fake_paho["client"]
    fake.fire_connect()
    fake.fire_message("us/midea/dev/APPL1", _push_payload())

    assert len(statuses) == 1
    assert statuses[0].mode == 4


async def test_push_client_reports_disconnect(hass, fake_paho):
    conn = []
    api = MagicMock()
    api.create_app_cert.return_value = _cert()

    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=conn.append,
    )
    await client.async_start()
    fake = fake_paho["client"]
    fake.fire_connect()
    fake.fire_disconnect(rc=1)

    assert conn == [True, False]
    assert client.connected is False


async def test_push_client_stop_is_clean(hass, fake_paho):
    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )
    await client.async_start()
    fake = fake_paho["client"]
    await client.async_stop()
    assert fake.loop_started is False


async def test_push_client_removes_cert_files_on_stop(hass, fake_paho):
    """The private key/cert must not outlive the connection (AC: no keys kept)."""
    import os

    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )
    await client.async_start()
    tmpdir = client._tmpdir.name
    assert os.path.isdir(tmpdir)

    await client.async_stop()
    assert not os.path.exists(tmpdir)


async def test_push_client_never_logs_key_material(hass, fake_paho, caplog):
    import logging
    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )
    with caplog.at_level(logging.DEBUG):
        await client.async_start()
        fake = fake_paho["client"]
        fake.fire_connect()
        fake.fire_message("us/midea/dev/APPL1", _push_payload())
    assert "BEGIN RSA PRIVATE KEY" not in caplog.text
    assert "APPL1" not in caplog.text  # appliance code is masked in logs


async def test_push_client_schedules_renewal_from_certificate_expiry(
    hass, fake_paho, monkeypatch
):
    """Rotation is driven by X.509 validity rather than an arbitrary cadence."""
    import custom_components.iletcomfort.mqtt as mod

    monkeypatch.setattr(
        mod,
        "certificate_expiry",
        lambda _: datetime.now(timezone.utc) + timedelta(days=30),
    )
    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )

    await client.async_start()

    assert client._renewal_handle is not None
    await client.async_stop()


async def test_failed_certificate_renewal_keeps_current_connection(
    hass, fake_paho, monkeypatch
):
    """A minting failure must not tear down a still-valid connection."""
    import custom_components.iletcomfort.mqtt as mod

    monkeypatch.setattr(
        mod,
        "certificate_expiry",
        lambda _: datetime.now(timezone.utc) + timedelta(days=30),
    )
    api = MagicMock()
    api.create_app_cert.side_effect = [_cert(), RuntimeError("mint failed")]
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )
    await client.async_start()
    original_mqtt = client._mqtt
    original_tmpdir = client._tmpdir

    await client.async_renew_certificate()

    assert client._mqtt is original_mqtt
    assert client._tmpdir is original_tmpdir
    assert client._renewal_handle is not None
    await client.async_stop()


async def test_successful_certificate_renewal_retires_old_transport_and_files(
    hass, fake_paho, monkeypatch
):
    """A replacement starts before the old transport and credentials are removed."""
    import os
    import custom_components.iletcomfort.mqtt as mod

    monkeypatch.setattr(
        mod,
        "certificate_expiry",
        lambda _: datetime.now(timezone.utc) + timedelta(days=30),
    )
    api = MagicMock()
    api.create_app_cert.side_effect = [_cert(), _cert()]
    client = ILetComfortPushClient(
        hass, api, region="us", appliance_code="APPL1",
        on_status=lambda *_: None, on_connected_change=lambda *_: None,
    )
    await client.async_start()
    old_mqtt = client._mqtt
    old_tmpdir = client._tmpdir.name

    await client.async_renew_certificate()

    assert client._mqtt is not old_mqtt
    assert old_mqtt.loop_started is False
    assert not os.path.exists(old_tmpdir)
    assert client._renewal_handle is not None
    await client.async_stop()
