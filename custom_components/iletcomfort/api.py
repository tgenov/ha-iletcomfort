"""iLetComfort / ITS/BTRI Heat Pump Cloud API Client.

Stripped version of iletcomfort_client.py for use as a Home Assistant integration
library. Contains only the API client classes and protocol functions — no CLI code.
"""

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
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding as crypto_padding

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

DEVICE_TYPE_C3 = 0xC3

TEMP_OFFSET = 35
SENSOR_DISCONNECTED = 204

# SET command operating modes
MODE_OFF = 0x00
MODE_HEAT = 0x01
MODE_COOL = 0x03
MODE_WATERPUMP = 0x04
MODE_MAP: dict[str, int] = {
    "off": MODE_OFF,
    "heat": MODE_HEAT,
    "cool": MODE_COOL,
    "waterpump": MODE_WATERPUMP,
}

# Temperature validation ranges per mode (Celsius)
TEMP_RANGES: dict[int, tuple[int, int]] = {
    MODE_HEAT: (10, 40),
    MODE_COOL: (12, 40),
    MODE_WATERPUMP: (15, 40),
}

# Query response mode → SET mode mapping
QUERY_TO_SET_MODE: dict[int, int] = {
    0: MODE_OFF,
    1: MODE_HEAT,
    2: MODE_COOL,
    4: MODE_WATERPUMP,
}


# ---------------------------------------------------------------------------
# Signing algorithms
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# C3 heat pump protocol -- frame construction
# ---------------------------------------------------------------------------

def build_c3_query(subtype: int) -> str:
    """Build a C3 query command frame."""
    header = [
        0xAA, 0x00, DEVICE_TYPE_C3,
        0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, 0x03,
    ]
    body = [subtype]
    header[1] = len(header) + len(body)
    frame = header + body
    checksum = (~sum(frame[1:]) + 1) & 0xFF
    frame.append(checksum)
    return bytes(frame).hex()


def build_c3_set(
    mode: int,
    temperature: int,
    status_body: bytearray,
    mute_level: int = 0,
    ctrl_flag: int = 0,
) -> str:
    """Build a C3 SET command frame (62 bytes)."""
    d = 1
    frame = bytearray(62)

    frame[0] = 0xAA
    frame[1] = 0x3D
    frame[2] = DEVICE_TYPE_C3
    frame[9] = 0x02
    frame[10] = 0x01
    frame[11] = 0x01
    frame[12] = mode
    frame[13] = temperature + TEMP_OFFSET

    if len(status_body) > d + 18:
        frame[14] = status_body[d + 4]
        frame[15] = status_body[d + 5]
        frame[16] = status_body[d + 6]
        frame[18] = status_body[d + 8]
        frame[19] = status_body[d + 9]
        frame[23] = status_body[d + 13]
        frame[28] = status_body[d + 18]

    frame[25] = mute_level
    frame[26] = ctrl_flag
    frame[61] = (~sum(frame[1:61]) + 1) & 0xFF

    return frame.hex()


# ---------------------------------------------------------------------------
# C3 response parsing
# ---------------------------------------------------------------------------

def parse_hex_response(hex_data: str) -> bytearray:
    """Parse a hex response string into bytes."""
    hex_data = hex_data.strip()
    if "," in hex_data:
        return bytearray(int(x.strip(), 16) for x in hex_data.split(","))
    return bytearray(bytes.fromhex(hex_data))


def extract_c3_body(raw: bytearray) -> tuple[int, bytearray]:
    """Extract the body from a C3 response frame."""
    if len(raw) < 12:
        raise ValueError(f"Response too short: {len(raw)} bytes")
    if raw[0] != 0xAA:
        raise ValueError(f"Invalid header byte: 0x{raw[0]:02x}")
    if raw[2] != DEVICE_TYPE_C3:
        raise ValueError(f"Not a C3 device response: 0x{raw[2]:02x}")

    body = raw[10:-1]
    body_type = body[0] if body else 0
    return body_type, body


def _temp_offset(raw_byte: int) -> float | None:
    """Decode a temperature byte using the ITS +35 offset encoding."""
    value = raw_byte - TEMP_OFFSET
    if value == SENSOR_DISCONNECTED:
        return None
    return float(value)


# ---------------------------------------------------------------------------
# ITS protocol response decoding -- subtype 0x01 (Status & Control)
# ---------------------------------------------------------------------------

@dataclass
class ITSStatus:
    """Decoded ITS subtype 0x01 -- status, control settings, and runtime data."""

    pump_outdoor: bool = False
    pump_system: bool = False
    mode: int = 0
    mode_name: str = "Off"
    t5s_def: float | None = None
    t5s_max: float | None = None
    set_temperature: int = 0
    config_status: int = 0
    td_max: float | None = None
    td_min: float | None = None
    ptc_temperature_1: float | None = None
    trdh_max: float | None = None
    trdh_min: float | None = None
    trdh_def: float | None = None
    mute_valid: bool = False
    force_heat_valid: bool = False
    sterilize_valid: bool = False
    comp_running: bool = False
    ibh_running: bool = False
    sterilize_running: bool = False
    status_flags_raw: int = 0
    enable_flags_1: int = 0
    enable_flags_2: int = 0
    box_bottom_temp: float | None = None
    ptc_temperature: float | None = None
    tr_temperature: float | None = None
    version_or_sterilize_hour: int = 0
    sterilize_min: int = 0
    sterilize_temperature: float | None = None
    sterilize_cycle_days: int = 0
    error_code: int = 0
    heat_pump_work_temp_limit: float | None = None
    vacation_start_year: int = 0
    vacation_start_month: int = 0
    vacation_start_day: int = 0
    vacation_end_month: int = 0
    vacation_end_day: int = 0
    exv_drg: int = 0
    pressure_h: int = 0
    pressure_l: int = 0
    comp_frq: int = 0
    total_kwh: int = 0
    comp_total_run_hours: int = 0
    fan_total_run_hours: int = 0
    raw_body: bytes = field(default_factory=bytes, repr=False)


def decode_its_status(body: bytearray) -> ITSStatus:
    """Decode ITS subtype 0x01 body into an ITSStatus object."""
    status = ITSStatus()
    status.raw_body = bytes(body)
    d = 1
    body_len = len(body)

    if body_len < d + 5:
        return status

    b0 = body[d + 0]
    status.pump_outdoor = bool(b0 & 0x01)
    status.pump_system = bool(b0 & 0x02)

    modes = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto", 4: "Water Pump"}
    status.mode = body[d + 1]
    status.mode_name = modes.get(status.mode, f"Unknown({status.mode})")

    status.t5s_def = _temp_offset(body[d + 2])
    status.t5s_max = _temp_offset(body[d + 3])
    status.set_temperature = body[d + 4]

    if body_len > d + 5:
        status.config_status = body[d + 5]
    if body_len > d + 7:
        status.td_max = _temp_offset(body[d + 6])
        status.td_min = _temp_offset(body[d + 7])
    if body_len > d + 11:
        status.ptc_temperature_1 = _temp_offset(body[d + 8])
        status.trdh_max = _temp_offset(body[d + 9])
        status.trdh_min = _temp_offset(body[d + 10])
        status.trdh_def = _temp_offset(body[d + 11])
    if body_len > d + 12:
        b12 = body[d + 12]
        status.mute_valid = bool(b12 & 0x80)
        status.force_heat_valid = bool(b12 & 0x40)
        status.sterilize_valid = bool(b12 & 0x20)
    if body_len > d + 13:
        b13 = body[d + 13]
        status.status_flags_raw = b13
        status.comp_running = bool(b13 & 0x01)
        status.ibh_running = bool(b13 & 0x02)
        status.sterilize_running = bool(b13 & 0x04)
    if body_len > d + 15:
        status.enable_flags_1 = body[d + 14]
        status.enable_flags_2 = body[d + 15]
    if body_len > d + 18:
        status.box_bottom_temp = _temp_offset(body[d + 16])
        status.ptc_temperature = _temp_offset(body[d + 17])
        status.tr_temperature = _temp_offset(body[d + 18])
    if body_len > d + 22:
        status.version_or_sterilize_hour = body[d + 19]
        status.sterilize_min = body[d + 20]
        status.sterilize_temperature = _temp_offset(body[d + 21])
        status.sterilize_cycle_days = body[d + 22]
    if body_len > d + 23:
        status.error_code = body[d + 23]
    if body_len > d + 24:
        status.heat_pump_work_temp_limit = _temp_offset(body[d + 24])
    if body_len > d + 28:
        status.vacation_start_year = body[d + 25]
        status.vacation_start_month = body[d + 26]
        status.vacation_start_day = body[d + 27]
        b28 = body[d + 28]
        status.vacation_end_month = (b28 >> 5) & 0x07
        status.vacation_end_day = b28 & 0x1F
    if body_len > d + 48:
        status.exv_drg = (body[d + 35] << 8) | body[d + 36]
        status.pressure_h = (body[d + 37] << 8) | body[d + 38]
        status.pressure_l = (body[d + 39] << 8) | body[d + 40]
        status.comp_frq = (body[d + 41] << 8) | body[d + 42]
        status.total_kwh = (body[d + 43] << 8) | body[d + 44]
        status.comp_total_run_hours = (body[d + 45] << 8) | body[d + 46]
        status.fan_total_run_hours = (body[d + 47] << 8) | body[d + 48]

    return status


# ---------------------------------------------------------------------------
# ITS protocol response decoding -- subtype 0x02 (Sensors & Extended)
# ---------------------------------------------------------------------------

@dataclass
class ITSSensors:
    """Decoded ITS subtype 0x02 -- sensor temperatures and extended data."""

    status_byte: int = 0
    online_num: int = 0
    odu_mac_type: int = 0
    limit_frq_code: int = 0
    tf_temp: int = 0
    tp_temp: int = 0
    th_temp: int = 0
    water_pres: int = 0
    water_flow: int = 0
    capacity_hp: int = 0
    t3_temp: float | None = None
    t4_temp: float | None = None
    t2_temp: float | None = None
    t2b_temp: float | None = None
    twin_temp: float | None = None
    twout_temp: float | None = None
    t1_temp: float | None = None
    odu_current: int = 0
    odu_voltage: int = 0
    dc_current: int = 0
    idu_version: str = ""
    odu_version: str = ""
    hmi_version: str = ""
    ctrl_flag: int = 0  # d+41: 0=normal, 1=mute, 2=boost
    mute_level: int = 0  # d+40: 0=Level 1 (or off), 1=Level 2
    dc_voltage: int = 0
    ibh1_total_run_hours: int = 0
    ibh2_total_run_hours: int = 0
    tbh_total_run_hours: int = 0
    ahs_total_run_hours: int = 0
    hpc_value: int = 0
    raw_body: bytes = field(default_factory=bytes, repr=False)


def _decode_its_version(b0: int, b1: int, b2: int) -> str:
    """Decode a 3-byte ITS version field."""
    year = 2000 + (b0 >> 1)
    month = ((b0 & 1) << 3) | (b1 >> 5)
    day = b1 & 0x1F
    version = b2
    return f"{year:04d}-{month:02d}-{day:02d} v{version}"


def decode_its_sensors(body: bytearray) -> ITSSensors:
    """Decode ITS subtype 0x02 body into an ITSSensors object."""
    sensors = ITSSensors()
    sensors.raw_body = bytes(body)
    d = 1
    body_len = len(body)

    if body_len < d + 1:
        return sensors

    sensors.status_byte = body[d + 0]

    if body_len > d + 13:
        sensors.online_num = body[d + 11]
        sensors.odu_mac_type = body[d + 12]
        sensors.limit_frq_code = body[d + 13]
    if body_len > d + 16:
        sensors.tf_temp = body[d + 14]
        sensors.tp_temp = body[d + 15]
        sensors.th_temp = body[d + 16]
    if body_len > d + 18:
        sensors.water_pres = body[d + 17]
        sensors.water_flow = body[d + 18]
    if body_len > d + 19:
        sensors.capacity_hp = body[d + 19]
    if body_len > d + 26:
        sensors.t3_temp = _temp_offset(body[d + 20])
        sensors.t4_temp = _temp_offset(body[d + 21])
        sensors.t2_temp = _temp_offset(body[d + 22])
        sensors.t2b_temp = _temp_offset(body[d + 23])
        sensors.twin_temp = _temp_offset(body[d + 24])
        sensors.twout_temp = _temp_offset(body[d + 25])
        sensors.t1_temp = _temp_offset(body[d + 26])
    if body_len > d + 30:
        sensors.odu_current = (body[d + 27] << 8) | body[d + 28]
        sensors.odu_voltage = body[d + 29]
        sensors.dc_current = body[d + 30]
    if body_len > d + 39:
        sensors.idu_version = _decode_its_version(
            body[d + 31], body[d + 32], body[d + 33],
        )
        sensors.odu_version = _decode_its_version(
            body[d + 34], body[d + 35], body[d + 36],
        )
        sensors.hmi_version = _decode_its_version(
            body[d + 37], body[d + 38], body[d + 39],
        )
    if body_len > d + 48:
        sensors.mute_level = body[d + 40]  # 0=Level 1 (or off), 1=Level 2
        sensors.ctrl_flag = body[d + 41]  # 0=normal, 1=mute, 2=boost
        sensors.dc_voltage = (body[d + 42] << 8) | body[d + 43]
        sensors.ibh1_total_run_hours = body[d + 44]
        sensors.ibh2_total_run_hours = body[d + 45]
        sensors.tbh_total_run_hours = body[d + 46]
        sensors.ahs_total_run_hours = body[d + 47]
        sensors.hpc_value = body[d + 48]

    return sensors


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AuthError(Exception):
    """Authentication or authorization error."""


class ApiError(Exception):
    """API request error."""


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class ILetComfortClient:
    """Client for the iLetComfort / Midea Dollin cloud API."""

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
        self, path: str, body_dict: dict[str, Any], *,
        use_iot_key: bool = False,
    ) -> dict[str, Any]:
        """Send a v1 API request with Scheme 1 signing."""
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
        if code in (14005, 12001):
            raise AuthError("Access token expired or invalid.")

        return result

    # -- Token persistence --

    def save_token(self, filepath: Path | None = None) -> None:
        """Save the access token to a JSON file."""
        if filepath is None:
            return
        data: dict[str, Any] = {}
        if filepath.exists():
            try:
                data = json.loads(filepath.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        data["access_token"] = self._access_token
        data["saved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        filepath.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8",
        )

    def load_token(self, filepath: Path | None = None) -> bool:
        """Load access token from a saved file."""
        if filepath is None or not filepath.exists():
            return False
        try:
            data = json.loads(filepath.read_text(encoding="utf-8"))
            token = data.get("access_token")
            if token:
                self._access_token = token
                return True
        except (json.JSONDecodeError, OSError, KeyError):
            pass
        return False

    # -- Public API methods --

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

        raise AuthError(
            f"Login failed: code={result.get('code')}, msg={result.get('msg')}"
        )

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

    def query_status(self, appliance_code: str) -> ITSStatus:
        """Query heat pump status (subtype 0x01)."""
        command = build_c3_query(0x01)
        response_hex = self.send_hex_command(appliance_code, command)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        return decode_its_status(body)

    def query_sensors(self, appliance_code: str) -> ITSSensors:
        """Query heat pump sensors (subtype 0x02)."""
        command = build_c3_query(0x02)
        response_hex = self.send_hex_command(appliance_code, command)
        raw = parse_hex_response(response_hex)
        _body_type, body = extract_c3_body(raw)
        return decode_its_sensors(body)

    def set_device(
        self,
        appliance_code: str,
        *,
        mode: int | None = None,
        temperature: int | None = None,
        boost: bool | None = None,
        mute: int | None = None,
        mute_level: int | None = None,
        power_on: bool = False,
        last_on_state: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """Send a SET command to the heat pump.

        Queries current status first to obtain echo bytes and current values,
        merges the requested changes, validates temperature ranges, builds
        the SET frame, and sends it.
        """
        # Query current status for echo bytes
        command = build_c3_query(0x01)
        response_hex = self.send_hex_command(appliance_code, command)
        raw = parse_hex_response(response_hex)
        _body_type, status_body = extract_c3_body(raw)
        status = decode_its_status(status_body)

        current_set_mode = QUERY_TO_SET_MODE.get(status.mode, MODE_OFF)

        # Handle power_on: restore last on-state
        if power_on and last_on_state is not None:
            if mode is None:
                mode = last_on_state[0]
            if temperature is None:
                temperature = last_on_state[1]

        eff_mode = mode if mode is not None else current_set_mode
        # Use active mode setpoint (t5s_def, offset-decoded) not DHW target (set_temperature)
        if temperature is not None:
            eff_temp = temperature
        elif status.t5s_def is not None:
            eff_temp = int(status.t5s_def)
        else:
            eff_temp = status.set_temperature
        temp_explicitly_set = temperature is not None

        # Determine ctrl_flag and mute_level
        eff_ctrl_flag = 0x00
        eff_mute_level = 0x00

        if boost is True:
            eff_ctrl_flag = 0x02
        elif mute is not None:
            if mute == 0:
                eff_ctrl_flag = 0x00
            else:
                eff_ctrl_flag = 0x01
                eff_mute_level = 0x00 if mute == 1 else 0x01

        if mute_level is not None:
            eff_mute_level = mute_level

        # Validate temperature
        if temp_explicitly_set and eff_mode in TEMP_RANGES and eff_mode != MODE_OFF:
            temp_min, temp_max = TEMP_RANGES[eff_mode]
            if not (temp_min <= eff_temp <= temp_max):
                mode_name = {v: k for k, v in MODE_MAP.items()}.get(eff_mode, "unknown")
                raise ValueError(
                    f"Temperature {eff_temp}C out of range for {mode_name} mode "
                    f"(allowed: {temp_min}-{temp_max}C)"
                )

        # Build and send
        set_hex = build_c3_set(
            mode=eff_mode,
            temperature=eff_temp,
            status_body=status_body,
            mute_level=eff_mute_level,
            ctrl_flag=eff_ctrl_flag,
        )

        set_response_hex = self.send_hex_command(appliance_code, set_hex)

        return {
            "sent": set_hex,
            "response": set_response_hex,
            "effective_mode": eff_mode,
            "effective_temp": eff_temp,
        }
