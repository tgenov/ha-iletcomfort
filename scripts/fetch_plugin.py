#!/usr/bin/env python3
"""Fetch the vendor's per-model plugin bundle from the Dollin cloud (dev tool).

**This is a development / reverse-engineering tool. It is deliberately NOT part
of the shipped Home Assistant integration** — nothing under
``custom_components/`` imports it, and it is never loaded at runtime.

Why it exists: the vendor ships a per-model plugin bundle (for ``0xC3`` a Weex
bundle of readable JS, one file per app screen) that documents the control
fields, their value domains and the fault-code table. That is the vendor's own
source of truth for the control schema, and pulling it beats guessing
control-frame layouts from status-frame offsets or needing a packet capture —
which on Android is a dead end anyway, because the Android app controls the
appliance over MQTT rather than HTTP (see issue #42).

The technique (credit: @dzerik in issue #48) is a version downgrade: tell the
server the installed plugin is ``0.0.0`` and it answers with a download URL for
the current bundle. Send the real version and it correctly reports that you are
already up to date and returns no URL.

Usage::

    export ILETCOMFORT_ACCOUNT='you@example.com'
    export ILETCOMFORT_PASSWORD='...'
    python3 scripts/fetch_plugin.py --model 17100007 --unpack

Credentials are read from the environment only, never from the command line, so
they cannot leak through shell history or ``ps``. Nothing this script writes or
prints contains the account, the access token, the account UID or the presigned
download URL (which embeds AWS SigV4 credentials).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import time
import zipfile
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.iletcomfort.api import (  # noqa: E402
    APP_ID,
    ApiError,
    AuthError,
    ILetComfortClient,
    sign_v1,
)
from custom_components.iletcomfort.const import (  # noqa: E402
    DEFAULT_REGION,
    REGION_URLS,
)

PLUGIN_PATH = "/v1/product/upgrade/plugin/get/latest"

#: Placeholder written in place of any value that embeds a credential.
REDACTED = "<redacted>"

#: Fields whose values are presigned URLs carrying AWS SigV4 credentials.
_URL_FIELDS = ("packageUrl", "previewUrl")

#: The SDK version the iOS app advertises; part of the plugin-eligibility match.
DEFAULT_SDK_VERSION = 20230725

#: Vendor codes seen while probing this endpoint, with what they actually mean.
_ERROR_EXPLANATIONS: dict[int, str] = {
    1105: "the request carried no access token (the endpoint requires one)",
    2200004: (
        "the product exists but has no plugin matching the request "
        "(this model has no bundle registered in this region/tenant)"
    ),
    2200007: "the product does not exist in this region's catalogue",
    3301: "signature rejected (the v1 body must be signed with the iot-key prefix)",
    9999: "the server refused the request outright",
    14005: "the access token was rejected (single-active-session: the app or HA took the session)",
}

# Characters allowed in a filename assembled from vendor-supplied metadata.
_SAFE_FILENAME_CHARS = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
)


class PluginFetchError(Exception):
    """Base class for a plugin bundle that could not be fetched."""


class PluginNotAvailable(PluginFetchError):
    """The server declined to serve a bundle for this model."""


class PluginUpToDate(PluginFetchError):
    """The server considers the requested version current, so it returns no URL."""


# ---------------------------------------------------------------------------
# Request construction
# ---------------------------------------------------------------------------

def build_plugin_request(
    model: str,
    *,
    plugin_type: str = "0xC3",
    version: str = "0.0.0",
    sdk_version: int = DEFAULT_SDK_VERSION,
    firmware_version: str = "000000000000",
    language: str = "en_US",
) -> dict[str, Any]:
    """Build the plugin-lookup body for ``model`` (an ``sn8`` model code).

    ``version`` defaults to ``"0.0.0"`` on purpose: the server only returns a
    ``packageUrl`` when it believes the caller's copy is out of date.
    """
    return {
        "language": language,
        "model": model,
        "sdkVersion": sdk_version,
        "firmwareVersion": firmware_version,
        "type": plugin_type,
        "version": version,
    }


def build_v1_headers(
    body_json: str,
    access_token: str,
    *,
    client_type: str = "2",
    app_version: str = "1.6.4",
) -> dict[str, str]:
    """Build the v1 headers this endpoint needs.

    Two details were established by probing the live endpoint and are easy to
    get wrong:

    * the body must be signed with the **iot-key** prefix (the app-key prefix
      used for other v1 calls answers ``3301 Signature Failed``);
    * the token goes in its own ``accessToken`` header — this endpoint does not
      accept ``Authorization: Bearer`` (without it: ``1105 ... [REQUIRE]``).
    """
    signature, random_value = sign_v1(body_json, use_iot_key=True)
    return {
        "Content-Type": "application/json",
        "random": random_value,
        "src": "20",
        "appid": APP_ID,
        "language": "en_US",
        "clienttype": client_type,
        "appvnum": app_version,
        "stamp": time.strftime("%Y%m%d%H%M%S"),
        "deviceid": hashlib.sha256(
            f"fetch-plugin-{int(time.time())}".encode()
        ).hexdigest()[:32].upper(),
        "sign": signature,
        "reqid": hashlib.md5(
            f"{time.time()}-{random.random()}".encode()
        ).hexdigest(),
        "accessToken": access_token,
    }


# ---------------------------------------------------------------------------
# Response interpretation
# ---------------------------------------------------------------------------

def extract_package_url(response: dict[str, Any]) -> str:
    """Return the bundle URL from a plugin-lookup response.

    Raises :class:`PluginUpToDate` for the "you are already current" answer
    (``code=0`` with metadata but no ``packageUrl``) and
    :class:`PluginNotAvailable` for every declined code.
    """
    code = response.get("code")
    data = response.get("data") or {}

    if code == 0:
        url = data.get("packageUrl")
        if url:
            return str(url)
        raise PluginUpToDate(
            "The server reports the requested version is already current, so it "
            "returned no download URL. Request version '0.0.0' to force it to "
            f"serve the bundle (server reports version={data.get('version')!r})."
        )

    explanation = _ERROR_EXPLANATIONS.get(
        code if isinstance(code, int) else -1, "unrecognized response code"
    )
    raise PluginNotAvailable(
        f"No bundle returned: code={code} ({explanation}); "
        f"server msg={response.get('msg')!r}"
    )


def redact_plugin_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return ``metadata`` with credential-bearing URLs replaced.

    ``packageUrl``/``previewUrl`` are presigned S3 links whose query string
    carries an AWS access-key id and signature, so they must not be printed or
    written to disk. Empty values are left alone, and no key is invented.
    """
    redacted = dict(metadata)
    for field in _URL_FIELDS:
        if redacted.get(field):
            redacted[field] = REDACTED
    return redacted


def _sanitize_filename_part(value: str) -> str:
    """Reduce a vendor-supplied string to something safe inside a filename."""
    cleaned = "".join(ch for ch in value if ch in _SAFE_FILENAME_CHARS)
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    return cleaned.strip(".-") or "unknown"


def bundle_filename(metadata: dict[str, Any], model: str) -> str:
    """Build the on-disk name for a fetched bundle.

    The metadata comes from the vendor, so every component is sanitized before
    it reaches a path.
    """
    part_model = _sanitize_filename_part(str(metadata.get("model") or model))
    part_type = _sanitize_filename_part(str(metadata.get("type") or "0xC3"))
    part_version = _sanitize_filename_part(str(metadata.get("version") or "unknown"))
    return f"{part_model}_{part_type}_v{part_version}.zip"


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def fetch_plugin_metadata(
    api_base: str,
    access_token: str,
    body: dict[str, Any],
    *,
    timeout: int = 30,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Call the plugin-lookup endpoint and return the parsed response."""
    http = session or requests.Session()
    body_json = json.dumps(body, separators=(",", ":"))
    response = http.post(
        api_base.rstrip("/") + PLUGIN_PATH,
        data=body_json,
        headers=build_v1_headers(body_json, access_token),
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def download_bundle(
    url: str,
    destination: Path,
    *,
    timeout: int = 300,
    session: requests.Session | None = None,
) -> int:
    """Stream the bundle at ``url`` into ``destination``; return bytes written.

    ``url`` is presigned and must never be logged.
    """
    http = session or requests.Session()
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with http.get(url, timeout=timeout, stream=True) as response:
        response.raise_for_status()
        with destination.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=1 << 16):
                if chunk:
                    handle.write(chunk)
                    written += len(chunk)
    return written


def unpack_bundle(archive: Path, target_dir: Path) -> Path:
    """Extract a bundle zip, refusing entries that escape ``target_dir``."""
    target_dir.mkdir(parents=True, exist_ok=True)
    resolved_root = target_dir.resolve()
    with zipfile.ZipFile(archive) as zf:
        for member in zf.namelist():
            destination = (resolved_root / member).resolve()
            if not destination.is_relative_to(resolved_root):
                raise PluginFetchError(f"Refusing unsafe archive entry: {member!r}")
        zf.extractall(target_dir)
    return target_dir


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    There is deliberately no credentials option: account and password are read
    from ``ILETCOMFORT_ACCOUNT`` / ``ILETCOMFORT_PASSWORD`` so they never reach
    shell history or the process table.
    """
    parser = argparse.ArgumentParser(
        prog="fetch_plugin.py",
        description=(
            "Fetch a vendor plugin bundle for one model (dev tool; not part of "
            "the shipped integration)."
        ),
        epilog=(
            "Credentials are read from the environment: ILETCOMFORT_ACCOUNT and "
            "ILETCOMFORT_PASSWORD. Logging in invalidates any other active "
            "session on the account (the iLetComfort app and a running Home "
            "Assistant instance included), so prefer a separate account."
        ),
    )
    parser.add_argument(
        "--model",
        required=True,
        help="sn8 model code to fetch, e.g. 17100007 (not a per-device serial)",
    )
    parser.add_argument(
        "--type",
        dest="plugin_type",
        default="0xC3",
        help="appliance type of the plugin (default: %(default)s)",
    )
    parser.add_argument(
        "--plugin-version",
        default="0.0.0",
        help=(
            "version to claim as installed; the server only serves a bundle "
            "when this looks out of date (default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--sdk-version", type=int, default=DEFAULT_SDK_VERSION,
        help="sdkVersion to advertise (default: %(default)s)",
    )
    parser.add_argument(
        "--firmware-version", default="000000000000",
        help="firmwareVersion to advertise (default: %(default)s)",
    )
    parser.add_argument(
        "--language", default="en_US", help="bundle language (default: %(default)s)",
    )
    parser.add_argument(
        "--region", default=DEFAULT_REGION, choices=sorted(REGION_URLS),
        help="cloud region to query (default: %(default)s)",
    )
    parser.add_argument(
        "--out-dir", default="plugin-bundles", type=Path,
        help="directory for the downloaded bundle (default: %(default)s)",
    )
    parser.add_argument(
        "--token-file", default=Path.home() / ".iletcomfort_plugin_token.json", type=Path,
        help=(
            "cache file for the access token, so repeat runs do not re-login "
            "(default: %(default)s)"
        ),
    )
    parser.add_argument(
        "--metadata-only", action="store_true",
        help="look the bundle up and print its metadata without downloading it",
    )
    parser.add_argument(
        "--save-metadata", action="store_true",
        help="also write the redacted metadata next to the bundle",
    )
    parser.add_argument(
        "--unpack", action="store_true",
        help="extract the bundle next to the downloaded archive",
    )
    parser.add_argument(
        "--timeout", type=int, default=30, help="API timeout in seconds (default: %(default)s)",
    )
    return parser


MISSING_CREDENTIALS = (
    "No usable credentials in the environment. Set ILETCOMFORT_ACCOUNT and "
    "ILETCOMFORT_PASSWORD (never pass them on the command line — they would "
    "leak via shell history and ps)."
)


def _credentials(env: dict[str, str]) -> tuple[str, str] | None:
    """Return the account/password pair, or None if either is missing."""
    account = env.get("ILETCOMFORT_ACCOUNT")
    password = env.get("ILETCOMFORT_PASSWORD")
    if not account or not password:
        return None
    return account, password


def _login(client: ILetComfortClient, token_file: Path, credentials: tuple[str, str]) -> None:
    """Log in and cache only the token.

    The login response also carries the account UID and profile fields; those
    are neither printed nor stored.
    """
    client.login(*credentials)
    client.save_token(token_file)
    print(f"Logged in; cached the access token in {token_file}")


def _authenticate(
    client: ILetComfortClient,
    token_file: Path,
    env: dict[str, str],
) -> str | None:
    """Log in (or reuse a cached token). Returns an error message, or None."""
    if client.load_token(token_file):
        print(f"Reusing the cached access token from {token_file}")
        return None

    credentials = _credentials(env)
    if credentials is None:
        return f"No cached token. {MISSING_CREDENTIALS}"

    _login(client, token_file, credentials)
    return None


def main(argv: list[str] | None = None, *, env: dict[str, str] | None = None) -> int:
    """Entry point. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    environment = dict(os.environ if env is None else env)
    api_base = REGION_URLS[args.region]

    client = ILetComfortClient(api_base=api_base, timeout=args.timeout)
    try:
        error = _authenticate(client, args.token_file, environment)
    except (AuthError, ApiError) as err:
        print(f"Login failed: {err}", file=sys.stderr)
        return 1
    if error:
        print(error, file=sys.stderr)
        return 2

    body = build_plugin_request(
        args.model,
        plugin_type=args.plugin_type,
        version=args.plugin_version,
        sdk_version=args.sdk_version,
        firmware_version=args.firmware_version,
        language=args.language,
    )

    print(
        f"Looking up the {args.plugin_type} plugin for model {args.model} "
        f"in region {args.region} (claiming version {args.plugin_version})"
    )
    try:
        response = fetch_plugin_metadata(
            api_base, client.access_token or "", body, timeout=args.timeout,
        )
        try:
            url = extract_package_url(response)
        except PluginNotAvailable:
            # A cached token that the app or a running HA instance has since
            # invalidated is the common case here; retry once with a fresh login.
            credentials = _credentials(environment)
            if response.get("code") not in (1105, 14005) or credentials is None:
                raise
            print("The cached token was rejected; logging in again")
            _login(client, args.token_file, credentials)
            response = fetch_plugin_metadata(
                api_base, client.access_token or "", body, timeout=args.timeout,
            )
            url = extract_package_url(response)
    except PluginFetchError as err:
        print(str(err), file=sys.stderr)
        return 1
    except (AuthError, ApiError) as err:
        print(f"API error: {err}", file=sys.stderr)
        return 1

    metadata = response.get("data") or {}
    redacted = redact_plugin_metadata(metadata)
    print("Bundle metadata:")
    print(json.dumps(redacted, indent=2, ensure_ascii=False, sort_keys=True))

    if args.metadata_only:
        return 0

    destination = args.out_dir / bundle_filename(metadata, args.model)
    written = download_bundle(url, destination, timeout=max(args.timeout, 300))
    print(f"Saved {written} bytes to {destination}")

    if args.save_metadata:
        meta_path = destination.with_suffix(".metadata.json")
        meta_path.write_text(
            json.dumps(redacted, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(f"Saved redacted metadata to {meta_path}")

    if args.unpack:
        target = destination.with_suffix("")
        unpack_bundle(destination, target)
        print(f"Unpacked to {target}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
