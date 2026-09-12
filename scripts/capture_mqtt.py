#!/usr/bin/env python3
"""Enumerate an iLetComfort account and capture one appliance's MQTT traffic.

The 1Password item JSON is read on stdin.  This utility never prints the
account password, access token, private key, or certificate.  MQTT protocol
keep-alives are recorded from Paho's debug callbacks separately from
application payloads.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Import the protocol modules without importing Home Assistant's integration
# entry point; this standalone capture tool only needs api/const/mqtt.
_components = types.ModuleType("custom_components")
_components.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components")]
_iletcomfort = types.ModuleType("custom_components.iletcomfort")
_iletcomfort.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components" / "iletcomfort")]
sys.modules.setdefault("custom_components", _components)
sys.modules.setdefault("custom_components.iletcomfort", _iletcomfort)

from custom_components.iletcomfort.api import ILetComfortClient
from custom_components.iletcomfort.const import REGION_URLS


def _item_fields(item: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in item.get("fields", []):
        if not isinstance(field, dict):
            continue
        value = field.get("value")
        if not isinstance(value, str):
            continue
        for key in (field.get("id"), field.get("label")):
            if isinstance(key, str) and key:
                result[key.lower()] = value
    return result


def _credential_pair() -> tuple[str, str]:
    try:
        item = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as err:
        raise RuntimeError("could not read 1Password item JSON from stdin") from err
    fields = _item_fields(item)
    account = fields.get("username") or fields.get("email")
    password = fields.get("password")
    if not account or not password:
        raise RuntimeError("1Password item must contain username/email and password fields")
    return account, password


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _device_summary(device: dict[str, Any]) -> dict[str, Any]:
    return {
        "applianceCode": device.get("applianceCode"),
        "applianceName": device.get("applianceName"),
        "sn8": device.get("sn8"),
        "applianceType": device.get("applianceType"),
        "online": device.get("online"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", choices=sorted(REGION_URLS), default="us")
    parser.add_argument("--device", help="exact applianceCode; required when more than one device exists")
    parser.add_argument("--hours", type=float, default=8.0)
    parser.add_argument("--output", type=Path, default=Path("mqtt-capture.jsonl"))
    parser.add_argument("--enumerate-only", action="store_true")
    args = parser.parse_args()
    if args.hours <= 0:
        parser.error("--hours must be positive")

    account, password = _credential_pair()
    client = ILetComfortClient(api_base=REGION_URLS[args.region], timeout=30)
    client.login(account, password)
    devices = client.list_appliances()
    print(json.dumps({"event": "enumerated", "devices": [_device_summary(d) for d in devices]}, sort_keys=True))
    if args.enumerate_only:
        return 0
    if not devices:
        raise RuntimeError("account has no appliances")
    if args.device:
        selected = next((d for d in devices if d.get("applianceCode") == args.device), None)
        if selected is None:
            raise RuntimeError("--device did not match an enumerated applianceCode")
    elif len(devices) == 1:
        selected = devices[0]
    else:
        raise RuntimeError("more than one appliance; rerun with --device <applianceCode>")

    appliance_code = selected.get("applianceCode")
    if not isinstance(appliance_code, str) or not appliance_code:
        raise RuntimeError("selected appliance has no applianceCode")
    import paho.mqtt.client as mqtt

    from custom_components.iletcomfort.mqtt import push_topics

    cert = client.create_app_cert()
    topic_list = push_topics(args.region, appliance_code)
    # #48 reported this vendor heartbeat shape for another Dollin appliance
    # family. C3 normally uses the midea/dev shape above, but probing this
    # exact appliance-scoped candidate is necessary to verify whether the C3
    # unit also exposes an hbt stream. Never widen this to a wildcard.
    topic_list.append(f"{args.region}/{args.region}_{appliance_code}/hbt")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    capture = args.output.open("a", encoding="utf-8", buffering=1)
    mqtt_client = mqtt.Client()
    # Paho requires the certificate paths for mTLS; use the same private temp
    # file approach as the integration without retaining secrets after exit.
    import tempfile
    cert_dir = tempfile.TemporaryDirectory(prefix="iletcomfort_capture_")
    cert_path = Path(cert_dir.name) / "client.crt"
    key_path = Path(cert_dir.name) / "client.key"
    cert_path.write_text(cert.certificate_pem, encoding="utf-8")
    key_path.write_text(cert.private_key, encoding="utf-8")
    cert_path.chmod(0o600)
    key_path.chmod(0o600)
    mqtt_client.tls_set(certfile=str(cert_path), keyfile=str(key_path))

    def record(event: str, **data: Any) -> None:
        capture.write(json.dumps({"timestamp": _stamp(), "event": event, **data}, sort_keys=True) + "\n")

    def on_connect(c: mqtt.Client, userdata: Any, flags: Any, rc: int, properties: Any = None) -> None:
        record("connect", rc=rc)
        if rc == 0:
            for topic in topic_list:
                c.subscribe(topic, qos=0)
                record("subscribe", topic=topic)

    def on_disconnect(c: mqtt.Client, userdata: Any, rc: int, properties: Any = None) -> None:
        record("disconnect", rc=rc)

    def on_message(c: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        payload = message.payload.decode("utf-8", "replace")
        record("application_message", topic=message.topic, qos=message.qos, payload=payload)

    def on_log(c: mqtt.Client, userdata: Any, level: int, buf: str) -> None:
        upper = buf.upper()
        if "PINGREQ" in upper or "PINGRESP" in upper:
            record("mqtt_keepalive", detail=buf)
        elif "CONNECT" in upper or "DISCONNECT" in upper:
            record("mqtt_protocol", detail=buf)

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message
    mqtt_client.on_log = on_log
    print(json.dumps({"event": "capturing", "device": _device_summary(selected), "topics": topic_list, "output": str(args.output), "hours": args.hours}, sort_keys=True))
    try:
        mqtt_client.connect(cert.endpoint, cert.port or 8883, keepalive=60)
        mqtt_client.loop_start()
        time.sleep(args.hours * 3600)
    finally:
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        capture.close()
        cert_dir.cleanup()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as err:  # concise operational failure; no credential reprs
        print(f"capture failed: {err}", file=sys.stderr)
        raise SystemExit(1)
