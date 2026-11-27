"""Dollin cloud account-session lifecycle (#109).

Concentrates token storage, login, vendor auth-rejection classification
(codes 14005 / 12001 and 14xxx login errors), single-flight retry-once-on-
AuthError execution, and operation-mode session policy (HA primary vs read-only
phone-app coexistence) in one framework-independent module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from .const import DEFAULT_OPERATION_MODE, OPERATION_MODE_PHONE_APP

if TYPE_CHECKING:
    from .cloud import AppCert, DollinCloudClient

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

# Vendor Dollin v2/session error codes that indicate the single active account
# token was invalidated (e.g. phone app logged in) or expired.
TOKEN_REJECTED_CODES: frozenset[int] = frozenset({14005, 12001})

# Vendor Dollin v1 login error codes in the 14xxx auth range.
LOGIN_AUTH_ERROR_MIN = 14000
LOGIN_AUTH_ERROR_MAX = 15000


class AuthError(Exception):
    """Authentication or authorization error."""


class ReauthError(AuthError):
    """Raised when re-authentication or the post-reauth retry fails."""


class SessionPolicyError(Exception):
    """Raised when an operation is disallowed by the active operation mode."""


def is_token_rejected_code(code: Any) -> bool:
    """Return True when a vendor API code indicates a rejected/expired token."""
    return (
        isinstance(code, int)
        and not isinstance(code, bool)
        and code in TOKEN_REJECTED_CODES
    )


def is_login_auth_error_code(code: Any) -> bool:
    """Return True when a vendor v1 login code is in the 14xxx auth range."""
    return (
        isinstance(code, int)
        and not isinstance(code, bool)
        and LOGIN_AUTH_ERROR_MIN <= code < LOGIN_AUTH_ERROR_MAX
    )


class TokenStore(Protocol):
    """Storage abstraction for persisting the Dollin account access token."""

    def load(self) -> str | None:
        """Return the persisted access token, or None if unavailable."""

    def save(self, token: str | None) -> None:
        """Persist the access token."""


class MemoryTokenStore:
    """In-memory TokenStore for tests and ephemeral config-flow sessions."""

    def __init__(self, initial_token: str | None = None) -> None:
        self._token: str | None = initial_token

    def load(self) -> str | None:
        return self._token

    def save(self, token: str | None) -> None:
        self._token = token


class FileTokenStore:
    """JSON file-backed TokenStore matching HA `.storage/iletcomfort_token_*`."""

    def __init__(self, filepath: Path) -> None:
        self._filepath = filepath

    @property
    def filepath(self) -> Path:
        return self._filepath

    def load(self) -> str | None:
        if not self._filepath.exists():
            return None
        try:
            data = json.loads(self._filepath.read_text(encoding="utf-8"))
            token = data.get("access_token")
            if token:
                return str(token)
        except (json.JSONDecodeError, OSError, KeyError, AttributeError):
            pass
        return None

    def save(self, token: str | None) -> None:
        data: dict[str, Any] = {}
        if self._filepath.exists():
            try:
                loaded = json.loads(self._filepath.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                pass
        data["access_token"] = token
        data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        self._filepath.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8",
        )


@dataclass(frozen=True)
class SessionPolicy:
    """Explicit account-session policy derived from the entry's operation mode.

    * HA primary: HA owns and refreshes the account session; periodic polling
      and control writes are permitted.
    * Phone-app coexistence: read-only certificate MQTT mode. One setup login
      mints the session-independent MQTT certificate; background polling and
      control writes are strictly disabled so HA never restarts the login war.
    """

    operation_mode: str = DEFAULT_OPERATION_MODE

    @property
    def is_phone_app_coexistence(self) -> bool:
        return self.operation_mode == OPERATION_MODE_PHONE_APP

    @property
    def allows_background_polling(self) -> bool:
        return not self.is_phone_app_coexistence

    @property
    def allows_writes(self) -> bool:
        return not self.is_phone_app_coexistence

    def require_writable(self) -> None:
        """Raise SessionPolicyError when writes are disabled by policy."""
        if not self.allows_writes:
            raise SessionPolicyError(
                "Control is disabled in phone app coexistence mode; "
                "switch to HA primary mode to send commands"
            )


async def _direct_run_blocking(func: Callable[..., T], *args: Any) -> T:
    """Default executor helper for framework-independent usage and tests."""
    return func(*args)


class DollinSessionManager:
    """Owns Dollin account login, token persistence, and retry-once re-auth (#109)."""

    def __init__(
        self,
        client: DollinCloudClient,
        *,
        email: str,
        password: str,
        token_store: TokenStore | None = None,
        operation_mode: str = DEFAULT_OPERATION_MODE,
        run_blocking: Callable[..., Awaitable[Any]] | None = None,
        reauth_hook: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._client = client
        self._email = email
        self._password = password
        self._token_store: TokenStore = token_store or MemoryTokenStore()
        self._policy = SessionPolicy(operation_mode=operation_mode)
        self._run_blocking: Callable[..., Awaitable[Any]] = (
            run_blocking or _direct_run_blocking
        )
        self._reauth_hook = reauth_hook
        self._reauth_lock = asyncio.Lock()

    @property
    def policy(self) -> SessionPolicy:
        return self._policy

    def set_operation_mode(self, operation_mode: str) -> None:
        """Update the active operation mode policy."""
        self._policy = SessionPolicy(operation_mode=operation_mode)

    @property
    def token_store(self) -> TokenStore:
        return self._token_store

    async def async_login(self) -> dict[str, Any]:
        """Authenticate with the Dollin cloud and persist the resulting token."""
        data: dict[str, Any] = await self._run_blocking(
            self._client.login, self._email, self._password,
        )
        await self._run_blocking(self._token_store.save, self._client.access_token)
        return data

    async def async_load_or_login(self) -> bool:
        """Load a persisted token into the client, or log in if none exists.

        Returns True if an existing non-empty token was loaded, False if a
        fresh login was performed.
        """
        token: str | None = await self._run_blocking(self._token_store.load)
        if token:
            self._client.access_token = token
            return True
        await self._async_reauth()
        return False

    async def async_discover_appliances(self) -> list[dict[str, Any]]:
        """List appliances linked to the authenticated account."""
        appliances: list[dict[str, Any]] = await self._run_blocking(
            self._client.list_appliances,
        )
        return appliances

    async def async_run_authenticated(
        self,
        action: Callable[[], T],
        *,
        reason: str = "request",
    ) -> T:
        """Run a blocking authenticated call, re-authenticating once on AuthError."""
        token_before = self._client.access_token
        try:
            result: T = await self._run_blocking(action)
            return result
        except AuthError:
            _LOGGER.info("Auth error during %s, re-authenticating", reason)
            await self._async_reauth_once(token_before)
            retry_result: T = await self._run_blocking(action)
            return retry_result

    async def async_run_authenticated_coro(
        self,
        action: Callable[[], Awaitable[T]],
        *,
        reason: str = "request",
        reauth_error_types: tuple[type[Exception], ...] = (),
    ) -> T:
        """Run an async authenticated operation, re-authenticating once on AuthError.

        Any exception matching ``reauth_error_types`` raised during re-auth or
        the post-reauth retry is wrapped in :class:`ReauthError` (an
        :class:`AuthError` subclass) so callers can distinguish re-auth failure
        from an ordinary initial poll error.
        """
        token_before = self._client.access_token
        try:
            return await action()
        except AuthError:
            _LOGGER.info("Auth error during %s, re-authenticating", reason)
            try:
                await self._async_reauth_once(token_before)
                return await action()
            except AuthError:
                raise
            except reauth_error_types as err:
                raise ReauthError(str(err)) from err

    async def async_mint_mqtt_certificate(self) -> AppCert:
        """Mint an independent AWS IoT MQTT client certificate.

        Re-authenticates only if the current account token is rejected; once
        minted, the MQTT connection authenticates with the X.509 certificate
        independently of the account session.
        """
        return await self.async_run_authenticated(
            self._client.create_app_cert,
            reason="MQTT certificate issuance or renewal",
        )

    async def _async_reauth_once(self, token_before: str | None) -> None:
        """Single-flight re-authentication to prevent concurrent login races."""
        async with self._reauth_lock:
            current = self._client.access_token
            if isinstance(current, str) and current and current != token_before:
                return
            await self._async_reauth()

    async def _async_reauth(self) -> None:
        if self._reauth_hook is not None:
            await self._reauth_hook()
            return
        await self.async_login()


__all__ = [
    "AuthError",
    "DollinSessionManager",
    "FileTokenStore",
    "LOGIN_AUTH_ERROR_MAX",
    "LOGIN_AUTH_ERROR_MIN",
    "MemoryTokenStore",
    "ReauthError",
    "SessionPolicy",
    "SessionPolicyError",
    "TOKEN_REJECTED_CODES",
    "TokenStore",
    "is_login_auth_error_code",
    "is_token_rejected_code",
]
