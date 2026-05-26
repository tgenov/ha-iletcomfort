"""Unit tests for the iLetComfort API client error mapping."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.iletcomfort.api import (
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSStatus,
)


def _make_client() -> ILetComfortClient:
    return ILetComfortClient(api_base="https://us.dollin.net")


def _c3_frame(body: bytes) -> str:
    """Build a minimal C3 response frame hex string wrapping the given body.

    ``extract_c3_body`` slices ``raw[10:-1]`` and does not validate the
    checksum, so a 10-byte header + body + 1 trailing byte is enough.
    """
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x04])
    return (header + body + b"\x00").hex()


def test_login_success_returns_data_and_stores_token():
    client = _make_client()
    fake_response = {
        "code": 0,
        "data": {"accessToken": "us_A_token", "uid": "42"},
    }
    with patch.object(client, "_v1_request", return_value=fake_response):
        data = client.login("user@example.com", "secret")

    assert data["accessToken"] == "us_A_token"
    assert client.access_token == "us_A_token"


def test_login_raises_auth_error_on_non_zero_code():
    """A bad password must raise AuthError, not ApiError.

    The config flow maps AuthError → invalid_auth. Before the fix this
    raised ApiError and the UI showed cannot_connect instead.
    """
    client = _make_client()
    bad_response = {"code": 14000, "msg": "user or password error"}
    with patch.object(client, "_v1_request", return_value=bad_response):
        with pytest.raises(AuthError) as exc_info:
            client.login("user@example.com", "wrong")

    assert "14000" in str(exc_info.value)


def test_login_raises_api_error_for_signature_failure():
    """Non-auth API failures (e.g. code 3301 Signature Failed) must raise ApiError.

    Earlier this raised AuthError for any non-zero code, which made wrong-region
    / signature failures look like bad passwords in the UI.
    """
    client = _make_client()
    sig_failed = {"code": 3301, "msg": "Signature Failed"}
    with patch.object(client, "_v1_request", return_value=sig_failed):
        with pytest.raises(ApiError) as exc_info:
            client.login("user@example.com", "secret")

    # And it must not be an AuthError, so the config flow's
    # invalid_auth branch is not taken for non-auth failures.
    assert not isinstance(exc_info.value, AuthError)
    assert "3301" in str(exc_info.value)


def test_login_raises_auth_error_only_for_auth_code_range():
    """Codes in the 14xxx range are account/auth-related; others are API errors."""
    client = _make_client()

    for auth_code in (14000, 14001, 14002, 14005):
        with patch.object(
            client, "_v1_request",
            return_value={"code": auth_code, "msg": "auth failure"},
        ):
            with pytest.raises(AuthError):
                client.login("user@example.com", "x")

    for api_code in (1, 3301, 9999):
        with patch.object(
            client, "_v1_request",
            return_value={"code": api_code, "msg": "api failure"},
        ):
            with pytest.raises(ApiError) as exc_info:
                client.login("user@example.com", "x")
            assert not isinstance(exc_info.value, AuthError)


def test_query_sensors_raises_on_echo_only_frame():
    """A sensors response that is just the subtype echo must raise ApiError.

    Reproduces issue #5 ("SENSORS RAW: 02"): the device returned a one-byte
    body, which would otherwise decode to an all-defaults ITSSensors and blank
    every entity. Raising lets the coordinator fall back to cached data.
    """
    client = _make_client()
    echo_only = _c3_frame(b"\x02")
    with patch.object(client, "send_hex_command", return_value=echo_only):
        with pytest.raises(ApiError) as exc_info:
            client.query_sensors("APPL1")

    assert "truncated" in str(exc_info.value)


def test_query_sensors_raises_on_14_byte_frame():
    """A 14-byte sensor body decodes only status_byte, so it must be rejected.

    decode_its_sensors does not read its first data block until body_len > d+13
    (>= 15 bytes); a 14-byte body would otherwise blank every sensor entity.
    """
    client = _make_client()
    fourteen = _c3_frame(bytes(14))
    with patch.object(client, "send_hex_command", return_value=fourteen):
        with pytest.raises(ApiError):
            client.query_sensors("APPL1")


def test_query_sensors_accepts_15_byte_frame():
    """A 15-byte sensor body reaches the first data block and must not raise."""
    client = _make_client()
    # d=1, so status_byte is body[d+0] = body[1]; body[0] is the subtype echo.
    fifteen = _c3_frame(bytes([0x02, 0x07]) + bytes(13))
    with patch.object(client, "send_hex_command", return_value=fifteen):
        sensors = client.query_sensors("APPL1")

    assert sensors.status_byte == 0x07


def test_query_status_raises_on_truncated_frame():
    """A status body shorter than the primary fields must raise ApiError."""
    client = _make_client()
    truncated = _c3_frame(b"\x01\x00")  # subtype echo + 1 byte, < 6 bytes
    with patch.object(client, "send_hex_command", return_value=truncated):
        with pytest.raises(ApiError) as exc_info:
            client.query_status("APPL1")

    assert "truncated" in str(exc_info.value)


def test_query_status_decodes_full_frame():
    """A substantive status body still decodes normally (no false positive)."""
    client = _make_client()
    # d=1, so body[2] (d+1) is the mode byte; set it to 1 (Heat).
    body = bytes([0x01, 0x00, 0x01]) + bytes(17)  # 20-byte body, >= 6
    with patch.object(client, "send_hex_command", return_value=_c3_frame(body)):
        status = client.query_status("APPL1")

    assert isinstance(status, ITSStatus)
    assert status.mode == 1
    assert status.mode_name == "Heat"
