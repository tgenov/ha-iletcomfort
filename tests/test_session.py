"""Focused unit tests for the Dollin account-session lifecycle (#109)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from custom_components.iletcomfort.cloud import ApiError, AppCert, DollinCloudClient
from custom_components.iletcomfort.const import (
    OPERATION_MODE_HA_PRIMARY,
    OPERATION_MODE_PHONE_APP,
)
from custom_components.iletcomfort.session import (
    AuthError,
    DollinSessionManager,
    FileTokenStore,
    MemoryTokenStore,
    SessionPolicy,
    SessionPolicyError,
    is_login_auth_error_code,
    is_token_rejected_code,
)


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def test_vendor_auth_code_classification() -> None:
    assert is_token_rejected_code(14005) is True
    assert is_token_rejected_code(12001) is True
    assert is_token_rejected_code(1214) is False
    assert is_token_rejected_code(0) is False
    assert is_token_rejected_code("14005") is False
    assert is_token_rejected_code(True) is False

    assert is_login_auth_error_code(14000) is True
    assert is_login_auth_error_code(14005) is True
    assert is_login_auth_error_code(14999) is True
    assert is_login_auth_error_code(15000) is False
    assert is_login_auth_error_code(12001) is False
    assert is_login_auth_error_code(True) is False


@pytest.mark.parametrize("code", [14005, 12001])
def test_cloud_v2_and_cert_classify_rejected_token_as_auth_error(code: int) -> None:
    client = DollinCloudClient(access_token="stale-token")
    with patch.object(
        client._session, "post", return_value=_mock_response({"code": code, "msg": "rejected"})
    ):
        with pytest.raises(AuthError):
            client.list_appliances()
        with pytest.raises(AuthError):
            client.create_app_cert()


def test_file_token_store_round_trip_preserves_extra_fields(tmp_path: Path) -> None:
    token_file = tmp_path / "token.json"
    token_file.write_text('{"extra": "keep"}\n', encoding="utf-8")

    store = FileTokenStore(token_file)
    assert store.load() is None

    store.save("tok-123")
    assert store.load() == "tok-123"
    assert '"extra": "keep"' in token_file.read_text(encoding="utf-8")


def test_session_policy_enforces_phone_app_coexistence() -> None:
    ha_primary = SessionPolicy(OPERATION_MODE_HA_PRIMARY)
    assert ha_primary.allows_background_polling is True
    assert ha_primary.allows_writes is True
    ha_primary.require_writable()

    phone_app = SessionPolicy(OPERATION_MODE_PHONE_APP)
    assert phone_app.is_phone_app_coexistence is True
    assert phone_app.allows_background_polling is False
    assert phone_app.allows_writes is False
    with pytest.raises(SessionPolicyError, match="phone app coexistence"):
        phone_app.require_writable()


async def test_session_manager_loads_persisted_token_or_logs_in_in_memory() -> None:
    client = MagicMock(spec=DollinCloudClient)
    client.access_token = None

    def _login(email: str, password: str) -> dict[str, str]:
        client.access_token = "fresh-token"
        return {"accessToken": "fresh-token"}

    client.login.side_effect = _login

    # Empty store -> performs login and persists token in memory (no filesystem/HA).
    store = MemoryTokenStore()
    session = DollinSessionManager(
        client, email="u@example.com", password="pw", token_store=store
    )
    assert await session.async_load_or_login() is False
    client.login.assert_called_once_with("u@example.com", "pw")
    assert store.load() == "fresh-token"

    # Pre-populated store -> loads token without logging in.
    client2 = MagicMock(spec=DollinCloudClient)
    client2.access_token = None
    session2 = DollinSessionManager(
        client2, email="u@example.com", password="pw", token_store=store
    )
    assert await session2.async_load_or_login() is True
    client2.login.assert_not_called()
    assert client2.access_token == "fresh-token"


async def test_session_manager_retries_once_on_auth_error_for_sync_coro_and_cert() -> None:
    client = MagicMock(spec=DollinCloudClient)
    client.access_token = "old"

    def _login(email: str, password: str) -> dict[str, str]:
        client.access_token = "new"
        return {"accessToken": "new"}

    client.login.side_effect = _login
    store = MemoryTokenStore("old")
    session = DollinSessionManager(
        client, email="u@example.com", password="pw", token_store=store
    )

    # 1) Blocking action: AuthError -> re-auth once -> succeeds.
    sync_action = MagicMock(side_effect=[AuthError("14005"), "ok"])
    assert await session.async_run_authenticated(sync_action, reason="set") == "ok"
    assert sync_action.call_count == 2
    assert client.login.call_count == 1
    assert store.load() == "new"

    # 2) Async coroutine: AuthError -> re-auth once -> propagates if retry also fails.
    attempts = 0

    async def failing_coro() -> str:
        nonlocal attempts
        attempts += 1
        raise AuthError("12001")

    with pytest.raises(AuthError):
        await session.async_run_authenticated_coro(failing_coro, reason="poll")
    assert attempts == 2
    assert client.login.call_count == 2

    # 3) MQTT cert minting: re-auths only on AuthError, never on non-auth ApiError.
    cert = AppCert(private_key="k", certificate_pem="c", endpoint="e", port=8883)
    client.create_app_cert.side_effect = [AuthError("14005"), cert]
    assert await session.async_mint_mqtt_certificate() is cert
    assert client.login.call_count == 3

    client.create_app_cert.side_effect = ApiError("code=1214")
    with pytest.raises(ApiError):
        await session.async_mint_mqtt_certificate()
    assert client.login.call_count == 3


async def test_session_manager_empty_string_token_triggers_login() -> None:
    client = MagicMock(spec=DollinCloudClient)
    client.access_token = None

    def _login(email: str, password: str) -> dict[str, str]:
        client.access_token = "fresh-token"
        return {"accessToken": "fresh-token"}

    client.login.side_effect = _login
    store = MemoryTokenStore("")
    session = DollinSessionManager(
        client, email="u@example.com", password="pw", token_store=store
    )
    assert await session.async_load_or_login() is False
    client.login.assert_called_once()
    assert client.access_token == "fresh-token"


async def test_session_manager_single_flight_prevents_concurrent_login_war() -> None:
    client = MagicMock(spec=DollinCloudClient)
    client.access_token = "stale-token"
    login_calls = 0

    async def _reauth_login() -> None:
        nonlocal login_calls
        login_calls += 1
        await asyncio.sleep(0.01)
        client.access_token = f"fresh-token-{login_calls}"

    session = DollinSessionManager(
        client,
        email="u@example.com",
        password="pw",
        token_store=MemoryTokenStore("stale-token"),
        reauth_hook=_reauth_login,
    )

    async def _op(name: str) -> str:
        if client.access_token == "stale-token":
            await asyncio.sleep(0)
            raise AuthError("14005")
        return f"{name}:{client.access_token}"

    r1, r2 = await asyncio.gather(
        session.async_run_authenticated_coro(lambda: _op("poll"), reason="poll"),
        session.async_run_authenticated_coro(lambda: _op("set"), reason="set"),
    )
    assert login_calls == 1
    assert r1 == "poll:fresh-token-1"
    assert r2 == "set:fresh-token-1"
