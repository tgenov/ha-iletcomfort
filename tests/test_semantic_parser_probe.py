"""Offline behavior tests for the development-only semantic parser probe."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
import requests

from custom_components.iletcomfort.api import build_c3_query


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "probe_semantic_parser.py"
    spec = importlib.util.spec_from_file_location("probe_semantic_parser", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["probe_semantic_parser"] = module
    spec.loader.exec_module(module)
    return module


probe = _load_script()


class FakeResponse:
    def __init__(self, payload=None, *, status=200, json_error: Exception | None = None):
        self.payload = payload
        self.status_code = status
        self.json_error = json_error

    def json(self):
        if self.json_error:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, response=None, error: Exception | None = None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_encode_control_uses_observed_envelope_and_validates_frame() -> None:
    frame = build_c3_query(1)
    session = FakeSession(FakeResponse({"code": 0, "data": {"commandHex": frame}}))
    client = probe.SemanticParserClient("https://parser.example.invalid", session=session)

    result = client.encode(
        sn8="171000AU",
        direction="control",
        operation={"control_type": "base", "dhw_temp_set": "27"},
    )

    assert result == frame
    assert session.calls == [
        (
            "https://parser.example.invalid/encode",
            {
                "json": {
                    "deviceinfo": {
                        "deviceSubType": "0",
                        "deviceType": "0xC3",
                        "modelSN8": "171000AU",
                    },
                    "control": {"control_type": "base", "dhw_temp_set": "27"},
                },
                "timeout": 10.0,
                "allow_redirects": False,
            },
        )
    ]


def test_decode_validates_input_frame_and_returns_semantic_object() -> None:
    frame = build_c3_query(2)
    session = FakeSession(
        FakeResponse({"code": 0, "data": {"status": {"query_type": "base", "dhw_temp": 40}}})
    )
    client = probe.SemanticParserClient("https://parser.example.invalid", session=session)

    result = client.decode(sn8="171000AU", frame=frame)

    assert result == {"query_type": "base", "dhw_temp": 40}
    assert session.calls[0][1]["json"]["query"] == {"commandHex": frame}


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        ("not-hex", "hexadecimal"),
        ("aa0bc10000000000000301f0", "C3"),
        ("aa0cc30000000000000301f0", "declared length"),
        ("aa0bc30000000000000301ff", "checksum"),
        ("aac3", "response shape"),
    ],
)
def test_frame_validation_rejects_untrusted_results(frame: str, message: str) -> None:
    with pytest.raises(probe.FrameValidationError, match=message):
        probe.validate_c3_frame(frame)


@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (FakeResponse({}, status=503), probe.ParserHttpError),
        (FakeResponse({"code": 1800, "msg": "rejected"}), probe.ParserVendorError),
        (FakeResponse({"data": {"commandHex": build_c3_query(1)}}), probe.ParserMalformedResponse),
        (FakeResponse(json_error=ValueError("bad json")), probe.ParserMalformedResponse),
        (FakeResponse({"code": 0, "data": None}), probe.ParserMalformedResponse),
    ],
)
def test_failures_are_classified(response, error_type) -> None:
    client = probe.SemanticParserClient(
        "https://parser.example.invalid", session=FakeSession(response)
    )

    with pytest.raises(error_type):
        client.encode(sn8="171000AU", direction="query", operation={"query_type": "base"})


@pytest.mark.parametrize(
    ("transport_error", "category"),
    [
        (requests.Timeout("late"), "timeout"),
        (requests.ConnectionError("offline"), "transport"),
    ],
)
def test_transport_failures_are_bounded_and_classified(transport_error, category) -> None:
    client = probe.SemanticParserClient(
        "https://parser.example.invalid", timeout=0.25, session=FakeSession(error=transport_error)
    )

    with pytest.raises(probe.ParserTransportError) as excinfo:
        client.encode(sn8="171000AU", direction="query", operation={"query_type": "base"})

    assert excinfo.value.category == category


def test_cli_output_redacts_semantic_values_frames_and_urls(tmp_path: Path, monkeypatch, capsys) -> None:
    input_path = tmp_path / "private-input.json"
    input_path.write_text(
        json.dumps({"control_type": "base", "dhw_temp_set": "SECRET-VALUE"}),
        encoding="utf-8",
    )
    frame = build_c3_query(1)

    monkeypatch.setattr(
        probe.SemanticParserClient,
        "encode",
        lambda self, **kwargs: frame,
    )
    result = probe.main(
        [
            "encode-control",
            "--sn8",
            "171000AU",
            "--base-url",
            "https://parser.example.invalid",
            "--input",
            str(input_path),
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "SECRET" not in output
    assert frame not in output
    assert "private-input" not in output
    assert "validated C3 frame" in output


def test_probe_has_no_appliance_control_transport() -> None:
    source = (Path(__file__).parent.parent / "scripts" / "probe_semantic_parser.py").read_text(
        encoding="utf-8"
    )

    assert "ILetComfortClient" not in source
    assert '"/appliance/control/hexadecimal"' not in source


def test_plain_http_parser_endpoint_is_refused() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        probe.SemanticParserClient("http://parser.example.invalid")


def test_cli_distinguishes_timeout_without_leaking_failure_data(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    input_path = tmp_path / "device-serial-SECRET.json"
    input_path.write_text(json.dumps({"query_type": "private", "token": "SECRET"}), encoding="utf-8")

    def fail(*args, **kwargs):
        raise probe.ParserTransportError("timeout")

    monkeypatch.setattr(probe.SemanticParserClient, "encode", fail)
    result = probe.main(
        [
            "encode-query",
            "--sn8",
            "171000AU",
            "--base-url",
            "https://parser.example.invalid",
            "--input",
            str(input_path),
        ]
    )

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert result == 1
    assert "timeout" in captured.err
    assert "SECRET" not in combined
    assert "171000AU" not in combined
    assert "parser.example.invalid" not in combined
