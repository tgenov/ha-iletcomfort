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


def test_query_sensors_raises_on_partial_frame():
    """A 31-byte sensor body leaves odu_voltage (and temps) undecoded.

    The highest offset among sensor.py's ITSSensors entities is odu_voltage,
    decoded only when body_len > d+30 (>= 32). A 31-byte body would replace
    cached values for those entities with 0/None, so it must be rejected.
    """
    client = _make_client()
    thirty_one = _c3_frame(bytes(31))
    with patch.object(client, "send_hex_command", return_value=thirty_one):
        with pytest.raises(ApiError):
            client.query_sensors("APPL1")


def test_query_sensors_accepts_full_data_block():
    """A 32-byte sensor body populates every exposed entity (incl. odu_voltage)."""
    client = _make_client()
    body = bytearray(32)
    body[1] = 0x07   # status_byte = body[d+0]
    body[30] = 230   # odu_voltage = body[d+29]
    with patch.object(client, "send_hex_command", return_value=_c3_frame(bytes(body))):
        sensors = client.query_sensors("APPL1")

    assert sensors.status_byte == 0x07
    assert sensors.odu_voltage == 230


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


# Real frames captured from a Midea MSC-70D2N8-A (issue #11), compressor running.
ISSUE_11_SENSORS_BODY = bytes(
    int(b, 16) for b in (
        "02,01,00,00,00,00,00,00,00,00,00,00,01,02,00,29,49,12,00,01,00,33,"
        "3e,41,69,40,42,ef,04,00,eb,03,31,37,0c,31,37,0c,31,67,23,00,00,01,"
        "72,00,00,00,00,00,00"
    ).split(",")
)
ISSUE_11_STATUS_BODY = bytes(
    int(b, 16) for b in (
        "01,01,01,42,4b,2d,3f,37,0f,23,37,25,28,c0,00,00,80,23,2d,3f,37,00,"
        "23,37,00,3f,00,00,00,00,00,00,01,c2,00,00,01,00,0a,dc,04,e2,00,33,"
        "00,00,00,16,00,00,01"
    ).split(",")
)


def test_query_sensors_scales_odu_current_to_amps():
    """ODU Current must be the fixed-point value (raw / 256), not the raw int.

    In the real MSC-70D2N8-A frame (issue #11) the current bytes 0x04,0x00
    decode big-endian to 1024, which the integration used to surface as
    "1024 A". The official app shows 4 A, confirming a ÷256 scale.
    """
    client = _make_client()
    with patch.object(
        client, "send_hex_command",
        return_value=_c3_frame(ISSUE_11_SENSORS_BODY),
    ):
        sensors = client.query_sensors("APPL1")

    assert sensors.odu_current == 4.0
    assert sensors.odu_voltage == 235
    assert sensors.dc_current == 3


def test_query_status_marks_compressor_running_from_frequency():
    """Compressor Running must follow a non-zero frequency even if the flag is 0.

    In the real MSC-70D2N8-A frame (issue #11) the status-flag byte is 0x00, so
    the old bit-0 check reported "not running", but the compressor frequency is
    51 Hz and the unit is clearly running.
    """
    client = _make_client()
    with patch.object(
        client, "send_hex_command",
        return_value=_c3_frame(ISSUE_11_STATUS_BODY),
    ):
        status = client.query_status("APPL1")

    assert status.comp_frq == 51
    assert status.status_flags_raw == 0
    assert status.comp_running is True


def test_status_flag_zero_and_no_frequency_keeps_compressor_off():
    """A genuinely idle unit (flag 0, comp_frq 0) must not report running."""
    client = _make_client()
    # 50-byte body so comp_frq is decoded (needs body_len > d+48); all zeros
    # after the subtype leave both the flag byte and comp_frq at 0.
    body = bytes([0x01]) + bytes(49)
    with patch.object(client, "send_hex_command", return_value=_c3_frame(body)):
        status = client.query_status("APPL1")

    assert status.comp_frq == 0
    assert status.comp_running is False
