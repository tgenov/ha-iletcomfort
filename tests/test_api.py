"""Unit tests for the iLetComfort API client error mapping."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from custom_components.iletcomfort.api import (
    MODE_HEAT,
    MODE_OFF,
    ApiError,
    AuthError,
    ILetComfortClient,
    ITSStatus,
    build_c3_query,
    build_c3_set,
    mask_identifier,
)
from custom_components.iletcomfort.commands import (
    HeatPumpCommand,
    SetBoostCommand,
    SetHvacModeCommand,
    SetPowerCommand,
    SetTargetTemperatureCommand,
    SetZonePowerCommand,
    SetZoneRunModeCommand,
    SetZoneTargetCommand,
)
from custom_components.iletcomfort.protocols.kjrh120l import decode_kjrh120l_status
from custom_components.iletcomfort.protocols import (
    WriteContext,
    WriteRefused,
    get_adapter,
    run_write_plan,
)
from tests.kjrh120l_snapshots import KJRH120L_SNAPSHOT_A


def _make_client() -> ILetComfortClient:
    return ILetComfortClient(api_base="https://us.dollin.net")


def _c3_frame(body: bytes) -> str:
    """Build a minimal C3 response frame hex string wrapping the given body.

    ``extract_c3_body`` slices ``raw[10:-1]`` and does not validate the
    checksum, so a 10-byte header + body + 1 trailing byte is enough.
    """
    header = bytes([0xAA, 0x00, 0xC3, 0, 0, 0, 0, 0, 0, 0x04])
    return (header + body + b"\x00").hex()


def test_mask_identifier_suffix_masks_long_value():
    """A long device id keeps the first 5 chars and hides the rest."""
    assert mask_identifier("153931629126443") == "15393…"


def test_mask_identifier_fully_masks_short_value():
    """A value no longer than ``keep`` is fully masked (don't expose it)."""
    # len("APPL1") == 5 == keep → fully masked, not echoed back.
    assert mask_identifier("APPL1") == "…"
    assert mask_identifier("abc") == "…"


def test_mask_identifier_empty_and_none_return_empty_string():
    """None / empty values return an empty string."""
    assert mask_identifier(None) == ""
    assert mask_identifier("") == ""


def test_mask_identifier_respects_custom_keep():
    """A custom ``keep`` controls the visible prefix length."""
    assert mask_identifier("153931629126443", keep=3) == "153…"


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


KJRH120L_SN8 = "17100003"


def test_query_status_uses_short_command_for_kjrh120l():
    """KJRH-120L (sn8 17100003) must send the short ``ffff010101ff`` status query.

    Its cloud rejects the standard long C3 frame with code 1214 (issues #21/#5).
    """
    client = _make_client()
    body = bytes([0x01, 0x00, 0x00]) + bytes(17)
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(body),
    ) as mock_send:
        client.query_status("APPL1", sn8=KJRH120L_SN8)

    assert mock_send.call_args.args[1] == "ffff010101ff"


def test_query_status_uses_long_command_for_standard_sn8():
    """An unknown/None sn8 must keep sending the legacy long C3 status query."""
    client = _make_client()
    body = bytes([0x01, 0x00, 0x00]) + bytes(17)
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(body),
    ) as mock_send:
        client.query_status("APPL1")

    assert mock_send.call_args.args[1] == build_c3_query(0x01)


def test_query_sensors_uses_short_command_for_kjrh120l():
    """KJRH-120L (sn8 17100003) must send the short ``ffff020202ff`` sensors query."""
    client = _make_client()
    body = bytearray(32)
    body[1] = 0x07
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(bytes(body)),
    ) as mock_send:
        client.query_sensors("APPL1", sn8=KJRH120L_SN8)

    assert mock_send.call_args.args[1] == "ffff020202ff"


def test_query_sensors_uses_long_command_for_standard_sn8():
    """An unknown/None sn8 must keep sending the legacy long C3 sensors query."""
    client = _make_client()
    body = bytearray(32)
    body[1] = 0x07
    with patch.object(
        client, "send_hex_command", return_value=_c3_frame(bytes(body)),
    ) as mock_send:
        client.query_sensors("APPL1")

    assert mock_send.call_args.args[1] == build_c3_query(0x02)


# --- Write path (issue #35, #113) -----------------------------------------
# Writes are planned by the selected protocol dialect and sent through the
# client's transport (``send_hex_command``). The KJRH-120L's cloud rejects the
# standard 62-byte C3 SET frame, so that dialect sends the captured short write
# commands directly — NO status query, NO build_c3_set. STANDARD/AQUAPURA keep
# the legacy build_c3_set path; ATW never receives the legacy frame.


def _send_command(
    client: ILetComfortClient,
    command: HeatPumpCommand,
    *,
    sn8: str | None = None,
    ctx: WriteContext | None = None,
) -> dict:
    """Plan ``command`` in the sn8's dialect and run it over ``client``'s transport."""
    plan = get_adapter(sn8).plan_write(command, ctx or WriteContext())
    return run_write_plan(plan, lambda command_hex: client.send_hex_command("APPL1", command_hex))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Set-temperature sends 0007 01 <temp> ff and nothing else.
        (SetTargetTemperatureCommand(60), "0007013cff"),
        # Turn-on sends the captured DHW-ON command.
        (SetPowerCommand(on=True), "00020101ff"),
        # hvac-off sends the captured DHW-OFF command.
        (SetHvacModeCommand("off"), "00020100ff"),
        # hvac_mode → heat is treated as DHW power-on.
        (SetHvacModeCommand("heat"), "00020101ff"),
    ],
)
def test_kjrh120l_write_sends_short_command(command, expected):
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        _send_command(client, command, sn8=KJRH120L_SN8)

    # Exactly one transmit (no preliminary status query for this model).
    assert mock_send.call_count == 1
    assert mock_send.call_args.args == ("APPL1", expected)


def test_standard_write_uses_build_c3_set_frame():
    """STANDARD (unknown/None sn8) keeps the legacy build_c3_set path, unchanged.

    The legacy path first queries status (one send) then transmits the 62-byte
    C3 SET frame (a second send). Assert the SET frame equals build_c3_set's
    output for the merged effective values.
    """
    client = _make_client()
    status_body = bytes([0x01, 0x00, 0x01]) + bytes(17)  # mode=Heat
    sent: list[str] = []

    def _fake_send(_code, command):
        sent.append(command)
        return _c3_frame(status_body)

    with patch.object(client, "send_hex_command", side_effect=_fake_send):
        result = _send_command(client, SetTargetTemperatureCommand(30))

    # Two transmits: status query, then the SET frame.
    assert len(sent) == 2
    assert sent[0] == build_c3_query(0x01)
    set_frame = sent[1]
    # 62-byte C3 SET frame → 124 hex chars, and equals build_c3_set's output.
    assert len(set_frame) == 124
    expected = build_c3_set(
        mode=MODE_HEAT,
        temperature=30,
        status_body=bytearray(status_body),
        mute_level=0x00,
        ctrl_flag=0x00,
    )
    assert set_frame == expected
    assert result["sent"] == set_frame


def test_atw_refuses_legacy_c3_set_frame():
    """ATW (sn8 171H120F) must never be sent the legacy 62-byte ITS SET frame."""
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        with pytest.raises(ApiError, match="171H120F"):
            _send_command(client, SetTargetTemperatureCommand(30), sn8="171H120F")

    mock_send.assert_not_called()


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


@pytest.mark.parametrize(
    "command",
    [SetBoostCommand(enabled=True), SetTargetTemperatureCommand(30), SetHvacModeCommand("heat")],
)
def test_legacy_write_refuses_unrecognized_status_mode(command):
    """An unfamiliar layout must never become an OFF command (issue #47)."""
    client = _make_client()
    # Actual status body from the reporter's diagnostics (no identifiers).
    body = bytes.fromhex(
        "01 01 13 00 02 02 17 17 32 30 41 23 19 05 37 19 19 05 3c 22 3c 14 19 00 80"
    )
    with patch.object(client, "send_hex_command", return_value=_c3_frame(body)) as send:
        with pytest.raises(ApiError, match="Unrecognized status mode"):
            _send_command(client, command)
    assert send.call_count == 1  # Only the read; no control frame reaches the device.


@pytest.mark.parametrize(("intent", "mode"), [("off", MODE_OFF), ("heat", MODE_HEAT)])
def test_legacy_write_can_explicitly_leave_auto(intent, mode):
    client = _make_client()
    body = bytes([1, 0, 3, 60]) + bytes(21)
    with patch.object(client, "send_hex_command", return_value=_c3_frame(body)) as send:
        result = _send_command(client, SetHvacModeCommand(intent))
    assert send.call_count == 2
    assert bytes.fromhex(result["sent"])[12] == mode


def test_legacy_write_does_not_turn_auto_off_for_boost():
    client = _make_client()
    body = bytes([1, 0, 3, 60]) + bytes(21)
    with patch.object(client, "send_hex_command", return_value=_c3_frame(body)) as send:
        with pytest.raises(ApiError, match="Cannot preserve Auto mode"):
            _send_command(client, SetBoostCommand(enabled=True))
    assert send.call_count == 1


# --- App client certificate issuance (MQTT push, issue #55) --------------------

def test_create_app_cert_returns_bundle_from_data():
    client = _make_client()
    client.access_token = "TOKEN"
    resp = {
        "code": 0,
        "data": {
            "privateKey": "-----BEGIN RSA PRIVATE KEY-----\nAAA\n-----END RSA PRIVATE KEY-----",
            "certificatePem": "-----BEGIN CERTIFICATE-----\nBBB\n-----END CERTIFICATE-----",
            "endpoint": "a39t91kk7ictzi-ats.iot.us-west-2.amazonaws.com",
            "port": "8883",
        },
    }
    with patch.object(client, "_v1_request", return_value=resp) as mock_req:
        cert = client.create_app_cert()

    # iot-key signing + the accessToken header are what this endpoint requires.
    assert mock_req.call_args.kwargs["use_iot_key"] is True
    assert mock_req.call_args.kwargs["include_access_token"] is True
    assert mock_req.call_args.args[0] == "/v1/certificate/create/app/cert"
    assert cert.private_key.startswith("-----BEGIN RSA PRIVATE KEY-----")
    assert cert.certificate_pem.startswith("-----BEGIN CERTIFICATE-----")
    assert cert.endpoint == "a39t91kk7ictzi-ats.iot.us-west-2.amazonaws.com"
    assert cert.port == 8883


def test_create_app_cert_repr_does_not_leak_key_material():
    """A cert bundle must never render its private key in logs/reprs."""
    client = _make_client()
    client.access_token = "TOKEN"
    resp = {"code": 0, "data": {
        "privateKey": "-----BEGIN RSA PRIVATE KEY-----\nSECRETKEYBYTES\n-----END RSA PRIVATE KEY-----",
        "certificatePem": "-----BEGIN CERTIFICATE-----\nCERTBYTES\n-----END CERTIFICATE-----",
        "endpoint": "broker.invalid", "port": "8883"}}
    with patch.object(client, "_v1_request", return_value=resp):
        cert = client.create_app_cert()

    assert "SECRETKEYBYTES" not in repr(cert)
    assert "CERTBYTES" not in repr(cert)


def test_create_app_cert_raises_api_error_on_failure():
    client = _make_client()
    client.access_token = "TOKEN"
    with patch.object(client, "_v1_request", return_value={"code": 5, "msg": "No login credential"}):
        with pytest.raises(ApiError) as exc:
            client.create_app_cert()
    assert "5" in str(exc.value)


def test_v1_request_includes_access_token_header_when_requested():
    client = _make_client()
    client.access_token = "TOKENVALUE"
    captured = {}

    class _Resp:
        def raise_for_status(self): ...
        def json(self): return {"code": 0, "data": {}}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    with patch.object(client._session, "post", side_effect=fake_post):
        client._v1_request("/x", {}, use_iot_key=True, include_access_token=True)
    assert captured["headers"]["accessToken"] == "TOKENVALUE"

    captured.clear()
    with patch.object(client._session, "post", side_effect=fake_post):
        client._v1_request("/x", {}, use_iot_key=True)
    assert "accessToken" not in captured["headers"]


# --- KJRH-120L Zone 1 writes ------------------------------------------------
# Command strings pinned from the iOS app capture (2026-11-12, 2026-11-14).

def _kjrh_ctx(setpoint: int) -> WriteContext:
    """KJRH-120L context whose Zone 1 setpoint is ``setpoint`` (snapshot A limits)."""
    body = bytearray(KJRH120L_SNAPSHOT_A)
    body[12] = setpoint
    return WriteContext(status=decode_kjrh120l_status(bytes(body)))


@pytest.mark.parametrize(
    ("command", "ctx", "expected"),
    [
        (SetZoneTargetCommand(zone=1, temperature=51), _kjrh_ctx(50), "00040133ff"),
        (SetZoneTargetCommand(zone=1, temperature=63), _kjrh_ctx(50), "0004013fff"),
        (SetZoneRunModeCommand(zone=1, mode="cool"), _kjrh_ctx(25), "00030102ff00040119ff"),
        (SetZoneRunModeCommand(zone=1, mode="heat"), _kjrh_ctx(50), "00030103ff00040132ff"),
        (SetZonePowerCommand(zone=1, on=True), None, "00000101ff"),
        (SetZonePowerCommand(zone=1, on=False), None, "00000100ff"),
    ],
)
def test_kjrh120l_zone1_writes_send_captured_command(command, ctx, expected):
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        _send_command(client, command, sn8=KJRH120L_SN8, ctx=ctx)

    assert mock_send.call_count == 1
    assert mock_send.call_args.args == ("APPL1", expected)


@pytest.mark.parametrize(
    ("command", "ctx"),
    [
        # The app always pairs mode with the setpoint; no status → no setpoint.
        (SetZoneRunModeCommand(zone=1, mode="cool"), None),
        (SetZoneTargetCommand(zone=1, temperature=300), None),
    ],
)
def test_kjrh120l_refuses_invalid_zone1_requests_without_sending(command, ctx):
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        with pytest.raises(ValueError):
            _send_command(client, command, sn8=KJRH120L_SN8, ctx=ctx)

    mock_send.assert_not_called()


@pytest.mark.parametrize("sn8", [None, "171000AU", "17100007", "UNKNOWN8"])
@pytest.mark.parametrize(
    "command",
    [
        SetZoneTargetCommand(zone=1, temperature=50),
        SetZonePowerCommand(zone=1, on=True),
    ],
)
def test_non_kjrh_refuses_zone1_writes_without_sending(sn8, command):
    """Other models must never receive a KJRH-120L frame or a status query."""
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        with pytest.raises(ApiError, match="KJRH-120L"):
            _send_command(client, command, sn8=sn8)

    mock_send.assert_not_called()


@pytest.mark.parametrize("sn8", [None, "171H120F", "171000AU", "17100007", "UNKNOWN8"])
def test_non_kjrh_refuses_run_mode_without_sending(sn8):
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        with pytest.raises(ApiError, match="KJRH-120L"):
            _send_command(
                client,
                SetZoneRunModeCommand(zone=1, mode="cool"),
                sn8=sn8,
                ctx=_kjrh_ctx(25),
            )

    mock_send.assert_not_called()


def test_atw_zone1_writes_need_c3_opt_in_without_sending():
    client = _make_client()
    with patch.object(client, "send_hex_command") as mock_send:
        with pytest.raises(WriteRefused, match="not enabled"):
            _send_command(client, SetZonePowerCommand(zone=1, on=True), sn8="171H120F")

    mock_send.assert_not_called()
