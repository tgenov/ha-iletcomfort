#!/usr/bin/env python3
"""Read-only C3 probe for sn8 171H120F units via the iLetComfort cloud.

Diagnostics tool. It only sends C3 *queries* (message type 0x03) — it cannot
send control (SET) frames. Every request/response pair is appended to
``captures/c3_probe.jsonl`` (git-ignored) without account data, tokens or the
full appliance code.

Credentials are read from ``.env`` in the repository root (git-ignored), using
exactly these keys (no other key is ever read or sent anywhere)::

    ILET_EMAIL=...
    ILET_PASSWORD=...
    ILET_REGION=eu        # or us

Values are never printed. The script exits if a key is missing.

Usage (the Home Assistant integration must be disabled meanwhile: the cloud
allows one active session per account):
    python scripts/c3_probe.py status --label "heating on, dhw on"
    python scripts/c3_probe.py probe  --label "baseline"
    python scripts/c3_probe.py decode "<hex frame>"      # offline, no login
"""

from __future__ import annotations

import argparse
import json
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CAPTURE_FILE = ROOT / "captures" / "c3_probe.jsonl"

sys.path.insert(0, str(ROOT))
# Import the protocol modules without Home Assistant (same trick as
# scripts/capture_mqtt.py).
_components = types.ModuleType("custom_components")
_components.__path__ = [str(ROOT / "custom_components")]
_iletcomfort = types.ModuleType("custom_components.iletcomfort")
_iletcomfort.__path__ = [str(ROOT / "custom_components" / "iletcomfort")]
sys.modules.setdefault("custom_components", _components)
sys.modules.setdefault("custom_components.iletcomfort", _iletcomfort)

from custom_components.iletcomfort.api import (  # noqa: E402
    ApiError,
    AuthError,
    ILetComfortClient,
    build_c3_query,
    mask_identifier,
    parse_hex_response,
)
from custom_components.iletcomfort.const import REGION_URLS  # noqa: E402

TARGET_SN8 = "171H120F"

# Query body types from midea-local (midealocal/devices/c3/message.py).
# 0x02 is the subtype the integration polls today (placeholder on this model);
# 0x04 is a notify1 body (energy) in midea-local — tried as a query out of
# curiosity, the cloud may reject it.
PROBE_BODY_TYPES: dict[int, str] = {
    0x01: "basic",
    0x02: "its_sensors",
    0x04: "energy",
    0x05: "silence",
    0x07: "eco",
    0x08: "install",
    0x09: "disinfect",
    0x0A: "hmi_para",
    0x10: "unit_para",
}


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------

def split_frame(raw: bytearray) -> dict[str, Any]:
    """Split an AA frame into header fields and body (body[0] = body type)."""
    if len(raw) < 12 or raw[0] != 0xAA:
        return {"valid": False}
    checksum_ok = ((~sum(raw[1:-1]) + 1) & 0xFF) == raw[-1]
    return {
        "valid": True,
        "length": raw[1],
        "device_type": raw[2],
        "protocol_version": raw[8],
        "message_type": raw[9],
        "body": raw[10:-1],
        "checksum_ok": checksum_ok,
    }


def _s8(value: int) -> int:
    return value - 256 if value > 127 else value


def _u32(b: bytearray, i: int) -> int:
    return (b[i] << 24) | (b[i + 1] << 16) | (b[i + 2] << 8) | b[i + 3]


# ---------------------------------------------------------------------------
# Decoders (midea-local layouts, data_offset = 1)
# ---------------------------------------------------------------------------

def decode_basic(body: bytearray) -> dict[str, Any]:
    """C3BasicBody, data_offset=1."""
    d = 1
    if len(body) < d + 23:
        return {"error": f"body too short for basic layout ({len(body)} bytes)"}
    b1, b2, b3 = body[d], body[d + 1], body[d + 2]
    out: dict[str, Any] = {
        "zone1_power (heating switch)": bool(b1 & 0x01),
        "zone2_power": bool(b1 & 0x02),
        "dhw_power (DHW switch)": bool(b1 & 0x04),
        "zone1_curve": bool(b1 & 0x08),
        "zone2_curve": bool(b1 & 0x10),
        "tbh": bool(b1 & 0x20),
        "fast_dhw": bool(b1 & 0x40),
        "remote_onoff": bool(b1 & 0x80),
        "cap_heat": bool(b2 & 0x01),
        "cap_cool": bool(b2 & 0x02),
        "cap_dhw": bool(b2 & 0x04),
        "cap_double_zone": bool(b2 & 0x08),
        "zone1_temp_type_water": bool(b2 & 0x10),
        "zone2_temp_type_water": bool(b2 & 0x20),
        "room_thermostat_support": bool(b2 & 0x40),
        "room_thermostat_state": bool(b2 & 0x80),
        "time_set": bool(b3 & 0x01),
        "silent_mode": bool(b3 & 0x02),
        "holiday_on": bool(b3 & 0x04),
        "eco_mode": bool(b3 & 0x08),
        "byte3_raw": f"0x{b3:02x}",
        "mode": {2: "COOL", 3: "HEAT"}.get(body[d + 3], f"raw {body[d + 3]}"),
        "mode_auto": body[d + 4],
        "zone1_target_C": body[d + 5],
        "zone2_target_C": body[d + 6],
        "dhw_target_C": body[d + 7],
        "room_target_C": body[d + 8] / 2,
        "zone1_heat_max/min_C": (body[d + 9], body[d + 10]),
        "zone1_cool_max/min_C": (body[d + 11], body[d + 12]),
        "zone2_heat_max/min_C": (body[d + 13], body[d + 14]),
        "zone2_cool_max/min_C": (body[d + 15], body[d + 16]),
        "room_max/min_C": (body[d + 17] / 2, body[d + 18] / 2),
        "dhw_max/min_C": (body[d + 19], body[d + 20]),
        "dhw_tank_actual_C": body[d + 21],
        "error_code": body[d + 22],
    }
    if len(body) > d + 23:
        b = body[d + 23]
        out["tbh_control"] = bool(b & 0x80)
        out["sys_energy_ana_en"] = bool(b & 0x20)
        out["hmi_energy_ana_set_en"] = bool(b & 0x40)
    return out


def decode_energy(body: bytearray) -> dict[str, Any]:
    d = 1
    if len(body) < d + 14:
        return {"error": f"body too short for energy layout ({len(body)} bytes)"}
    s = body[d]
    return {
        "status_heating": bool(s & 0x01),
        "status_cool": bool(s & 0x02),
        "status_dhw": bool(s & 0x04),
        "status_tbh": bool(s & 0x08),
        "status_ibh": bool(s & 0x10),
        "total_energy_consumption": _u32(body, d + 1),
        "total_produced_energy": _u32(body, d + 5),
        "outdoor_temp_C": _s8(body[d + 9]),
        "zone1_temp_set": body[d + 10],
        "zone2_temp_set": body[d + 11],
        "t5s": body[d + 12],
        "tas": body[d + 13],
    }


def decode_silence(body: bytearray) -> dict[str, Any]:
    d = 1
    if len(body) < d + 1:
        return {"error": "empty"}
    b = body[d]
    return {
        "silent_on": bool(b & 0x01),
        "timer1_on": bool(b & 0x02),
        "timer2_on": bool(b & 0x04),
        "super_silent_level": bool(b & 0x08),
        "raw_tail": body[d + 1:].hex(" "),
    }


def decode_eco(body: bytearray) -> dict[str, Any]:
    d = 1
    if len(body) < d + 1:
        return {"error": "empty"}
    return {
        "eco_on": bool(body[d] & 0x01),
        "eco_timer_on": bool(body[d] & 0x02),
        "raw_tail": body[d + 1:].hex(" "),
    }


def decode_disinfect(body: bytearray) -> dict[str, Any]:
    d = 1
    if len(body) < d + 4:
        return {"error": f"body too short ({len(body)} bytes)"}
    return {
        "disinfect_on": bool(body[d] & 0x01),
        "disinfect_running": bool(body[d] & 0x02),
        "weekday": body[d + 1],
        "start": f"{body[d + 2]:02d}:{body[d + 3]:02d}",
    }


def decode_unit_para(body: bytearray) -> dict[str, Any]:
    d = 1
    if len(body) < d + 64:
        return {"error": f"body too short for unit_para layout ({len(body)} bytes)"}
    out: dict[str, Any] = {
        "comp_freq_Hz": body[d],
        "unit_mode_run": body[d + 1],
        "fan_speed_rpm": body[d + 2] * 10,
        "capacity_need": body[d + 5],
        "T3_coil_C": _s8(body[d + 6]),
        "T4_outdoor_C": _s8(body[d + 7]),
        "Tp_discharge_C": body[d + 8],
        "Tw_in_C": _s8(body[d + 9]),
        "Tw_out_C": _s8(body[d + 10]),
        "Tsolar_C": _s8(body[d + 11]),
        "odu_voltage_V": body[d + 17] * 256 + body[d + 18],
        "exv": body[d + 19] * 256 + body[d + 20],
        "T1_C": _s8(body[d + 33]),
        "Tw2_C": _s8(body[d + 34]),
        "T2_C": _s8(body[d + 35]),
        "T2B_C": _s8(body[d + 36]),
        "T5_tank_C": _s8(body[d + 37]),
        "Ta_room_C": _s8(body[d + 38]),
        "pressure_high": body[d + 42] * 256 + body[d + 43],
        "pressure_low": body[d + 44] * 256 + body[d + 45],
        "Th_suction_C": _s8(body[d + 46]),
        "dc_current": body[d + 49],
        "Tf_module_C": _s8(body[d + 51]),
        "water_flow": body[d + 54] * 256 + body[d + 55],
        "water_pressure": body[d + 61] * 256 + body[d + 62],
    }
    if len(body) > d + 85:
        out.update({
            "total_electricity": _u32(body, d + 66),
            "total_thermal": _u32(body, d + 70),
            "heat_elec_total_consum": _u32(body, d + 74),
            "heat_elec_total_capacity": _u32(body, d + 78),
            "instant_power": (body[d + 82] << 8) | body[d + 83],
            "instant_renew_power": (body[d + 84] << 8) | body[d + 85],
        })
    return out


DECODERS = {
    0x01: decode_basic,
    0x04: decode_energy,
    0x05: decode_silence,
    0x07: decode_eco,
    0x09: decode_disinfect,
    0x10: decode_unit_para,
}


def describe_response(response_hex: str) -> dict[str, Any]:
    """Parse a cloud response hex into header info + decoded body."""
    try:
        raw = parse_hex_response(response_hex)
    except ValueError as err:
        return {"parse_error": str(err)}
    info = split_frame(raw)
    if not info.get("valid"):
        return {"parse_error": "not an AA frame", "len": len(raw)}
    body: bytearray = info["body"]
    body_type = body[0] if body else None
    result: dict[str, Any] = {
        "message_type": f"0x{info['message_type']:02x}",
        "protocol_version": info["protocol_version"],
        "checksum_ok": info["checksum_ok"],
        "body_type": f"0x{body_type:02x}" if body_type is not None else None,
        "body_len": len(body),
        "body_hex": body.hex(" "),
    }
    decoder = DECODERS.get(body_type) if body_type is not None else None
    if decoder:
        result["decoded"] = decoder(body)
    return result


# ---------------------------------------------------------------------------
# Credentials / session
# ---------------------------------------------------------------------------

ENV_ACCOUNT = "ILET_EMAIL"
ENV_PASSWORD = "ILET_PASSWORD"
ENV_REGION = "ILET_REGION"


def load_env() -> tuple[str, str, str]:
    """Read the three documented keys from ``.env``; nothing else is used."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        sys.exit("Missing .env in the repository root.")
    wanted = {ENV_ACCOUNT, ENV_PASSWORD, ENV_REGION}
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        if key in wanted:
            values[key] = value.strip().strip('"').strip("'")
    missing = sorted(k for k in (ENV_ACCOUNT, ENV_PASSWORD, ENV_REGION) if not values.get(k))
    if missing:
        sys.exit(
            f".env is missing {', '.join(missing)} "
            f"(expected {ENV_ACCOUNT}, {ENV_PASSWORD}, {ENV_REGION}=eu|us). "
            "Values are never printed."
        )
    region = values[ENV_REGION].lower()
    if region not in REGION_URLS:
        sys.exit(f"Unknown {ENV_REGION} {region!r}; use one of {sorted(REGION_URLS)}.")
    return values[ENV_ACCOUNT], values[ENV_PASSWORD], region


def connect() -> tuple[ILetComfortClient, str, dict[str, Any]]:
    account, password, region = load_env()
    client = ILetComfortClient(api_base=REGION_URLS[region])
    client.login(account, password)
    appliances = client.list_appliances()
    print(f"Logged in (region={region}); {len(appliances)} appliance(s):")
    chosen: dict[str, Any] | None = None
    for app in appliances:
        print(
            f"  type={app.get('applianceType')} sn8={app.get('sn8')} "
            f"online={app.get('onlineStatus')} "
            f"code={mask_identifier(str(app.get('applianceCode', '')))}"
        )
        if chosen is None and app.get("sn8") == TARGET_SN8:
            chosen = app
    if chosen is None:
        sys.exit(f"No appliance with sn8 {TARGET_SN8} on this account.")
    return client, str(chosen["applianceCode"]), chosen


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def record(label: str, body_type: int, command: str, response: str | None,
           error: str | None) -> None:
    CAPTURE_FILE.parent.mkdir(exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "sn8": TARGET_SN8,
        "query_body_type": f"0x{body_type:02x}",
        "command": command,
        "response": response,
        "error": error,
    }
    with CAPTURE_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def run_query(client: ILetComfortClient, code: str, body_type: int,
              label: str) -> None:
    name = PROBE_BODY_TYPES.get(body_type, "?")
    command = build_c3_query(body_type)
    print(f"\n=== query 0x{body_type:02x} ({name}) -> {command}")
    try:
        try:
            response = client.send_hex_command(code, command)
        except AuthError:
            # Another login (HA, phone app) evicted our session; re-auth once.
            print("  session evicted (14005/12001) - logging in again")
            account, password, _region = load_env()
            client.login(account, password)
            response = client.send_hex_command(code, command)
    except (ApiError, AuthError) as err:
        print(f"  REJECTED: {err}")
        record(label, body_type, command, None, str(err))
        return
    record(label, body_type, command, response, None)
    print(f"  raw: {response}")
    print(json.dumps(describe_response(response), indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_status = sub.add_parser("status", help="query 0x01 and decode")
    p_status.add_argument("--label", default="")
    p_probe = sub.add_parser("probe", help="query every known body type")
    p_probe.add_argument("--label", default="")
    p_probe.add_argument(
        "--types", default=",".join(f"{t:02x}" for t in PROBE_BODY_TYPES),
        help="comma-separated hex body types (default: all known)",
    )
    p_decode = sub.add_parser("decode", help="decode a hex frame offline")
    p_decode.add_argument("hex")
    args = parser.parse_args()

    if args.cmd == "decode":
        print(json.dumps(describe_response(args.hex), indent=2, ensure_ascii=False))
        return

    client, code, _meta = connect()
    if args.cmd == "status":
        run_query(client, code, 0x01, args.label)
    else:
        for t in (int(x, 16) for x in args.types.split(",")):
            run_query(client, code, t, args.label)
    print(f"\nSaved to {CAPTURE_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
