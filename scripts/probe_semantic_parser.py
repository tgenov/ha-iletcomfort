#!/usr/bin/env python3
"""Safely probe an undocumented model-aware semantic parser (development only).

The tool talks only to the explicitly configured semantic parser. It has no
Dollin appliance-control transport and never forwards a generated frame.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import requests

DEFAULT_TIMEOUT = 10.0
DEVICE_TYPE = "0xC3"
DEVICE_SUBTYPE = "0"


class SemanticParserError(Exception):
    """Base class for a safely classified semantic-parser failure."""


class ParserTransportError(SemanticParserError):
    def __init__(self, category: Literal["timeout", "transport"]) -> None:
        self.category = category
        super().__init__(category)


class ParserHttpError(SemanticParserError):
    """The parser returned a non-success HTTP status."""


class ParserVendorError(SemanticParserError):
    """The parser returned a nonzero vendor result code."""


class ParserMalformedResponse(SemanticParserError):
    """The parser response was not the expected JSON shape."""


class FrameValidationError(SemanticParserError):
    """A raw or generated frame failed local C3 validation."""


def _frame_bytes(frame: str) -> bytes:
    compact = frame.strip().replace(",", "").replace(" ", "")
    if not compact or len(compact) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", compact):
        raise FrameValidationError("frame is not valid hexadecimal")
    return bytes.fromhex(compact)


def validate_c3_frame(frame: str) -> str:
    """Validate and return a normalized C3 frame without interpreting fields."""
    raw = _frame_bytes(frame)
    if len(raw) < 12:
        raise FrameValidationError("invalid C3 response shape: fewer than 12 bytes")
    if raw[0] != 0xAA:
        raise FrameValidationError("invalid C3 response shape: missing AA header")
    if raw[2] != 0xC3:
        raise FrameValidationError("frame is not for C3 device type")
    if raw[1] != len(raw) - 1:
        raise FrameValidationError(
            f"declared length {raw[1]} does not match frame length {len(raw) - 1}"
        )
    if sum(raw[1:]) & 0xFF:
        raise FrameValidationError("C3 checksum mismatch")
    if not raw[10:-1]:
        raise FrameValidationError("invalid C3 response shape: empty body")
    return raw.hex()


def build_envelope(
    *,
    sn8: str,
    direction: Literal["query", "control"],
    operation: dict[str, Any],
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9]{8}", sn8):
        raise ValueError("sn8 must be exactly eight alphanumeric characters")
    return {
        "deviceinfo": {
            "deviceSubType": DEVICE_SUBTYPE,
            "deviceType": DEVICE_TYPE,
            "modelSN8": sn8,
        },
        direction: operation,
    }


class SemanticParserClient:
    """Small interface around parser transport, response checks, and frame validation."""

    def __init__(
        self,
        base_url: str,
        *,
        encode_path: str = "/encode",
        decode_path: str = "/decode",
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        parsed_url = urlsplit(base_url)
        if (
            parsed_url.scheme != "https"
            or not parsed_url.hostname
            or parsed_url.username
            or parsed_url.password
            or parsed_url.query
            or parsed_url.fragment
        ):
            raise ValueError("base URL must be a credential-free HTTPS URL")
        if timeout <= 0 or timeout > 60:
            raise ValueError("timeout must be greater than zero and at most 60 seconds")
        self._base_url = base_url.rstrip("/")
        self._encode_path = "/" + encode_path.lstrip("/")
        self._decode_path = "/" + decode_path.lstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    def _post(self, path: str, envelope: dict[str, Any]) -> dict[str, Any]:
        try:
            response = self._session.post(
                self._base_url + path,
                json=envelope,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.Timeout as err:
            raise ParserTransportError("timeout") from err
        except requests.RequestException as err:
            raise ParserTransportError("transport") from err
        if not 200 <= response.status_code < 300:
            raise ParserHttpError(f"HTTP {response.status_code}")
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as err:
            raise ParserMalformedResponse("response is not JSON") from err
        if not isinstance(payload, dict):
            raise ParserMalformedResponse("response root is not an object")
        if "code" not in payload or not isinstance(payload["code"], int):
            raise ParserMalformedResponse("response has no numeric vendor code")
        code = payload["code"]
        if code != 0:
            raise ParserVendorError(f"vendor code {code!r}")
        data = payload.get("data")
        if not isinstance(data, dict) or not data:
            raise ParserMalformedResponse("response data is empty or not an object")
        return data

    def encode(
        self,
        *,
        sn8: str,
        direction: Literal["query", "control"],
        operation: dict[str, Any],
    ) -> str:
        envelope = build_envelope(sn8=sn8, direction=direction, operation=operation)
        data = self._post(self._encode_path, envelope)
        frame = data.get("commandHex")
        if not isinstance(frame, str):
            raise ParserMalformedResponse("encode data has no commandHex string")
        return validate_c3_frame(frame)

    def decode(self, *, sn8: str, frame: str) -> dict[str, Any]:
        normalized = validate_c3_frame(frame)
        envelope = build_envelope(
            sn8=sn8, direction="query", operation={"commandHex": normalized}
        )
        data = self._post(self._decode_path, envelope)
        status = data.get("status")
        if not isinstance(status, dict) or not status:
            raise ParserMalformedResponse("decode data has no nonempty status object")
        return status


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate an authorized semantic encode/decode probe without appliance transport."
    )
    parser.add_argument("operation", choices=("encode-query", "encode-control", "decode"))
    parser.add_argument("--sn8", required=True, help="eight-character model code")
    parser.add_argument("--base-url", required=True, help="undocumented parser base URL")
    parser.add_argument("--encode-path", default="/encode")
    parser.add_argument("--decode-path", default="/decode")
    parser.add_argument("--input", required=True, type=Path, help="authorized local JSON input")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    return parser


def _load_input(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as err:
        raise ValueError("input is not readable JSON") from err


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        value = _load_input(args.input)
        client = SemanticParserClient(
            args.base_url,
            encode_path=args.encode_path,
            decode_path=args.decode_path,
            timeout=args.timeout,
        )
        if args.operation == "decode":
            if not isinstance(value, dict) or not isinstance(value.get("commandHex"), str):
                raise ValueError("decode input must be an object containing commandHex")
            status = client.decode(sn8=args.sn8, frame=value["commandHex"])
            names = sorted(str(name) for name in status)
            digest = hashlib.sha256("\n".join(names).encode()).hexdigest()[:12]
            print(f"Validated semantic status shape: {len(names)} fields (schema {digest})")
        else:
            if not isinstance(value, dict) or not value:
                raise ValueError("encode input must be a nonempty semantic object")
            direction: Literal["query", "control"] = (
                "query" if args.operation == "encode-query" else "control"
            )
            frame = client.encode(sn8=args.sn8, direction=direction, operation=value)
            print(f"Received validated C3 frame ({len(bytes.fromhex(frame))} bytes)")
        return 0
    except (SemanticParserError, ValueError) as err:
        category = (
            f"transport:{err.category}"
            if isinstance(err, ParserTransportError)
            else type(err).__name__
        )
        print(f"Probe failed safely: {category}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
