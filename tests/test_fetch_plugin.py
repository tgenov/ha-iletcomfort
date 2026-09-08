"""Tests for the dev-only vendor plugin fetcher (``scripts/fetch_plugin.py``).

The script is intentionally NOT part of the shipped integration, so it is not
importable as a package. It is loaded by path here.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from custom_components.iletcomfort.api import APP_SECRET, IOT_KEY


def _load_script():
    path = Path(__file__).resolve().parent.parent / "scripts" / "fetch_plugin.py"
    spec = importlib.util.spec_from_file_location("fetch_plugin", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_plugin"] = module
    spec.loader.exec_module(module)
    return module


fetch_plugin = _load_script()


# --- request body -----------------------------------------------------------

def test_build_request_defaults_to_the_downgrade_trick() -> None:
    """``version="0.0.0"`` is what makes the server hand back a packageUrl."""
    body = fetch_plugin.build_plugin_request("171H120F")

    assert body["version"] == "0.0.0"
    assert body["type"] == "0xC3"
    assert body["model"] == "171H120F"
    assert body["sdkVersion"] == 20230725
    assert body["firmwareVersion"] == "000000000000"
    assert body["language"] == "en_US"


def test_build_request_honors_overrides() -> None:
    body = fetch_plugin.build_plugin_request(
        "17100003", plugin_type="0xAC", version="1.2.3", sdk_version=1, language="zh_CN",
    )

    assert body["type"] == "0xAC"
    assert body["version"] == "1.2.3"
    assert body["sdkVersion"] == 1
    assert body["language"] == "zh_CN"


# --- signing / headers ------------------------------------------------------

def test_headers_sign_with_the_iot_key_prefix() -> None:
    """PoC: the ``btri`` app-key prefix returns code=3301 Signature Failed."""
    body_json = json.dumps({"model": "17100007"}, separators=(",", ":"))

    headers = fetch_plugin.build_v1_headers(body_json, "TOKEN123")

    expected = hmac.new(
        APP_SECRET.encode("ascii"),
        (IOT_KEY + body_json + headers["random"]).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert headers["sign"] == expected


def test_headers_carry_the_access_token_as_its_own_header() -> None:
    """PoC: the endpoint answers 1105 accessToken[REQUIRE]; Bearer auth is not used."""
    headers = fetch_plugin.build_v1_headers("{}", "TOKEN123")

    assert headers["accessToken"] == "TOKEN123"
    assert not any(k.lower() == "authorization" for k in headers)


# --- response interpretation -------------------------------------------------

def test_extract_package_url_returns_the_url() -> None:
    response = {"code": 0, "data": {"packageUrl": "https://example.invalid/b.zip"}}

    assert fetch_plugin.extract_package_url(response) == "https://example.invalid/b.zip"


def test_extract_package_url_detects_the_up_to_date_branch() -> None:
    """code=0 with data but no packageUrl means "you are already current"."""
    response = {"code": 0, "msg": "success!", "data": {"version": "1.0.65"}}

    with pytest.raises(fetch_plugin.PluginUpToDate):
        fetch_plugin.extract_package_url(response)


@pytest.mark.parametrize(
    ("code", "needle"),
    [
        (2200004, "no plugin"),
        (2200007, "not exist"),
        (1105, "access token"),
        (3301, "signature"),
    ],
)
def test_extract_package_url_explains_known_error_codes(code: int, needle: str) -> None:
    with pytest.raises(fetch_plugin.PluginNotAvailable) as excinfo:
        fetch_plugin.extract_package_url({"code": code, "msg": "vendor msg", "data": None})

    assert needle in str(excinfo.value).lower()
    assert str(code) in str(excinfo.value)


# --- redaction ---------------------------------------------------------------

def test_redact_metadata_strips_the_presigned_url() -> None:
    """packageUrl embeds AWS SigV4 credentials -- it must never be written or printed."""
    meta = {
        "version": "1.0.65",
        "model": "17100007",
        "type": "0xC3",
        "size": "13743895",
        "packageUrl": (
            "https://prod-us-version-file.s3.us-west-2.amazonaws.com/abc.zip"
            "?X-Amz-Credential=AKIAWV3ZAULCG7CYJME6%2F20260908%2Fus-west-2"
            "&X-Amz-Signature=bc0aec165f8c55ba1b74448d6a000ac7"
        ),
        "previewUrl": "https://prod-us-version-file.s3.amazonaws.com/p.png?X-Amz-Signature=deadbeef",
    }

    redacted = fetch_plugin.redact_plugin_metadata(meta)
    dumped = json.dumps(redacted)

    assert "X-Amz-Signature" not in dumped
    assert "X-Amz-Credential" not in dumped
    assert "AKIA" not in dumped
    assert redacted["version"] == "1.0.65"
    assert redacted["model"] == "17100007"


def test_redact_metadata_keeps_a_marker_so_the_field_is_not_silently_dropped() -> None:
    redacted = fetch_plugin.redact_plugin_metadata({"packageUrl": "https://x.invalid/a.zip?X-Amz-Signature=1"})

    assert redacted["packageUrl"] == fetch_plugin.REDACTED


def test_redact_metadata_tolerates_missing_url_fields() -> None:
    assert fetch_plugin.redact_plugin_metadata({"version": "1.0.0"}) == {"version": "1.0.0"}


# --- filenames ---------------------------------------------------------------

def test_bundle_filename_is_model_type_and_version() -> None:
    meta = {"model": "17100007", "type": "0xC3", "version": "1.0.65"}

    assert fetch_plugin.bundle_filename(meta, "17100007") == "17100007_0xC3_v1.0.65.zip"


def test_bundle_filename_falls_back_when_metadata_is_thin() -> None:
    assert fetch_plugin.bundle_filename({}, "171H120F") == "171H120F_0xC3_vunknown.zip"


def test_bundle_filename_rejects_path_traversal_from_vendor_metadata() -> None:
    meta = {"model": "../../etc/passwd", "type": "0xC3", "version": "1.0"}

    name = fetch_plugin.bundle_filename(meta, "17100007")

    assert "/" not in name
    assert ".." not in name


# --- end to end (fake transport) --------------------------------------------

class _FakeResponse:
    def __init__(self, payload=None, content=b"", status=200, headers=None):
        self._payload = payload
        self.content = content
        self.status_code = status
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=8192):
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


PRESIGNED = "https://prod-us-version-file.s3.amazonaws.com/x.zip?X-Amz-Signature=cafebabe"

METADATA_OK = {
    "code": 0,
    "msg": "success!",
    "data": {
        "version": "1.0.65",
        "model": "17100007",
        "type": "0xC3",
        "size": "10",
        "packageUrl": PRESIGNED,
    },
}

ZIP_BYTES = b"PK\x03\x04" + b"payload!!"


@pytest.fixture
def fake_transport(monkeypatch):
    """Intercept every HTTP call the script can make."""
    calls: dict[str, list] = {"post": [], "get": [], "login": []}

    def fake_post(self, url, **kwargs):
        calls["post"].append((url, kwargs))
        if url.endswith("/v1/user/login"):
            calls["login"].append(url)
            return _FakeResponse({"code": 0, "data": {"accessToken": "SECRET-TOKEN", "uid": "SECRET-UID"}})
        return _FakeResponse(METADATA_OK)

    def fake_get(self, url, **kwargs):
        calls["get"].append((url, kwargs))
        return _FakeResponse(content=ZIP_BYTES, headers={"Content-Type": "application/zip"})

    monkeypatch.setattr("requests.Session.post", fake_post, raising=True)
    monkeypatch.setattr("requests.Session.get", fake_get, raising=True)
    monkeypatch.setattr("requests.post", lambda url, **kw: fake_post(None, url, **kw), raising=False)
    monkeypatch.setattr("requests.get", lambda url, **kw: fake_get(None, url, **kw), raising=False)
    return calls


def _env(tmp_path):
    return {
        "ILETCOMFORT_ACCOUNT": "user@example.invalid",
        "ILETCOMFORT_PASSWORD": "hunter2",
    }


def test_main_saves_the_bundle(tmp_path, fake_transport, capsys) -> None:
    rc = fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json")],
        env=_env(tmp_path),
    )

    assert rc == 0
    saved = tmp_path / "17100007_0xC3_v1.0.65.zip"
    assert saved.read_bytes() == ZIP_BYTES


def test_main_never_prints_credentials_token_or_presigned_url(tmp_path, fake_transport, capsys) -> None:
    fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json")],
        env=_env(tmp_path),
    )

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "SECRET-TOKEN" not in combined
    assert "SECRET-UID" not in combined
    assert "hunter2" not in combined
    assert "user@example.invalid" not in combined
    assert "X-Amz-Signature" not in combined
    assert "cafebabe" not in combined


def test_main_writes_no_credentials_into_the_output_dir(tmp_path, fake_transport) -> None:
    fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json"),
         "--save-metadata"],
        env=_env(tmp_path),
    )

    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "t.json":
            blob = path.read_bytes()
            assert b"SECRET-TOKEN" not in blob
            assert b"X-Amz-Signature" not in blob
            assert b"hunter2" not in blob


def test_main_reuses_a_cached_token_instead_of_logging_in_again(tmp_path, fake_transport) -> None:
    token_file = tmp_path / "t.json"
    token_file.write_text(json.dumps({"access_token": "CACHED"}), encoding="utf-8")

    fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(token_file)],
        env=_env(tmp_path),
    )

    assert fake_transport["login"] == []


def test_main_requires_credentials_in_the_environment_not_argv(tmp_path, fake_transport, capsys) -> None:
    rc = fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json")],
        env={},
    )

    assert rc != 0
    assert "ILETCOMFORT_ACCOUNT" in capsys.readouterr().err


def test_main_has_no_password_or_account_option() -> None:
    """Credentials on argv would leak via shell history and ps(1)."""
    parser_help = fetch_plugin.build_parser().format_help()

    assert "--password" not in parser_help
    assert "--account" not in parser_help


def test_main_metadata_only_does_not_download(tmp_path, fake_transport) -> None:
    rc = fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json"),
         "--metadata-only"],
        env=_env(tmp_path),
    )

    assert rc == 0
    assert fake_transport["get"] == []
    assert list(tmp_path.glob("*.zip")) == []


def test_main_reports_the_no_plugin_case_without_a_traceback(tmp_path, monkeypatch, capsys) -> None:
    """171H120F / 17100003 answer 2200004 on the US tenant -- must be a clean message."""
    def fake_post(self, url, **kwargs):
        if url.endswith("/v1/user/login"):
            return _FakeResponse({"code": 0, "data": {"accessToken": "SECRET-TOKEN"}})
        return _FakeResponse({"code": 2200004, "msg": "产品无符合条件插件", "data": None})

    monkeypatch.setattr("requests.Session.post", fake_post, raising=True)

    rc = fetch_plugin.main(
        ["--model", "171H120F", "--out-dir", str(tmp_path), "--token-file", str(tmp_path / "t.json")],
        env=_env(tmp_path),
    )

    assert rc != 0
    assert "2200004" in capsys.readouterr().err


def test_main_handles_a_rejected_token_with_a_half_set_environment(tmp_path, monkeypatch, capsys) -> None:
    """A cached token can be stale; retrying needs BOTH credentials, not just the account."""
    token_file = tmp_path / "t.json"
    token_file.write_text(json.dumps({"access_token": "STALE"}), encoding="utf-8")

    def fake_post(self, url, **kwargs):
        return _FakeResponse({"code": 14005, "msg": "token rejected", "data": None})

    monkeypatch.setattr("requests.Session.post", fake_post, raising=True)

    rc = fetch_plugin.main(
        ["--model", "17100007", "--out-dir", str(tmp_path), "--token-file", str(token_file)],
        env={"ILETCOMFORT_ACCOUNT": "user@example.invalid"},  # password deliberately absent
    )

    assert rc != 0
    assert "14005" in capsys.readouterr().err
