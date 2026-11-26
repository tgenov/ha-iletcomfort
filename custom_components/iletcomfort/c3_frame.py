"""Low-level Midea 0xC3 wire framing, checksums, and offset helpers."""

from __future__ import annotations

DEVICE_TYPE_C3 = 0xC3
TEMP_OFFSET = 35
SENSOR_DISCONNECTED = 204


def build_c3_frame(message_type: int, body: bytes) -> str:
    """Build an AA-framed C3 message: header, ``body`` and checksum.

    Shared by the status query (message type 0x03) and the standard Midea C3
    SET (message type 0x02) used for sn8 171H120F (see ``c3_basic``).
    """
    header = [
        0xAA, 0x00, DEVICE_TYPE_C3,
        0x00, 0x00, 0x00, 0x00, 0x00,
        0x00, message_type,
    ]
    frame = header + list(body)
    frame[1] = len(frame)
    checksum = (~sum(frame[1:]) + 1) & 0xFF
    frame.append(checksum)
    return bytes(frame).hex()


def build_c3_query(subtype: int) -> str:
    """Build a C3 query command frame."""
    return build_c3_frame(0x03, bytes([subtype]))


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
