"""MQTT client-certificate materialization and rotation lifecycle (#108).

Isolates X.509 expiry calculation, renewal scheduling, hourly retry,
private-key temp-file materialization, and transactional transport replacement
with rollback from paho broker callbacks.
"""

from __future__ import annotations

import asyncio
import logging
import tempfile
from asyncio import TimerHandle
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Generic, TypeVar

from cryptography import x509

from .api import AppCert

_LOGGER = logging.getLogger(__name__)

CERT_RENEW_BEFORE = timedelta(days=7)
CERT_RENEW_RETRY = timedelta(hours=1)

CertificateProvider = Callable[[], Awaitable[AppCert]]
ScheduleLater = Callable[[float, Callable[[], Any]], TimerHandle]
TTransport = TypeVar("TTransport")


def certificate_expiry(certificate_pem: str) -> datetime:
    """Return the UTC expiry embedded in an app-issued X.509 certificate."""
    cert = x509.load_pem_x509_certificate(certificate_pem.encode("utf-8"))
    expires_utc = getattr(cert, "not_valid_after_utc", None)
    if expires_utc is not None:
        return expires_utc
    return cert.not_valid_after.replace(tzinfo=timezone.utc)


def renewal_delay_seconds(
    certificate_pem: str,
    *,
    now: datetime | None = None,
    expiry_fn: Callable[[str], datetime] = certificate_expiry,
) -> float | None:
    """Return seconds until next planned renewal, or None if cert cannot be parsed.

    Renews at 90% of short certificate lifetimes, capped at seven days early for
    long-lived certificates, with a 1-second floor to avoid tight loops.
    """
    try:
        expires = expiry_fn(certificate_pem)
    except ValueError:
        return None
    current = now if now is not None else datetime.now(timezone.utc)
    remaining = expires - current
    renew_before = min(CERT_RENEW_BEFORE, remaining / 10)
    return max(1.0, (remaining - renew_before).total_seconds())


class MaterializedCredentials:
    """Private 0700 temp directory holding 0600 cert/key files for TLS.

    Private key contents are never logged and are deleted on ``cleanup()``.
    """

    def __init__(self, cert: AppCert) -> None:
        self.tmpdir = tempfile.TemporaryDirectory(prefix="iletcomfort_mqtt_")
        try:
            base = Path(self.tmpdir.name)
            self.cert_path = base / "client.crt"
            self.key_path = base / "client.key"
            self.cert_path.write_text(cert.certificate_pem, encoding="utf-8")
            self.key_path.write_text(cert.private_key, encoding="utf-8")
            self.cert_path.chmod(0o600)
            self.key_path.chmod(0o600)
        except Exception:
            self.tmpdir.cleanup()
            raise

    def cleanup(self) -> None:
        """Remove temporary credential files and directory."""
        self.tmpdir.cleanup()


async def _default_materialize(cert: AppCert) -> MaterializedCredentials:
    return MaterializedCredentials(cert)


async def _default_cleanup(creds: MaterializedCredentials) -> None:
    creds.cleanup()


class MqttCredentialRotator(Generic[TTransport]):
    """Owns MQTT certificate minting, renewal scheduling, retry, and rollback."""

    def __init__(
        self,
        *,
        get_certificate: CertificateProvider,
        connect: Callable[[AppCert, MaterializedCredentials], Awaitable[TTransport]],
        retire: Callable[[TTransport], Awaitable[None]],
        schedule_later: ScheduleLater,
        trigger_renewal: Callable[[], Any],
        materialize: Callable[[AppCert], Awaitable[MaterializedCredentials]] | None = None,
        cleanup_credentials: Callable[[MaterializedCredentials], Awaitable[None]] | None = None,
        expiry_fn: Callable[[str], datetime] = certificate_expiry,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._get_certificate = get_certificate
        self._connect = connect
        self._retire = retire
        self._schedule_later = schedule_later
        self._trigger_renewal = trigger_renewal
        self._materialize = materialize or _default_materialize
        self._cleanup_credentials = cleanup_credentials or _default_cleanup
        self._expiry_fn = expiry_fn
        self._now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self._active_transport: TTransport | None = None
        self._active_credentials: MaterializedCredentials | None = None
        self._renewal_handle: TimerHandle | None = None
        self._renew_lock = asyncio.Lock()
        self._stopped = False

    @property
    def active_transport(self) -> TTransport | None:
        return self._active_transport

    @property
    def active_credentials(self) -> MaterializedCredentials | None:
        return self._active_credentials

    @property
    def renewal_handle(self) -> TimerHandle | None:
        return self._renewal_handle

    async def async_start(self) -> TTransport:
        """Mint initial credentials, connect transport, and schedule renewal."""
        self._stopped = False
        cert = await self._get_certificate()
        if self._stopped:
            raise asyncio.CancelledError("MQTT push stopped before credential materialization")
        creds = await self._materialize(cert)
        if self._stopped:
            await self._cleanup_credentials(creds)
            raise asyncio.CancelledError("MQTT push stopped before broker connect")
        try:
            transport = await self._connect(cert, creds)
        except Exception:
            await self._cleanup_credentials(creds)
            raise
        if self._stopped:
            try:
                await self._retire(transport)
            finally:
                await self._cleanup_credentials(creds)
            raise asyncio.CancelledError("MQTT push stopped during broker connect")
        self._active_transport = transport
        self._active_credentials = creds
        self.schedule_renewal(cert.certificate_pem)
        return transport

    def schedule_renewal(
        self,
        certificate_pem: str | None = None,
        *,
        retry: bool = False,
    ) -> None:
        """Schedule planned rotation from certificate validity or hourly retry."""
        if self._stopped:
            return
        if self._renewal_handle is not None:
            self._renewal_handle.cancel()
            self._renewal_handle = None
        if retry:
            delay = CERT_RENEW_RETRY.total_seconds()
        else:
            assert certificate_pem is not None
            computed = renewal_delay_seconds(
                certificate_pem,
                now=self._now_fn(),
                expiry_fn=self._expiry_fn,
            )
            if computed is None:
                _LOGGER.warning(
                    "MQTT certificate validity could not be read; automatic "
                    "renewal is disabled for this connection"
                )
                return
            delay = computed
        self._renewal_handle = self._schedule_later(delay, self._trigger_renewal)

    async def async_renew(self) -> bool:
        """Rotate credentials while retaining the live transport on failure.

        Single-flight: overlapping renewal triggers return False immediately.
        Returns True when rotation succeeded, False when skipped or rolled back.
        """
        if self._stopped or self._renew_lock.locked():
            return False
        async with self._renew_lock:
            if self._stopped:
                return False
            if self._renewal_handle is not None:
                self._renewal_handle.cancel()
                self._renewal_handle = None
            try:
                cert = await self._get_certificate()
            except Exception as err:  # noqa: BLE001 — retry while old cert remains live
                _LOGGER.warning("MQTT certificate renewal failed; retrying later: %s", err)
                self.schedule_renewal(retry=True)
                return False

            if self._stopped:
                return False

            old_transport = self._active_transport
            old_creds = self._active_credentials
            new_creds: MaterializedCredentials | None = None
            try:
                new_creds = await self._materialize(cert)
                if self._stopped:
                    await self._cleanup_credentials(new_creds)
                    return False
                new_transport = await self._connect(cert, new_creds)
            except Exception as err:  # noqa: BLE001 — keep old transport live and retry
                if new_creds is not None:
                    await self._cleanup_credentials(new_creds)
                _LOGGER.warning(
                    "MQTT certificate replacement connection failed; retrying later: %s",
                    err,
                )
                self.schedule_renewal(retry=True)
                return False

            if self._stopped:
                try:
                    await self._retire(new_transport)
                finally:
                    await self._cleanup_credentials(new_creds)
                return False

            self._active_transport = new_transport
            self._active_credentials = new_creds

            # Retire the previous transport only after the replacement is live,
            # without publishing a false disconnect transition.
            try:
                if old_transport is not None:
                    await self._retire(old_transport)
            except Exception as err:  # noqa: BLE001 — replacement transport is already live
                _LOGGER.warning("Failed to retire previous MQTT transport: %s", err)
            finally:
                if old_creds is not None:
                    await self._cleanup_credentials(old_creds)
                self.schedule_renewal(cert.certificate_pem)
            return True

    async def async_stop(self) -> None:
        """Cancel scheduled rotation, retire active transport, and wipe key files."""
        self._stopped = True
        if self._renewal_handle is not None:
            self._renewal_handle.cancel()
            self._renewal_handle = None
        transport = self._active_transport
        self._active_transport = None
        creds = self._active_credentials
        self._active_credentials = None
        try:
            if transport is not None:
                await self._retire(transport)
        finally:
            if creds is not None:
                await self._cleanup_credentials(creds)


__all__ = [
    "CERT_RENEW_BEFORE",
    "CERT_RENEW_RETRY",
    "CertificateProvider",
    "MaterializedCredentials",
    "MqttCredentialRotator",
    "certificate_expiry",
    "renewal_delay_seconds",
]
