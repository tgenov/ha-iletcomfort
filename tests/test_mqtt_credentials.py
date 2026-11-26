"""Deterministic unit tests for MQTT credential rotation seam (#108)."""

from __future__ import annotations

import asyncio
import os
import stat
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from custom_components.iletcomfort.api import AppCert
from custom_components.iletcomfort.mqtt_credentials import (
    CERT_RENEW_BEFORE,
    CERT_RENEW_RETRY,
    MaterializedCredentials,
    MqttCredentialRotator,
    renewal_delay_seconds,
)


def _cert(tag: str = "1") -> AppCert:
    return AppCert(
        endpoint="iot.example.com",
        port=8883,
        certificate_pem=f"CERT-{tag}",
        private_key=f"PRIVATE-KEY-{tag}",
    )


class _FakeScheduler:
    """Captures scheduled renewal timers deterministically without an event loop."""

    def __init__(self) -> None:
        self.calls: list[tuple[float, object, MagicMock]] = []

    def schedule(self, delay: float, callback):
        handle = MagicMock()
        self.calls.append((delay, callback, handle))
        return handle


def test_renewal_delay_seconds_short_and_long_lifetimes() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)

    # Long-lived cert (90 days): capped at 7 days early -> 83 days.
    delay_long = renewal_delay_seconds(
        "long", now=now, expiry_fn=lambda _: now + timedelta(days=90)
    )
    assert delay_long == pytest.approx((timedelta(days=90) - CERT_RENEW_BEFORE).total_seconds())

    # Short-lived cert (10 hours): 90% of lifetime -> 9 hours.
    delay_short = renewal_delay_seconds(
        "short", now=now, expiry_fn=lambda _: now + timedelta(hours=10)
    )
    assert delay_short == pytest.approx(timedelta(hours=9).total_seconds())

    # Already-expired cert: floored at 1.0s.
    delay_expired = renewal_delay_seconds(
        "expired", now=now, expiry_fn=lambda _: now - timedelta(minutes=5)
    )
    assert delay_expired == 1.0

    # Unparseable cert: returns None.
    def _bad(_: str) -> datetime:
        raise ValueError("invalid pem")

    assert renewal_delay_seconds("bad", now=now, expiry_fn=_bad) is None


def test_materialized_credentials_enforces_0600_and_cleans_up() -> None:
    creds = MaterializedCredentials(_cert("secret"))
    cert_path = creds.cert_path
    key_path = creds.key_path
    tmpdir_name = creds.tmpdir.name

    assert cert_path.read_text(encoding="utf-8") == "CERT-secret"
    assert key_path.read_text(encoding="utf-8") == "PRIVATE-KEY-secret"
    assert stat.S_IMODE(os.stat(tmpdir_name).st_mode) == 0o700
    assert stat.S_IMODE(cert_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600

    creds.cleanup()
    assert not os.path.exists(tmpdir_name)


async def test_rotator_retains_live_transport_on_mint_and_connect_failure() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    scheduler = _FakeScheduler()
    certs = [_cert("v1"), RuntimeError("mint failed"), _cert("v2"), _cert("v3")]
    mint_idx = 0

    async def get_cert() -> AppCert:
        nonlocal mint_idx
        item = certs[mint_idx]
        mint_idx += 1
        if isinstance(item, Exception):
            raise item
        return item

    transports: list[str] = []
    retired: list[str] = []
    failed_dirs: list[str] = []

    async def connect(cert: AppCert, creds: MaterializedCredentials) -> str:
        if cert.certificate_pem == "CERT-v2":
            failed_dirs.append(creds.tmpdir.name)
            raise ConnectionError("broker unreachable")
        name = f"transport-{cert.certificate_pem}"
        transports.append(name)
        return name

    async def retire(transport: str) -> None:
        retired.append(transport)

    rotator = MqttCredentialRotator(
        get_certificate=get_cert,
        connect=connect,
        retire=retire,
        schedule_later=scheduler.schedule,
        trigger_renewal=lambda: None,
        expiry_fn=lambda _: now + timedelta(days=90),
        now_fn=lambda: now,
    )

    t1 = await rotator.async_start()
    assert t1 == "transport-CERT-v1"
    orig_creds_dir = rotator.active_credentials.tmpdir.name  # type: ignore[union-attr]
    assert len(scheduler.calls) == 1
    assert scheduler.calls[0][0] == pytest.approx(timedelta(days=83).total_seconds())

    # 1) Mint failure: old transport and key files stay intact, retries hourly.
    assert await rotator.async_renew() is False
    assert rotator.active_transport == "transport-CERT-v1"
    assert os.path.exists(orig_creds_dir)
    assert retired == []
    assert scheduler.calls[-1][0] == CERT_RENEW_RETRY.total_seconds()

    # 2) Replacement connect failure: old transport stays live, failed temp dir wiped, retries hourly.
    assert await rotator.async_renew() is False
    assert rotator.active_transport == "transport-CERT-v1"
    assert os.path.exists(orig_creds_dir)
    assert len(failed_dirs) == 1
    assert not os.path.exists(failed_dirs[0])
    assert retired == []
    assert scheduler.calls[-1][0] == CERT_RENEW_RETRY.total_seconds()

    # 3) Successful rotation: new transport active, old retired & old temp dir wiped.
    assert await rotator.async_renew() is True
    assert rotator.active_transport == "transport-CERT-v3"
    assert retired == ["transport-CERT-v1"]
    assert not os.path.exists(orig_creds_dir)
    v3_dir = rotator.active_credentials.tmpdir.name  # type: ignore[union-attr]
    assert os.path.exists(v3_dir)

    # 4) Stop: retires active transport, cancels timer, wipes remaining temp dir.
    await rotator.async_stop()
    assert retired == ["transport-CERT-v1", "transport-CERT-v3"]
    assert not os.path.exists(v3_dir)
    assert rotator.active_transport is None
    assert rotator.renewal_handle is None


async def test_rotator_stop_during_in_flight_connect_cleans_up_without_leak() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    scheduler = _FakeScheduler()
    certs = iter([_cert("v1"), _cert("v2")])
    connect_started = asyncio.Event()
    allow_connect_finish = asyncio.Event()
    retired: list[str] = []
    v2_dirs: list[str] = []

    async def get_cert() -> AppCert:
        return next(certs)

    async def connect(cert: AppCert, creds: MaterializedCredentials) -> str:
        if cert.certificate_pem == "CERT-v2":
            v2_dirs.append(creds.tmpdir.name)
            connect_started.set()
            await allow_connect_finish.wait()
        return f"transport-{cert.certificate_pem}"

    async def retire(transport: str) -> None:
        retired.append(transport)

    rotator = MqttCredentialRotator(
        get_certificate=get_cert,
        connect=connect,
        retire=retire,
        schedule_later=scheduler.schedule,
        trigger_renewal=lambda: None,
        expiry_fn=lambda _: now + timedelta(days=90),
        now_fn=lambda: now,
    )

    await rotator.async_start()
    v1_dir = rotator.active_credentials.tmpdir.name  # type: ignore[union-attr]

    renew_task = asyncio.create_task(rotator.async_renew())
    await connect_started.wait()

    # Stop races with the in-flight v2 connect.
    await rotator.async_stop()
    allow_connect_finish.set()
    assert await renew_task is False

    assert rotator.active_transport is None
    assert rotator.active_credentials is None
    assert set(retired) == {"transport-CERT-v1", "transport-CERT-v2"}
    assert not os.path.exists(v1_dir)
    assert len(v2_dirs) == 1
    assert not os.path.exists(v2_dirs[0])


async def test_rotator_single_flight_skips_overlapping_renew() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    scheduler = _FakeScheduler()
    mint_count = 0
    gate = asyncio.Event()

    async def get_cert() -> AppCert:
        nonlocal mint_count
        mint_count += 1
        if mint_count == 2:
            await gate.wait()
        return _cert(f"v{mint_count}")

    async def connect(cert: AppCert, creds: MaterializedCredentials) -> str:
        return f"transport-{cert.certificate_pem}"

    async def retire(transport: str) -> None:
        return None

    rotator = MqttCredentialRotator(
        get_certificate=get_cert,
        connect=connect,
        retire=retire,
        schedule_later=scheduler.schedule,
        trigger_renewal=lambda: None,
        expiry_fn=lambda _: now + timedelta(days=90),
        now_fn=lambda: now,
    )

    await rotator.async_start()
    first = asyncio.create_task(rotator.async_renew())
    await asyncio.sleep(0)
    # Overlapping renewal while first is in flight returns False immediately.
    assert await rotator.async_renew() is False
    gate.set()
    assert await first is True
    assert mint_count == 2
    await rotator.async_stop()


async def test_rotator_cleans_up_creds_even_if_retire_or_start_connect_fails() -> None:
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    scheduler = _FakeScheduler()
    seen_dirs: list[str] = []

    async def get_cert() -> AppCert:
        return _cert("v1")

    async def failing_connect(cert: AppCert, creds: MaterializedCredentials) -> str:
        seen_dirs.append(creds.tmpdir.name)
        raise ConnectionError("boom")

    async def ok_retire(transport: str) -> None:
        return None

    rotator_fail_start = MqttCredentialRotator(
        get_certificate=get_cert,
        connect=failing_connect,
        retire=ok_retire,
        schedule_later=scheduler.schedule,
        trigger_renewal=lambda: None,
        expiry_fn=lambda _: now + timedelta(days=90),
        now_fn=lambda: now,
    )
    with pytest.raises(ConnectionError):
        await rotator_fail_start.async_start()
    assert len(seen_dirs) == 1
    assert not os.path.exists(seen_dirs[0])

    # Now test _retire() raising during rotation still wipes old creds and schedules next renewal.
    certs = iter([_cert("a"), _cert("b")])

    async def next_cert() -> AppCert:
        return next(certs)

    async def ok_connect(cert: AppCert, creds: MaterializedCredentials) -> str:
        return f"t-{cert.certificate_pem}"

    async def failing_retire(transport: str) -> None:
        raise RuntimeError("teardown error")

    rotator = MqttCredentialRotator(
        get_certificate=next_cert,
        connect=ok_connect,
        retire=failing_retire,
        schedule_later=scheduler.schedule,
        trigger_renewal=lambda: None,
        expiry_fn=lambda _: now + timedelta(days=90),
        now_fn=lambda: now,
    )
    await rotator.async_start()
    dir_a = rotator.active_credentials.tmpdir.name  # type: ignore[union-attr]
    assert await rotator.async_renew() is True
    assert not os.path.exists(dir_a)
    assert rotator.renewal_handle is not None

    dir_b = rotator.active_credentials.tmpdir.name  # type: ignore[union-attr]
    with pytest.raises(RuntimeError):
        await rotator.async_stop()
    assert not os.path.exists(dir_b)
