"""Dollin cloud HTTP transport, request signing, auth, and certificate minting."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random as random_module
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import padding as crypto_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .session import (
    AuthError,
    FileTokenStore,
    is_login_auth_error_code,
    is_token_rejected_code,
)

# ---------------------------------------------------------------------------
# App constants extracted from the iLetComfort iOS binary
# ---------------------------------------------------------------------------
APP_SECRET = "SIT_4VjZdg19laDoIrut"
APP_KEY = "btri"
IOT_KEY = "meicloud"
CLIENT_ID = "d056337d77334ecc95aca4bff6025533"
CLIENT_SECRET = "35b965531383ce9f37f829a19712bf3a"
ENCRYPT_KEY = "4dbc9ff6c15944d78eebb581c2b23de3"
APP_ID = "8010"
API_BASE = "https://us.dollin.net"


class ApiError(Exception):
    """API request error."""


@dataclass
class AppCert:
    """An app-issued client certificate for the vendor's AWS IoT MQTT broker.

    Minted per session-independent of the account token, so MQTT push keeps
    working while the phone app holds the single active account session (the
    login-war fix, see issue #55). ``private_key`` is secret and is kept out of
    ``repr`` so it never lands in logs.
    """

    private_key: str = field(repr=False)
    certificate_pem: str = field(repr=False)
    endpoint: str
    port: int


def mask_identifier(value: str | None, *, keep: int = 5) -> str:
    """Suffix-mask a device identifier for safe display/logging.

    Keeps the first ``keep`` characters and replaces the remainder with ``…`` so
    a value can still be loosely correlated without exposing the full ID. Short
    or empty values are fully masked.
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "…"
    return value[:keep] + "…"


def sign_v1(json_body: str, *, use_iot_key: bool = False) -> tuple[str, str]:
    """Compute the v1 API signature."""
    timestamp_fmt = time.strftime("%Y%m%d%H%M%S")
    random_suffix = str(random_module.randint(0, 65535))
    random_value = timestamp_fmt + random_suffix

    prefix = IOT_KEY if use_iot_key else APP_KEY
    message = prefix + json_body + random_value
    signature = hmac.new(
        APP_SECRET.encode("ascii"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return signature, random_value


def encrypt_password(plaintext_password: str) -> str:
    """Encrypt a plaintext password for the Dollin v1 login API."""
    password_hash_hex = hashlib.sha256(plaintext_password.encode("utf-8")).hexdigest()

    key_material = hashlib.sha256(ENCRYPT_KEY.encode("utf-8")).hexdigest()
    aes_key = key_material[0:16].encode("ascii")
    aes_iv = key_material[16:32].encode("ascii")

    padder = crypto_padding.PKCS7(128).padder()
    padded_data = padder.update(password_hash_hex.encode("utf-8")) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_data) + encryptor.finalize()

    return ciphertext.hex()


def sign_v2(method: str, path: str, body: str) -> str:
    """Compute the v2.0 business API signature."""
    message = method + path + body
    sig_bytes = hmac.new(
        CLIENT_SECRET.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(sig_bytes).decode("utf-8")


class DollinCloudClient:
    """Pure Dollin cloud transport client (login, appliance list, hex RPC, certs)."""

    def __init__(
        self,
        api_base: str = API_BASE,
        access_token: str | None = None,
        timeout: int = 15,
    ) -> None:
        self._api_base = api_base.rstrip("/")
        self._access_token = access_token
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": (
                "iLetComfort/1.6.4 (com.btri.OEMPlus; build:308; iOS 26.3.0) "
                "Alamofire/5.5.0"
            ),
            "language": "en_US",
        })

    @property
    def access_token(self) -> str | None:
        return self._access_token

    @access_token.setter
    def access_token(self, value: str | None) -> None:
        self._access_token = value

    def _v1_request(
        self,
        path: str,
        body_dict: dict[str, Any],
        *,
        use_iot_key: bool = False,
        include_access_token: bool = False,
    ) -> dict[str, Any]:
        """Send a v1 API request with Scheme 1 signing.

        Some v1 endpoints (e.g. the app-certificate mint used for MQTT push) are
        additionally gated on a bearer-style ``accessToken`` header on top of the
        iot-key signing; set ``include_access_token`` for those.
        """
        url = self._api_base + path
        json_body = json.dumps(body_dict, separators=(",", ":"))
        sign_hex, random_value = sign_v1(json_body, use_iot_key=use_iot_key)

        stamp = time.strftime("%Y%m%d%H%M%S")

        headers = {
            "random": random_value,
            "src": "20",
            "appid": APP_ID,
            "language": "en_US",
            "clienttype": "2",
            "appvnum": "1.6.4",
            "stamp": stamp,
            "deviceid": hashlib.sha256(
                f"iletcomfort-py-{int(time.time())}".encode()
            ).hexdigest()[:32].upper(),
            "sign": sign_hex,
            "reqid": hashlib.md5(
                f"{time.time()}-{random_module.random()}".encode()
            ).hexdigest(),
        }

        if include_access_token:
            headers["accessToken"] = self._access_token or ""

        response = self._session.post(
            url, data=json_body, headers=headers, timeout=self._timeout,
        )
        response.raise_for_status()
        return response.json()

    def _v2_request(self, path: str, body_dict: dict[str, Any]) -> dict[str, Any]:
        """Send a v2.0 business API request with Scheme 2 signing."""
        if not self._access_token:
            raise AuthError("No access token available.")

        url = self._api_base + path
        json_body = json.dumps(body_dict, separators=(",", ":"))
        signature = sign_v2("POST", path, json_body)

        headers = {
            "authorization": f"Bearer {self._access_token}",
            "clientId": CLIENT_ID,
            "signature": signature,
            "signatureversion": "2.0",
            "reqid": hashlib.md5(
                f"{time.time()}-{random_module.random()}".encode()
            ).hexdigest(),
        }

        response = self._session.post(
            url, data=json_body, headers=headers, timeout=self._timeout,
        )
        response.raise_for_status()
        result = response.json()

        code = result.get("code")
        if is_token_rejected_code(code):
            raise AuthError("Access token expired or invalid.")

        return result

    def save_token(self, filepath: Path | None = None) -> None:
        """Save the access token to a JSON file."""
        if filepath is None:
            return
        FileTokenStore(filepath).save(self._access_token)

    def load_token(self, filepath: Path | None = None) -> bool:
        """Load access token from a saved file."""
        if filepath is None:
            return False
        token = FileTokenStore(filepath).load()
        if token is not None:
            self._access_token = token
            return True
        return False

    def login(
        self, account: str, password: str, *, pre_encrypted: bool = False,
    ) -> dict[str, Any]:
        """Authenticate with the Dollin cloud."""
        encrypted_pw = password if pre_encrypted else encrypt_password(password)

        body = {
            "loginAccount": account,
            "password": encrypted_pw,
            "encryptVersion": "1",
        }

        result = self._v1_request("/v1/user/login", body, use_iot_key=True)

        if result.get("code") == 0 and "data" in result:
            self._access_token = result["data"]["accessToken"]
            return result["data"]

        code = result.get("code")
        msg = f"Login failed: code={code}, msg={result.get('msg')}"
        if is_login_auth_error_code(code):
            raise AuthError(msg)
        raise ApiError(msg)

    def list_appliances(self) -> list[dict[str, Any]]:
        """List all appliances linked to the account."""
        result = self._v2_request(
            "/midea/open/business/v1/appliance/list",
            {"queryAuth": True},
        )
        if result.get("code") == 0:
            return result.get("data", [])
        raise ApiError(
            f"List appliances failed: code={result.get('code')}, "
            f"msg={result.get('msg')}"
        )

    def send_hex_command(
        self, appliance_code: str, command_hex: str,
    ) -> str:
        """Send a raw hex command to an appliance and return the response hex."""
        result = self._v2_request(
            "/midea/open/business/v1/appliance/control/hexadecimal",
            {
                "applianceCode": appliance_code,
                "command": command_hex,
            },
        )
        if result.get("code") == 0:
            return result.get("data", "")
        raise ApiError(
            f"Send command failed: code={result.get('code')}, "
            f"msg={result.get('msg')}"
        )

    def create_app_cert(self) -> AppCert:
        """Mint an app client certificate for the vendor's AWS IoT MQTT broker."""
        result = self._v1_request(
            "/v1/certificate/create/app/cert",
            {},
            use_iot_key=True,
            include_access_token=True,
        )
        if result.get("code") == 0 and isinstance(result.get("data"), dict):
            data = result["data"]
            return AppCert(
                private_key=str(data["privateKey"]),
                certificate_pem=str(data["certificatePem"]),
                endpoint=str(data["endpoint"]),
                port=int(data["port"]),
            )
        code = result.get("code")
        if is_token_rejected_code(code):
            raise AuthError("Access token expired or invalid.")
        raise ApiError(
            f"Certificate request failed: code={code}, "
            f"msg={result.get('msg')}"
        )
