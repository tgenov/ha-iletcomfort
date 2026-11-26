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


async def test_push_client_debug_logs_unhandled_scoped_command_frame(
    hass, fake_paho, caplog
):
    """Keep an evidence trail for phone-app commands on the appliance topic."""
    import logging

    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass,
        api,
        region="us",
        appliance_code="APPL1",
        on_status=lambda *_: None,
        on_connected_change=lambda *_: None,
    )
    payload = json.dumps(
        {
            "messageType": "control",
            "data": {
                "applianceCode": "APPL1",
                "commandHex": "aa0bc3000000000000020101",
            },
        }
    )

    with caplog.at_level(logging.DEBUG):
        await client.async_start()
        fake_paho["client"].fire_connect()
        fake_paho["client"].fire_message("us/midea/dev/APPL1", payload)

    assert "message_type='control'" in caplog.text
    assert "command_hex=aa0bc3000000000000020101" in caplog.text
    assert "APPL1" not in caplog.text


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


# Real Hyundai ATW (sn8 171H120F, #129) unsolicited MQTT 0x04 C3EnergyBody push.
HYUNDAI_MQTT_0X04_HEX = (
    "aab9c30000000000000404010000372e00007db5002f1e3230ffff01"
    + "00" * 145
    + "e80000000000000000000000000000001e000030"
)


def test_decode_push_energy_payload_hyundai_issue129():
    from custom_components.iletcomfort.mqtt import decode_push_energy_payload

    payload = json.dumps(
        {"messageType": "status", "data": {"commandHex": HYUNDAI_MQTT_0X04_HEX}}
    )
    assert decode_push_payload(payload) is None
    body = decode_push_energy_payload(payload)
    assert body is not None
    assert body[0] == 0x04
    assert body[1] == 0x01
    assert int.from_bytes(body[2:6], "big") == 14126
    assert int.from_bytes(body[6:10], "big") == 32181
    assert body[10] == 0x00


async def test_push_client_dispatches_0x04_energy_callback(hass, fake_paho):
    received_energy: list[bytes] = []
    api = MagicMock()
    api.create_app_cert.return_value = _cert()
    client = ILetComfortPushClient(
        hass,
        api,
        region="eu",
        appliance_code="APPL1",
        on_status=lambda *_: None,
        on_energy=received_energy.append,
        on_connected_change=lambda *_: None,
    )
    await client.async_start()

    msg = MagicMock()
    msg.payload = json.dumps(
        {"messageType": "status", "data": {"commandHex": HYUNDAI_MQTT_0X04_HEX}}
    ).encode()
    client._on_message(client._mqtt, None, msg)

    assert len(received_energy) == 1
    assert received_energy[0][0] == 0x04
    await client.async_stop()


async def test_coordinator_apply_push_energy_records_push_and_overlays_atw(
    hass,
):
    from custom_components.iletcomfort.api import ITSSensors
    from custom_components.iletcomfort.const import (
        CONF_APPLIANCE_CODE,
        CONF_OPERATION_MODE,
        CONF_REGION,
        DOMAIN,
        OPERATION_MODE_PHONE_APP,
        REGION_EU,
    )
    from custom_components.iletcomfort.coordinator import ILetComfortCoordinator
    from custom_components.iletcomfort.model_profiles import ATW_SN8, decode_atw_status
    from custom_components.iletcomfort.mqtt import decode_push_energy_payload
    from custom_components.iletcomfort.sensor import SENSOR_DESCRIPTIONS
    from unittest.mock import patch

    from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "u@example.com",
            CONF_PASSWORD: "p",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        options={CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP},
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)

    coord.appliance_meta = {"sn8": ATW_SN8}
    initial_status = decode_atw_status(
        bytes.fromhex(
            "01 0d 15 90 03 03 28 1e 32 30 41 23 19 05 37 19 19 05 3c 22 3c 14 32 00 80"
        )
    )
    coord.data = {
        "status": initial_status,
        "sensors": ITSSensors(),
        "raw_frames": {0x01: initial_status.raw_body},
    }

    energy_body = decode_push_energy_payload(
        json.dumps({"messageType": "status", "data": {"commandHex": HYUNDAI_MQTT_0X04_HEX}})
    )
    assert energy_body is not None
    coord._apply_push_energy(energy_body)

    assert coord._last_push_status_at is not None
    # ATW has no periodic status cadence, so no heartbeat watchdog is armed (#129).
    assert coord._push_status_watchdog is None
    assert coord.data["status"].total_kwh == 14126
    assert coord.data["status"].total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == 0.0
    assert 0x04 in coord.data["raw_frames"]

    by_key = {desc.key: desc for desc in SENSOR_DESCRIPTIONS}
    assert by_key["total_thermal_energy"].value_fn(coord.data) == 32181

    # A subsequent 0x01 status push must NOT clobber 0x04 energy fields back to None.
    next_0x01 = decode_atw_status(
        bytes.fromhex(
            "01 0c 15 90 03 03 28 1e 32 30 41 23 19 05 37 19 19 05 3c 22 3c 14 32 00 80"
        )
    )
    coord._apply_push_status(next_0x01)
    assert coord.data["status"].total_kwh == 14126
    assert coord.data["status"].total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == 0.0

    # Negative outdoor temp (0xFB -> -5.0 °C) and 0x7F disconnected sentinel / 0xFFFFFFFF guard.
    neg_body = bytearray(energy_body)
    neg_body[10] = 0xFB
    coord._apply_push_energy(bytes(neg_body))
    assert coord.data["sensors"].t4_temp == -5.0

    sentinel_body = bytearray(energy_body)
    sentinel_body[2:6] = b"\xff\xff\xff\xff"
    sentinel_body[6:10] = b"\x00\x00\x00\x00"
    sentinel_body[10] = 0x7F
    coord._apply_push_energy(bytes(sentinel_body))
    assert coord.data["status"].total_kwh == 14126
    assert coord.data["status"].total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == -5.0

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


# --- Issue #225: MQTT status push must keep ATW subtype overlays -------------

_HYUNDAI_ATW_STATUS = bytes.fromhex(
    "01 0d 15 90 03 03 28 1e 32 30 41 23 19 05 37 19 19 05 3c 22 3c 14 32 00 80"
)


def _corpus_frame(case_id: str, key: str) -> bytes:
    from pathlib import Path

    path = Path(__file__).parent / "corpus" / "atw_171H120F.json"
    case = next(
        c for c in json.loads(path.read_text(encoding="utf-8")) if c["id"] == case_id
    )
    return bytes(int(part, 16) for part in case["frames"][key].split(","))


def _atw_phone_app_coordinator(hass, sn8: str | None = "171H120F"):
    from unittest.mock import patch

    from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.iletcomfort.const import (
        CONF_APPLIANCE_CODE,
        CONF_OPERATION_MODE,
        CONF_REGION,
        DOMAIN,
        OPERATION_MODE_PHONE_APP,
        REGION_EU,
    )
    from custom_components.iletcomfort.coordinator import ILetComfortCoordinator

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "u@example.com",
            CONF_PASSWORD: "p",
            CONF_APPLIANCE_CODE: "APPL1",
            CONF_REGION: REGION_EU,
        },
        options={CONF_OPERATION_MODE: OPERATION_MODE_PHONE_APP},
        version=2,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.iletcomfort.coordinator.ILetComfortClient"):
        coord = ILetComfortCoordinator(hass, entry)
    coord.appliance_meta = {"sn8": sn8}
    return coord


def _polled_atw_snapshot(
    status_frame: bytes | None = None, silent_frame: bytes | None = None
) -> dict:
    """Return coordinator data as an ATW poll with 0x10/0x0A/0x05/0x07 leaves it.

    Built by hand (not via the production builder) so the regression tests
    pin the overlay values independently of the code under test. The default
    0x01 frame has the Silent bit (body[3] & 0x02) set, matching the default
    0x05 frame (Silent Level 1 on), as one real poll would see them.
    """
    from custom_components.iletcomfort.api import decode_its_sensors
    from custom_components.iletcomfort.model_profiles import (
        apply_atw_eco,
        apply_atw_hmi_params,
        apply_atw_silent,
        apply_atw_unit_params,
        decode_atw_status,
    )
    from custom_components.iletcomfort.protocols import get_adapter

    frames = {
        0x01: status_frame if status_frame is not None else _ATW_STATUS_SILENT_ON,
        0x02: _corpus_frame("atw_galmet_prima_8gt_0x10_issue118", "0x02"),
        0x10: _corpus_frame("atw_galmet_prima_8gt_0x10_issue118", "0x10"),
        0x0A: _corpus_frame("atw_galmet_prima_8gt_0x10_issue118", "0x0a"),
        0x05: (
            silent_frame
            if silent_frame is not None
            else _corpus_frame("atw_airwell_split_silent_eco_issue139", "0x05")
        ),
        0x07: _corpus_frame("atw_airwell_split_silent_eco_issue139", "0x07"),
    }
    status = decode_atw_status(frames[0x01])
    assert status.c3_basic is not None
    adapter = get_adapter("171H120F")
    sensors = adapter.decode_sensors(decode_its_sensors(frames[0x02]), status)
    status, sensors = apply_atw_unit_params(frames[0x10], status, sensors)
    status = apply_atw_hmi_params(frames[0x0A], status)
    status = apply_atw_silent(frames[0x05], status)
    status = apply_atw_eco(frames[0x07], status)
    return {"status": status, "sensors": sensors, "raw_frames": dict(frames)}


def _with_silent_bit(frame: bytes, on: bool) -> bytes:
    body = bytearray(frame)
    if on:
        body[3] |= 0x02
    else:
        body[3] &= ~0x02 & 0xFF
    return bytes(body)


_ATW_STATUS_SILENT_ON = _with_silent_bit(_HYUNDAI_ATW_STATUS, True)

# Values that cannot change between polls in a way a push could contradict:
# cumulative counters (TOTAL_INCREASING) and ECO (no push frame carries it).
_RETAINED_STATUS_FIELDS = (
    "total_kwh",
    "total_thermal_kwh",
    "comp_total_run_hours",
    "atw_eco_mode",
    "atw_eco_timer",
)
# Instantaneous 0x10 values: the setup poll's reading must not freeze.
_INSTANT_0X10_STATUS_FIELDS = ("comp_frq", "comp_running", "exv_drg", "pressure_h")
_OVERLAY_SENSOR_FIELDS = (
    "t3_temp",
    "t4_temp",
    "twin_temp",
    "twout_temp",
    "odu_voltage",
)


async def test_atw_status_push_keeps_subtype_overlays_issue225(hass):
    """A 0x01 push keeps counters, run hours, Silent and ECO from retained frames."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    before_status = coord.data["status"]
    before_sensors = coord.data["sensors"]
    for field in _RETAINED_STATUS_FIELDS + _INSTANT_0X10_STATUS_FIELDS:
        assert getattr(before_status, field) is not None, field
    assert before_status.atw_silent_level == 1

    coord._apply_push_status(decode_atw_status(_ATW_STATUS_SILENT_ON))

    after = coord.data
    for field in _RETAINED_STATUS_FIELDS:
        assert getattr(after["status"], field) == getattr(before_status, field), field
    assert after["status"].atw_silent_mode is True
    assert after["status"].atw_silent_level == 1
    assert after["status"].atw_silent_super_selected is False
    for field in _OVERLAY_SENSOR_FIELDS:
        assert getattr(after["sensors"], field) == getattr(before_sensors, field), field
    assert after["state"].status is after["status"]
    assert after["state"].has_atw_0x10 is True

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_status_push_does_not_freeze_instant_0x10_values_issue225(hass):
    """Compressor/pressure values from the setup poll go unknown on push (as main)."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()

    polled = coord.data["status"]
    pushed = decode_atw_status(_ATW_STATUS_SILENT_ON)
    coord._apply_push_status(pushed)

    # Instantaneous fields are exactly what the pushed 0x01 decode produces
    # (None, or ITSStatus's int default for exv_drg), never the poll's value.
    for field in _INSTANT_0X10_STATUS_FIELDS:
        assert getattr(coord.data["status"], field) == getattr(pushed, field), field
        assert getattr(coord.data["status"], field) != getattr(polled, field), field
    assert coord.data["status"].comp_frq is None
    assert coord.data["status"].comp_running is None
    assert coord.data["status"].pressure_h is None
    assert coord.data["state"].compressor_freq is None

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_status_push_updates_status_fields_and_keeps_overlays_issue225(
    hass,
):
    """Pushed 0x01 values win while counter/ECO overlays persist."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    before = coord.data["status"]

    pushed = bytearray(_ATW_STATUS_SILENT_ON)
    pushed[1] = 0x0C  # Zone-1 power off
    coord._apply_push_status(decode_atw_status(bytes(pushed)))

    after = coord.data["status"]
    assert after.raw_body == bytes(pushed)
    assert coord.data["raw_frames"][0x01] == bytes(pushed)
    assert after.mode != before.mode
    assert after.total_kwh == before.total_kwh
    assert after.comp_total_run_hours == before.comp_total_run_hours
    assert after.atw_silent_level == before.atw_silent_level
    assert after.atw_eco_mode == before.atw_eco_mode

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_status_push_silent_off_overrides_retained_0x05_issue225(hass):
    """Silent turned off in the app: the 0x01 bit wins over the setup 0x05."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    assert coord.data["status"].atw_silent_level == 1

    off = _with_silent_bit(_ATW_STATUS_SILENT_ON, False)
    coord._apply_push_status(decode_atw_status(off))
    status = coord.data["status"]
    assert status.c3_basic.silent_mode is False
    assert status.atw_silent_mode is False
    assert status.atw_silent_level == 0
    assert status.atw_silent_super_selected is False

    # Turned back on in the app: the retained 0x05 level applies again.
    coord._apply_push_status(decode_atw_status(_ATW_STATUS_SILENT_ON))
    assert coord.data["status"].atw_silent_mode is True
    assert coord.data["status"].atw_silent_level == 1

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_status_push_silent_off_keeps_super_selected_issue225(hass):
    """0x05 bit 3 (Super Silent selected) is retained even when Silent is off."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    super_on = bytearray(_corpus_frame("atw_airwell_split_silent_eco_issue139", "0x05"))
    super_on[1] = 0x09  # on + Super Silent
    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot(silent_frame=bytes(super_on))
    assert coord.data["status"].atw_silent_level == 2

    coord._apply_push_status(
        decode_atw_status(_with_silent_bit(_ATW_STATUS_SILENT_ON, False))
    )
    status = coord.data["status"]
    assert status.atw_silent_mode is False
    assert status.atw_silent_level == 0
    assert status.atw_silent_super_selected is True

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_status_push_silent_on_with_stale_off_0x05_falls_back_issue225(
    hass,
):
    """Silent on in 0x01 but off in the retained 0x05: level is unknown (None)."""
    from custom_components.iletcomfort.model_profiles import decode_atw_status
    from custom_components.iletcomfort.select import ILetComfortMuteSelect

    silent_off = bytearray(
        _corpus_frame("atw_airwell_split_silent_eco_issue139", "0x05")
    )
    silent_off[1] = 0x02  # off, timer 1 kept
    off_status = _with_silent_bit(_ATW_STATUS_SILENT_ON, False)
    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot(
        status_frame=off_status, silent_frame=bytes(silent_off)
    )
    assert coord.data["status"].atw_silent_level == 0

    coord._apply_push_status(decode_atw_status(_ATW_STATUS_SILENT_ON))
    status = coord.data["status"]
    assert status.atw_silent_mode is None
    assert status.atw_silent_level is None
    assert status.atw_silent_super_selected is None

    select = ILetComfortMuteSelect.__new__(ILetComfortMuteSelect)
    select.coordinator = coord
    assert select.current_option == "Level 1"

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_energy_push_keeps_subtype_overlays_issue225(hass):
    """A 0x04 push overlays energy without dropping the other subtype overlays."""
    from custom_components.iletcomfort.mqtt import decode_push_energy_payload

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    before = coord.data["status"]

    energy_body = decode_push_energy_payload(
        json.dumps(
            {"messageType": "status", "data": {"commandHex": HYUNDAI_MQTT_0X04_HEX}}
        )
    )
    assert energy_body is not None
    coord._apply_push_energy(energy_body)

    after = coord.data["status"]
    assert after.total_kwh == 14126
    assert after.total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == 0.0
    for field in ("comp_total_run_hours", "atw_silent_level", "atw_eco_mode"):
        assert getattr(after, field) == getattr(before, field), field

    # A later 0x01 push re-derives the snapshot from all retained frames:
    # the 0x04 energy (applied last) and the retained overlays both survive.
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord._apply_push_status(decode_atw_status(_ATW_STATUS_SILENT_ON))
    after = coord.data["status"]
    assert after.total_kwh == 14126
    assert after.total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == 0.0
    assert after.comp_total_run_hours == before.comp_total_run_hours
    assert after.atw_eco_mode == before.atw_eco_mode

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_atw_energy_push_keeps_literal_0x04_in_raw_frames_issue225(hass):
    """raw_frames[0x04] is the device's literal frame; the merged state is separate."""
    from custom_components.iletcomfort.const import DOMAIN
    from custom_components.iletcomfort.diagnostics import (
        async_get_config_entry_diagnostics,
    )
    from custom_components.iletcomfort.mqtt import decode_push_energy_payload

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    energy_body = decode_push_energy_payload(
        json.dumps(
            {"messageType": "status", "data": {"commandHex": HYUNDAI_MQTT_0X04_HEX}}
        )
    )
    assert energy_body is not None
    coord._apply_push_energy(energy_body)

    sentinel = bytearray(energy_body)
    sentinel[2:6] = b"\xff\xff\xff\xff"
    sentinel[6:10] = b"\x00\x00\x00\x00"
    sentinel[10] = 0x7F
    coord._apply_push_energy(bytes(sentinel))

    assert coord.data["raw_frames"][0x04] == bytes(sentinel)
    assert coord.data["state"].raw_frames[0x04] == bytes(sentinel)
    assert coord.data["status"].total_kwh == 14126
    assert coord.data["status"].total_thermal_kwh == 32181
    assert coord.data["sensors"].t4_temp == 0.0

    hass.data.setdefault(DOMAIN, {})[coord.entry.entry_id] = coord
    diag = await async_get_config_entry_diagnostics(hass, coord.entry)
    protocol = diag["protocol"]
    assert protocol["raw_frames"]["0x04"] == bytes(sentinel).hex(",")
    assert protocol["atw_energy_notify_merged"] == bytes(energy_body).hex(",")

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


# --- Push re-runs the profile sensors decode on the retained sensors ---------


async def test_atw_status_push_th_temp_follows_pushed_tank_temp_issue225(hass):
    from custom_components.iletcomfort.model_profiles import decode_atw_status

    coord = _atw_phone_app_coordinator(hass)
    coord.data = _polled_atw_snapshot()
    assert coord.data["sensors"].th_temp == 50.0

    pushed = bytearray(_ATW_STATUS_SILENT_ON)
    pushed[22] = 47  # DHW tank °C
    coord._apply_push_status(decode_atw_status(bytes(pushed)))
    assert coord.data["sensors"].th_temp == 47.0

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_aquapura_status_push_th_temp_follows_box_bottom_issue225(hass):
    from custom_components.iletcomfort.api import ITSSensors, decode_its_status
    from custom_components.iletcomfort.model_profiles import AQUAPURA_SN8

    frame = bytearray(bytes.fromhex(
        "0123232323232323232323232323232323" "4b" + "23" * 32
    ))
    coord = _atw_phone_app_coordinator(hass, AQUAPURA_SN8)
    coord.data = {
        "status": decode_its_status(bytes(frame)),
        "sensors": ITSSensors(th_temp=40.0, twin_temp=0.0),
        "raw_frames": {0x01: bytes(frame)},
    }

    frame[17] = 0x50  # 80 - 35 = 45 °C
    coord._apply_push_status(decode_its_status(bytes(frame)))
    assert coord.data["status"].box_bottom_temp == 45.0
    assert coord.data["sensors"].th_temp == 45.0
    assert coord.data["sensors"].twin_temp == 0.0

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_kjrh120l_status_push_t4_follows_pushed_outdoor_issue225(hass):
    from custom_components.iletcomfort.api import ITSSensors, decode_its_status
    from custom_components.iletcomfort.model_profiles import KJRH120L_SN8
    from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A

    coord = _atw_phone_app_coordinator(hass, KJRH120L_SN8)
    coord.data = {
        "status": decode_its_status(KJRH120L_SNAPSHOT_A),
        "sensors": ITSSensors(t4_temp=12.0),
        "raw_frames": {0x01: KJRH120L_SNAPSHOT_A},
    }

    pushed = bytearray(KJRH120L_SNAPSHOT_A)
    pushed[57] = 0xCA  # 202 - 196 = 6 °C
    coord._apply_push_status(decode_its_status(bytes(pushed)))
    assert coord.data["sensors"].t4_temp == 6.0

    pushed[57] = 0xFF  # missing
    coord._apply_push_status(decode_its_status(bytes(pushed)))
    assert coord.data["sensors"].t4_temp is None

    pushed[57] = 0x00  # far out of the valid range
    coord._apply_push_status(decode_its_status(bytes(pushed)))
    assert coord.data["sensors"].t4_temp is None

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()


async def test_standard_status_push_leaves_sensors_unchanged_issue225(hass):
    from custom_components.iletcomfort.api import ITSSensors, ITSStatus

    coord = _atw_phone_app_coordinator(hass, None)
    sensors = ITSSensors(twin_temp=21.0, th_temp=48.0, t4_temp=3.0)
    coord.data = {"status": ITSStatus(mode=0), "sensors": sensors, "raw_frames": {}}

    coord._apply_push_status(ITSStatus(mode=1, box_bottom_temp=99.0))
    assert coord.data["sensors"] is sensors
    assert coord.data["status"].mode == 1

    coord._cancel_push_status_watchdog()
    await coord.async_shutdown()
