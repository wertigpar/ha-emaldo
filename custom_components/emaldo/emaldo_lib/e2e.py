"""E2E (UDP) protocol for direct device communication.

Used for reading and writing charge/discharge override schedules
and for retrieving realtime power flow data directly from the device.
The protocol uses AES-256-CBC encryption over UDP.
"""

import json
import random
import socket
import string
import struct
import time
from typing import Callable

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

from .const import (
    DEFAULT_E2E_HOST,
    DEFAULT_E2E_PORT,
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    SLOT_NO_OVERRIDE,
    get_app_id,
)
from .exceptions import EmaldoE2EError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def generate_nonce(length: int = 16) -> str:
    """Generate a random alphanumeric nonce."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def generate_msg_id() -> str:
    """Generate a message ID: ``and_`` + 10 random chars + 13-digit ms timestamp."""
    chars = string.ascii_letters + string.digits
    rand_part = "".join(random.choice(chars) for _ in range(10))
    ts_ms = str(int(time.time() * 1000))
    return f"and_{rand_part}{ts_ms}"


def encrypt_payload(plaintext: bytes, key: str, nonce: str) -> bytes:
    """Encrypt with AES-256-CBC + PKCS#7 padding."""
    cipher = AES.new(key.encode(), AES.MODE_CBC, iv=nonce.encode())
    return cipher.encrypt(pad(plaintext, AES.block_size))


# ---------------------------------------------------------------------------
# Packet builders
# ---------------------------------------------------------------------------

def build_override_packet(
    e2e_creds: dict,
    slot_values: bytes,
    nonce: str | None = None,
    msg_id: str | None = None,
    *,
    high_marker: int = DEFAULT_MARKER_HIGH,
    low_marker: int = DEFAULT_MARKER_LOW,
) -> bytes:
    """Build an E2E override UDP packet (type 0x1a).

    Args:
        e2e_creds: Credentials dict from :func:`login`.
        slot_values: 96 or 192 bytes of slot override values.
        nonce: 16-char session nonce (generated if *None*).
        msg_id: 27-char message ID (generated if *None*).
        high_marker: High battery marker percentage (default 72).
        low_marker: Low battery marker percentage (default 20).

    Returns:
        Complete UDP packet ready to send.
    """
    if nonce is None:
        nonce = generate_nonce()
    if msg_id is None:
        msg_id = generate_msg_id()

    n_slots = len(slot_values)
    assert n_slots in (96, 192)
    assert len(nonce) == 16
    assert len(msg_id) == 27

    # Payload: 4-byte header + slot bytes
    # Header: [high_marker, low_marker, version_flag, slot_count]
    override_payload = bytes([high_marker, low_marker, 0x00, n_slots]) + slot_values
    encrypted = encrypt_payload(override_payload, e2e_creds["chat_secret"], nonce)

    pkt = bytes([0xD9, 0xA0, 0xA0])
    pkt += e2e_creds["sender_end_id"].encode()
    pkt += bytes([0xA0, 0xA1])
    pkt += e2e_creds["sender_group_id"].encode()
    pkt += bytes([0x84, 0xF1, 0x00, 0x00, 0x00, 0x01])
    pkt += bytes([0xA0, 0xA2])
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])
    pkt += nonce.encode()
    pkt += bytes([0xA0, 0xB5])
    pkt += get_app_id().encode()
    pkt += bytes([0x82, 0xF5, 0x1A])
    pkt += bytes([0xA0, 0x9B, 0xF6])
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])
    pkt += b"application/byte"
    pkt += encrypted
    return pkt


def build_subscription_packet(
    e2e_creds: dict,
    msg_type: int,
    nonce: str,
    msg_id: str | None = None,
    payload: bytes = b"",
    *,
    request_mode: bool = False,
) -> bytes:
    """Build a subscription or request packet.

    Args:
        request_mode: If *True*, use ``0x10`` mode byte (direct request to
            device, e.g. battery info).  If *False* (default), use ``0xA0``
            (subscribe to server-held state, e.g. overrides).
    """
    if msg_id is None:
        msg_id = generate_msg_id()

    assert len(nonce) == 16
    assert len(msg_id) == 27

    encrypted = encrypt_payload(payload, e2e_creds["chat_secret"], nonce)

    mode_byte = 0x10 if request_mode else 0xA0

    pkt = bytes([0xD9, 0xA0, 0xA0])
    pkt += e2e_creds["sender_end_id"].encode()
    pkt += bytes([0xA0, 0xA1])
    pkt += e2e_creds["sender_group_id"].encode()
    pkt += bytes([0x84, 0xF1, 0x00, 0x00, 0x00, 0x01])
    pkt += bytes([0xA0, 0xA2])
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])
    pkt += nonce.encode()
    pkt += bytes([0xA0, 0xB5])
    pkt += get_app_id().encode()
    pkt += bytes([0x82, 0xF5, msg_type])
    pkt += bytes([mode_byte, 0x9B, 0xF6])
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])
    pkt += b"application/byte"
    pkt += encrypted
    return pkt


def build_alive_packet(
    sender_end_id: str,
    sender_group_id: str,
    end_secret: str,
    nonce: str | None = None,
    msg_id: str | None = None,
) -> bytes:
    """Build an alive / keepalive packet (173 bytes)."""
    if nonce is None:
        nonce = generate_nonce()
    if msg_id is None:
        msg_id = generate_msg_id()

    assert len(nonce) == 16
    assert len(msg_id) == 27

    payload_json = json.dumps(
        {"__time": int(time.time())}, separators=(",", ":")
    ).encode()
    encrypted = encrypt_payload(payload_json, end_secret, nonce)

    pkt = bytes([0xD9, 0xA0, 0xA0])
    pkt += sender_end_id.encode()
    pkt += bytes([0xA0, 0xA1])
    pkt += sender_group_id.encode()
    pkt += bytes([0x90, 0xA3])
    pkt += nonce.encode()
    pkt += bytes([0x85, 0xF5])
    pkt += b"alive"
    pkt += bytes([0x9B, 0xF6])
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])
    pkt += b"application/json"
    pkt += encrypted
    return pkt


def build_heartbeat_packet(
    e2e_creds: dict,
    session_nonce: str,
    msg_id: str | None = None,
) -> bytes:
    """Build a heartbeat packet (251 bytes)."""
    if msg_id is None:
        msg_id = generate_msg_id()

    assert len(session_nonce) == 16
    assert len(msg_id) == 27

    payload_json = json.dumps(
        {"__time": int(time.time())}, separators=(",", ":")
    ).encode()
    encrypted = encrypt_payload(payload_json, e2e_creds["chat_secret"], session_nonce)

    pkt = bytes([0xD9, 0xA0, 0xA0])
    pkt += e2e_creds["sender_end_id"].encode()
    pkt += bytes([0xA0, 0xA1])
    pkt += e2e_creds["sender_group_id"].encode()
    pkt += bytes([0x84, 0xF1, 0x00, 0x00, 0x00, 0x01])
    pkt += bytes([0xA0, 0xA2])
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])
    pkt += session_nonce.encode()
    pkt += bytes([0x89, 0xF5])
    pkt += b"heartbeat"
    pkt += bytes([0xA0, 0xB5])
    pkt += get_app_id().encode()
    pkt += bytes([0x9B, 0xF6])
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])
    pkt += b"application/json"
    pkt += encrypted
    return pkt


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

# Known payload header signatures for different message types.
HEADER_OVERRIDE = (0x48, 0x14)    # Override state (type 0x1b) — default markers 72/20
HEADER_BATTERY = (0x03, 0x00)     # Battery info (type 0x06)

_DEFAULT_HEADERS = {HEADER_OVERRIDE, HEADER_BATTERY}


def _is_override_payload(payload: bytes) -> bool:
    """Check if decrypted payload looks like an override subscription response.

    Override subscription response:
        byte 0-1: high/low markers (dynamic)
        byte 2:   version / dirty flag
        byte 3:   variable
        byte 8:   slot count (0x60=96 or 0xC0=192)
    """
    if len(payload) < 105:
        return False
    return payload[8] in (0x60, 0xC0)


def decrypt_response(
    data: bytes,
    key: str,
    *,
    accepted_headers: set[tuple[int, int]] | None = None,
    payload_validator: Callable[[bytes], bool] | None = None,
) -> bytes | None:
    """Decrypt the encrypted payload from an E2E response packet.

    Scans for the AES nonce/IV (signalled by ``90 a3`` or ``10 a3`` tags)
    then tries every 16-aligned tail offset until a valid AES-CBC block
    passes validation.

    Args:
        data: Raw UDP response bytes.
        key: Chat secret (AES-256 key).
        accepted_headers: Set of ``(byte0, byte1)`` tuples to accept.
            Defaults to override + battery headers.  Ignored when
            *payload_validator* is given.
        payload_validator: Optional callable that receives the decrypted
            payload and returns *True* if valid.  When provided, this
            replaces the *accepted_headers* check.

    Returns:
        Decrypted payload bytes, or *None* on failure.
    """
    if payload_validator is None and accepted_headers is None:
        accepted_headers = _DEFAULT_HEADERS

    key_bytes = key.encode()[:32]

    # Collect all candidate nonces after both nonce tag variants
    nonces: list[bytes] = []
    for marker in [b"\x90\xa3", b"\x10\xa3"]:
        idx = 0
        while idx < len(data) - 18:
            pos = data.find(marker, idx)
            if pos < 0:
                break
            candidate = data[pos + 2 : pos + 18]
            if len(candidate) == 16 and all(32 <= b < 127 for b in candidate):
                nonces.append(candidate)
            idx = pos + 1

    if not nonces:
        return None

    for nonce in nonces:
        for offset in range(len(data) - 16, 39, -1):
            remaining = len(data) - offset
            if remaining % 16 != 0:
                continue
            try:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv=nonce)
                decrypted = unpad(cipher.decrypt(data[offset:]), AES.block_size)
                if payload_validator is not None:
                    if payload_validator(decrypted):
                        return decrypted
                elif len(decrypted) >= 2 and (decrypted[0], decrypted[1]) in accepted_headers:
                    return decrypted
            except (ValueError, KeyError):
                continue

    return None


def parse_override_state(payload: bytes) -> dict | None:
    """Parse a type 0x1b response payload into override state.

    Payload format:
        Byte 0:   high battery marker (percentage)
        Byte 1:   low battery marker (percentage)
        Byte 2:   version / dirty flag
        Byte 3:   ``0x58`` (subscription response tag)
        Bytes 4-7: extended header
        Byte 8:   slot count (``0x60``=96 or ``0xC0``=192)
        Bytes 9+:  slot values

    Slot values:
        ``0x80`` = no override, ``0x00`` = idle,
        1-100 = charge when battery < value%,
        129-255 = discharge when battery > (256 - value)%.

    Returns:
        Dict with ``slots`` (list of 96 or 192 ints), ``high_marker``,
        and ``low_marker``; or *None* on invalid input.
    """
    if payload is None or len(payload) < 105:
        return None
    n_slots = payload[8]
    if n_slots not in (0x60, 0xC0):
        return None
    if len(payload) < 9 + n_slots:
        return None
    return {
        "high_marker": payload[0],
        "low_marker": payload[1],
        "slots": list(payload[9 : 9 + n_slots]),
    }


def parse_battery_data(payload: bytes) -> dict | None:
    """Parse a type 0x06 battery-info response payload.

    Payload layout (≥80 bytes, variable due to length-prefixed strings):

    ======  ====  =========================  ================================
    Offset  Size  Field                      Description
    ======  ====  =========================  ================================
    0-1     2     state_flags                LE uint16 (bit0=discharge on,
                                             bit1=charge on, bits 2-15=faults)
    2-3     2     bms_temp_raw               LE uint16 deciKelvin
    4-5     2     electrode_a_temp_raw       LE uint16 deciKelvin
    6-7     2     electrode_b_temp_raw       LE uint16 deciKelvin
    8-9     2     voltage_mv                 LE uint16 millivolts
    10-13   4     current_ma                 LE int32 signed milliamps
                                             (negative = discharging)
    14-15   2     soc                        LE uint16 percent
    16-17   2     current_energy_wh          LE uint16 Wh
    18-19   2     full_energy_wh             LE uint16 Wh
    20-21   2     cycle_count                LE uint16
    22-23   2     soh                        LE uint16 percent
    24      1     id_info_len                length N₁ (usually 0)
    25…     N₁    id_info                    ASCII (optional)
    +1      1     version_len                length N₂
    …       N₂    version                    ASCII model string
    +1      1     barcode_len                length N₃
    …       N₃    barcode                    ASCII serial number
    +1      1     index                      battery instance index
    +1      1     cabinet_index              cabinet slot index
    +1      1     cabinet_position_index     position within cabinet
    +2      2     capacity                   LE uint16
    ======  ====  =========================  ================================

    Returns:
        Dict with decoded fields, or *None* if payload is invalid.
    """
    if payload is None or len(payload) < 26:
        return None
    if payload[0] != HEADER_BATTERY[0] or payload[1] != HEADER_BATTERY[1]:
        return None

    def _deci_kelvin_to_c(raw: int) -> float:
        return round(raw / 10.0 - 273.15, 1)

    state_flags = struct.unpack_from("<H", payload, 0)[0]
    bms_temp_raw = struct.unpack_from("<H", payload, 2)[0]
    electrode_a_raw = struct.unpack_from("<H", payload, 4)[0]
    electrode_b_raw = struct.unpack_from("<H", payload, 6)[0]
    voltage_mv = struct.unpack_from("<H", payload, 8)[0]
    current_ma = struct.unpack_from("<i", payload, 10)[0]
    soc = struct.unpack_from("<H", payload, 14)[0]
    current_energy_wh = struct.unpack_from("<H", payload, 16)[0]
    full_energy_wh = struct.unpack_from("<H", payload, 18)[0]
    cycle_count = struct.unpack_from("<H", payload, 20)[0]
    soh = struct.unpack_from("<H", payload, 22)[0]

    # Variable-length strings: id_info, version (model), barcode (serial)
    pos = 24
    id_info_len = payload[pos]; pos += 1
    id_info = payload[pos : pos + id_info_len].decode("ascii", errors="replace") if id_info_len else ""
    pos += id_info_len

    if pos >= len(payload):
        return None
    version_len = payload[pos]; pos += 1
    model = payload[pos : pos + version_len].decode("ascii", errors="replace") if version_len else ""
    pos += version_len

    if pos >= len(payload):
        return None
    barcode_len = payload[pos]; pos += 1
    serial = payload[pos : pos + barcode_len].decode("ascii", errors="replace") if barcode_len else ""
    pos += barcode_len

    # Trailing fixed fields
    index = payload[pos] if pos < len(payload) else 0; pos += 1
    cabinet_index = payload[pos] if pos < len(payload) else 0; pos += 1
    cabinet_position = payload[pos] if pos < len(payload) else 0; pos += 1
    capacity = struct.unpack_from("<H", payload, pos)[0] if pos + 2 <= len(payload) else 0

    return {
        "state_flags": state_flags,
        "discharge_on": bool(state_flags & 0x01),
        "charge_on": bool(state_flags & 0x02),
        "fault_bits": (state_flags >> 2) & 0x3FFF,
        "bms_temp_c": _deci_kelvin_to_c(bms_temp_raw),
        "electrode_a_temp_c": _deci_kelvin_to_c(electrode_a_raw),
        "electrode_b_temp_c": _deci_kelvin_to_c(electrode_b_raw),
        "voltage_v": round(voltage_mv / 1000.0, 2),
        "current_a": round(current_ma / 1000.0, 2),
        "soc": soc,
        "current_energy_wh": current_energy_wh,
        "full_energy_wh": full_energy_wh,
        "cycle_count": cycle_count,
        "soh": soh,
        "id_info": id_info,
        "model": model,
        "serial": serial,
        "index": index,
        "cabinet_index": cabinet_index,
        "cabinet_position": cabinet_position,
        "capacity": capacity,
    }


def _is_power_flow_payload(payload: bytes) -> bool:
    """Check if decrypted payload looks like a power flow response.

    Power flow responses are 16–24 bytes of signed-short watt values.
    Heuristic: correct length range, reasonable watt values, and
    boolean flags at bytes 16–17 must be 0 or 1.
    """
    if len(payload) < 16 or len(payload) > 24:
        return False
    # First two shorts should be reasonable watt values
    battery_w = struct.unpack_from("<h", payload, 0)[0]
    solar_w = struct.unpack_from("<h", payload, 2)[0]
    if abs(battery_w) >= 30000 or abs(solar_w) >= 30000:
        return False
    # Bytes 16–17 are boolean flags (gridValid, bsensorValid)
    if len(payload) >= 18:
        if payload[16] not in (0, 1) or payload[17] not in (0, 1):
            return False
    return True


def parse_power_flow(payload: bytes) -> dict | None:
    """Parse a type 0x30 power-flow response payload.

    This is the ``GET_GLOBAL_CURRENT_FLOW_INFO`` command response.
    The app screen calls it "Realtime Power".

    Payload layout (16–22 bytes, little-endian):

    ======  ====  ======================  ================================
    Offset  Size  Field                   Description
    ======  ====  ======================  ================================
    0-1     2     battery_w               signed short – battery power
                                          (hectowatts, ×100 = W).
                                          positive = charging,
                                          negative = discharging
    2-3     2     solar_w                 signed short – solar/PV power
    4-5     2     grid_w                  signed short – grid power
                                          positive = importing,
                                          negative = exporting
    6-7     2     addition_load_w         signed short – additional load
    8-9     2     other_load_w            signed short – other load
    10-11   2     ev_w                    signed short – EV charger
    12-13   2     ip2_w                   unsigned short – input port 2
    14-15   2     op2_w                   unsigned short – output port 2
    16      1     grid_valid              bool – grid CT sensor present
    17      1     bsensor_valid           bool – battery sensor present
    18      1     solar_efficiency        enum – solar efficiency type
    19      1     thirdparty_pv_on        bool – 3rd-party PV enabled
    20-21   2     dual_power_w            signed short – household +
                                          solar combined (W)
    ======  ====  ======================  ================================

    Returns:
        Dict with decoded power flow values, or *None* if invalid.
    """
    if payload is None or len(payload) < 16:
        return None

    # Protocol values are in units of 100 W (hectowatts).
    _scale = 100
    battery_w = struct.unpack_from("<h", payload, 0)[0] * _scale
    solar_w = struct.unpack_from("<h", payload, 2)[0] * _scale
    grid_w = struct.unpack_from("<h", payload, 4)[0] * _scale
    addition_load_w = struct.unpack_from("<h", payload, 6)[0] * _scale
    other_load_w = struct.unpack_from("<h", payload, 8)[0] * _scale
    ev_w = struct.unpack_from("<h", payload, 10)[0] * _scale
    ip2_w = struct.unpack_from("<H", payload, 12)[0] * _scale
    op2_w = struct.unpack_from("<H", payload, 14)[0] * _scale

    # Extended fields (bytes 16-21) may be absent in older firmware
    length = len(payload)
    max_len = max(length, 22)
    buf = bytearray(max_len)
    buf[:length] = payload
    # Pad missing bytes with defaults (match Java logic)
    for i in range(length, max_len):
        buf[i] = 1 if i in (16, 17) else 0

    grid_valid = buf[16] == 1
    bsensor_valid = buf[17] == 1
    solar_efficiency = buf[18]
    thirdparty_pv_on = buf[19] == 1
    dual_power_w = struct.unpack_from("<h", buf, 20)[0] * _scale

    return {
        "battery_w": battery_w,
        "solar_w": solar_w,
        "grid_w": grid_w,
        "addition_load_w": addition_load_w,
        "other_load_w": other_load_w,
        "ev_w": ev_w,
        "ip2_w": ip2_w,
        "op2_w": op2_w,
        "grid_valid": grid_valid,
        "bsensor_valid": bsensor_valid,
        "solar_efficiency": solar_efficiency,
        "thirdparty_pv_on": thirdparty_pv_on,
        "dual_power_w": dual_power_w,
    }


# ---------------------------------------------------------------------------
# High-level session flows
# ---------------------------------------------------------------------------

def _resolve_host(host_port: str) -> tuple[str, int]:
    """Split ``host:port`` string; default to :const:`DEFAULT_E2E_PORT`."""
    if ":" in host_port:
        host, port_s = host_port.rsplit(":", 1)
        return host, int(port_s)
    return host_port, DEFAULT_E2E_PORT


def _run_session(
    e2e_creds: dict,
    action_packets: list[tuple[str, bytes]],
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> list[tuple[str, bytes | None]]:
    """Execute a full E2E UDP session.

    Sends alive(home), alive(device), heartbeat, then the supplied
    *action_packets* in order.  Returns ``[(label, response_bytes), ...]``
    for the action packets only.
    """
    home_alive_nonce = generate_nonce()
    dev_alive_nonce = generate_nonce()
    session_nonce = generate_nonce()

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
        nonce=home_alive_nonce,
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
        nonce=dev_alive_nonce,
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → no response")
            return None

    results: list[tuple[str, bytes | None]] = []
    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        for label, pkt in action_packets:
            resp = _send(pkt, label)
            results.append((label, resp))
    finally:
        sock.close()

    return results


def read_overrides(
    e2e_creds: dict,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read current override state via E2E subscription.

    Performs the full session flow (alive → heartbeat → subscribe 0x1b)
    and returns a dict with ``slots`` (96 ints), ``high_marker``,
    and ``low_marker``; or *None* on failure.

    The device override function is day-scoped: only 96 slots (today)
    are returned and meaningful.  Tomorrow's schedule must be pushed
    fresh after midnight.
    """
    session_nonce = generate_nonce()

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)
    sub_pkt = build_subscription_packet(e2e_creds, 0x1B, session_nonce)

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → no response")
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(sub_pkt, "Subscribe(0x1b)")
        if not resp:
            return None

        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_override_payload,
        )
        state = parse_override_state(decrypted)
        if state is not None:
            return state

        # The first response may not be the right type; try a few more
        for _ in range(5):
            try:
                resp, _ = sock.recvfrom(4096)
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_override_payload,
                )
                state = parse_override_state(decrypted)
                if state is not None:
                    return state
            except socket.timeout:
                break

        return None
    finally:
        sock.close()


def read_battery_info(
    e2e_creds: dict,
    *,
    timeout: float = 5.0,
    max_batteries: int = 10,
    log: Callable[..., None] | None = None,
) -> list[dict]:
    """Read battery cell info via E2E request (type 0x06).

    Performs the full session flow (alive → heartbeat) then sends one
    request per cabinet index (0 … *max_batteries*-1).  The device
    responds with battery data for each valid index.

    Returns:
        List of battery-info dicts (one per cell), possibly empty.
    """
    session_nonce = generate_nonce()

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → no response")
            return None

    def _try_parse_battery(resp: bytes) -> dict | None:
        """Attempt to decrypt + parse a battery response."""
        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            accepted_headers={HEADER_BATTERY},
        )
        return parse_battery_data(decrypted)

    batteries: list[dict] = []
    seen_serials: set[str] = set()

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        # Send all battery requests, then collect responses.
        # The app sends one request per cabinet index.
        request_pkts = []
        for idx in range(max_batteries):
            req = build_subscription_packet(
                e2e_creds, 0x06, session_nonce,
                payload=bytes([idx]),
                request_mode=True,
            )
            request_pkts.append((idx, req))

        consecutive_short = 0
        for idx, req_pkt in request_pkts:
            resp = _send(req_pkt, f"Battery(idx={idx})")
            if not resp:
                consecutive_short += 1
                if consecutive_short >= 2:
                    break
                continue

            # A short response (e.g. 206B vs 275B) means no battery at
            # this index — stop probing after one such reply.
            if len(resp) < 250:
                consecutive_short += 1
                if consecutive_short >= 1:
                    break
                continue

            info = _try_parse_battery(resp)
            if info is None:
                # First response might be an echo/ACK; try reading next
                try:
                    resp2, _ = sock.recvfrom(4096)
                    if log:
                        log(f"  follow-up: {len(resp2)}B")
                    info = _try_parse_battery(resp2)
                except socket.timeout:
                    pass

            if info and info["serial"] not in seen_serials:
                seen_serials.add(info["serial"])
                batteries.append(info)
                consecutive_short = 0
            else:
                consecutive_short += 1
                if consecutive_short >= 2:
                    break

        return batteries
    finally:
        sock.close()


def _log_power_flow_raw(payload: bytes, log: Callable[..., None]) -> None:
    """Dump raw power flow payload for debugging."""
    log(f"Raw payload ({len(payload)}B): {payload.hex()}")
    if len(payload) >= 2:
        log(f"  [0:2]   batteryWat      = {struct.unpack_from('<h', payload, 0)[0]}")
    if len(payload) >= 4:
        log(f"  [2:4]   solarWat        = {struct.unpack_from('<h', payload, 2)[0]}")
    if len(payload) >= 6:
        log(f"  [4:6]   gridWat         = {struct.unpack_from('<h', payload, 4)[0]}")
    if len(payload) >= 8:
        log(f"  [6:8]   additionLoadWat = {struct.unpack_from('<h', payload, 6)[0]}")
    if len(payload) >= 10:
        log(f"  [8:10]  otherLoadWat    = {struct.unpack_from('<h', payload, 8)[0]}")
    if len(payload) >= 12:
        log(f"  [10:12] vechiWat        = {struct.unpack_from('<h', payload, 10)[0]}")
    if len(payload) >= 14:
        log(f"  [12:14] ip2Wat          = {struct.unpack_from('<H', payload, 12)[0]}")
    if len(payload) >= 16:
        log(f"  [14:16] op2Wat          = {struct.unpack_from('<H', payload, 14)[0]}")
    if len(payload) >= 17:
        log(f"  [16]    gridValid       = {payload[16]}")
    if len(payload) >= 18:
        log(f"  [17]    bsensorValid    = {payload[17]}")
    if len(payload) >= 19:
        log(f"  [18]    solarEfficiency = {payload[18]}")
    if len(payload) >= 20:
        log(f"  [19]    thirdpartyPVOn  = {payload[19]}")
    if len(payload) >= 22:
        log(f"  [20:22] dualPowerWat    = {struct.unpack_from('<h', payload, 20)[0]}")


def read_power_flow(
    e2e_creds: dict,
    *,
    timeout: float = 5.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read realtime power flow via E2E (type 0x30).

    Sends ``GET_GLOBAL_CURRENT_FLOW_INFO`` and returns a dict with
    ``battery_w``, ``solar_w``, ``grid_w``, ``dual_power_w``, etc.
    Returns *None* on failure.
    """
    session_nonce = generate_nonce()

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)
    power_pkt = build_subscription_packet(
        e2e_creds, 0x30, session_nonce, payload=bytes([0x01]),
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → no response")
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(power_pkt, "PowerFlow(0x30)")
        if not resp:
            return None

        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_power_flow_payload,
        )
        if decrypted is not None and log:
            _log_power_flow_raw(decrypted, log)
        result = parse_power_flow(decrypted)
        if result is not None:
            return result

        # First response may be an echo/ACK; try a few more
        for _ in range(5):
            try:
                resp, _ = sock.recvfrom(4096)
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_power_flow_payload,
                )
                if decrypted is not None and log:
                    _log_power_flow_raw(decrypted, log)
                result = parse_power_flow(decrypted)
                if result is not None:
                    return result
            except socket.timeout:
                break

        return None
    finally:
        sock.close()


def send_override(
    e2e_creds: dict,
    slot_values: bytes,
    *,
    high_marker: int = DEFAULT_MARKER_HIGH,
    low_marker: int = DEFAULT_MARKER_LOW,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Send override slot values via E2E protocol.

    Performs the full session flow and sends the override packet.
    Returns *True* if the server acknowledged the override.
    """
    session_nonce = generate_nonce()

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)
    override_pkt = build_override_packet(
        e2e_creds, slot_values, nonce=session_nonce,
        high_marker=high_marker, low_marker=low_marker,
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → no response")
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(override_pkt, "Override")
        if resp and len(resp) == 161:
            return True
        # Non-standard response size is still considered sent
        return resp is not None
    finally:
        sock.close()


def send_sell(
    e2e_creds: dict,
    duration_seconds: int,
    *,
    label: str = "Sell",
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Send a sell (discharge-to-grid) command via E2E.

    Uses type 0x01, subscribe mode with a 9-byte payload:
    ``[0x01, start_timestamp_LE32, end_timestamp_LE32]``.

    Args:
        duration_seconds: How long the sell window should last.
        timeout: UDP timeout in seconds.
        log: Optional log callback.

    Returns:
        *True* if the server acknowledged the command.
    """
    session_nonce = generate_nonce()

    start_ts = int(time.time())
    end_ts = start_ts + duration_seconds
    payload = struct.pack("<BII", 0x01, start_ts, end_ts)

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)
    sell_pkt = build_subscription_packet(
        e2e_creds, 0x01, session_nonce,
        payload=payload,
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 no response")
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(sell_pkt, label)
        if resp and len(resp) == 161:
            return True
        return resp is not None
    finally:
        sock.close()


def cancel_sell(
    e2e_creds: dict,
    *,
    label: str = "Cancel sell",
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Cancel an active sell command.

    Sends 9 zero bytes on type 0x01 subscribe.
    Returns *True* if the server acknowledged.
    """
    session_nonce = generate_nonce()
    payload = bytes(9)  # 9 zero bytes = cancel

    home_alive = build_alive_packet(
        sender_end_id=e2e_creds["home_end_id"],
        sender_group_id=e2e_creds["home_group_id"],
        end_secret=e2e_creds["home_end_secret"],
    )
    dev_alive = build_alive_packet(
        sender_end_id=e2e_creds["sender_end_id"],
        sender_group_id=e2e_creds["sender_group_id"],
        end_secret=e2e_creds["sender_end_secret"],
    )
    heartbeat = build_heartbeat_packet(e2e_creds, session_nonce)
    cancel_pkt = build_subscription_packet(
        e2e_creds, 0x01, session_nonce,
        payload=payload,
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 got {len(resp)}B")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 no response")
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(cancel_pkt, label)
        if resp and len(resp) == 161:
            return True
        return resp is not None
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Peak shaving
# ---------------------------------------------------------------------------

# E2E message types for peak shaving
_PS_TYPE_TOGGLE = 0x57       # Toggle on/off (1 byte payload)
_PS_TYPE_SET_POINTS = 0x58   # Set reserve percentages (2 bytes)
_PS_TYPE_ADD_SCHEDULE = 0x5A # Add/modify schedule (27 bytes)
_PS_TYPE_GET_CONFIG = 0x5B   # Subscribe → 20-byte config response
_PS_TYPE_GET_SCHEDULE = 0x5C # Subscribe → 28-byte schedule response
_PS_TYPE_SET_REDUNDANCY = 0x77  # Set redundancy value


def parse_peak_shaving_config(payload: bytes) -> dict | None:
    """Parse a 0x5B peak shaving config response (20 bytes).

    Returns:
        Dict with ``enabled``, ``peak_reserve_pct``, ``ups_reserve_pct``,
        and ``redundancy``; or *None* on invalid input.
    """
    if payload is None or len(payload) < 20:
        return None
    return {
        "enabled": bool(payload[0]),
        "peak_reserve_pct": payload[5],
        "ups_reserve_pct": payload[6],
        "redundancy": payload[18],
    }


def parse_peak_shaving_schedule(payload: bytes) -> dict | None:
    """Parse a 0x5C peak shaving schedule response (28 bytes).

    Times are LE uint32 seconds-from-midnight.

    Returns:
        Dict with ``schedule_id``, ``start_seconds``, ``end_seconds``,
        ``start_time`` (HH:MM), ``end_time`` (HH:MM), ``repeat_days``,
        ``min_peak_power_w``, and ``created_ts``; or *None* on invalid input.
    """
    if payload is None or len(payload) < 16:
        return None

    schedule_id = struct.unpack_from("<H", payload, 0)[0]
    all_day = bool(payload[2])  # 1 = all-day mode, 0 = use start/end times
    start_sec = struct.unpack_from("<I", payload, 3)[0]
    end_sec = struct.unpack_from("<I", payload, 7)[0]
    repeat_days = payload[11]
    min_peak_power = struct.unpack_from("<H", payload, 12)[0]

    created_ts = 0
    if len(payload) >= 20:
        created_ts = struct.unpack_from("<I", payload, 16)[0]

    def _fmt_time(secs: int) -> str:
        h, m = divmod(secs // 60, 60)
        return f"{h:02d}:{m:02d}"

    # Keep trailing bytes (offsets 16+ in response, after the 2 pad bytes)
    # so they can be passed back when updating the schedule via 0x5A.
    # The pad bytes at [14-15] are added by _build_schedule_payload itself.
    _trailing = bytes(payload[16:]) if len(payload) > 16 else b""

    return {
        "schedule_id": schedule_id,
        "all_day": all_day,
        "start_seconds": start_sec,
        "end_seconds": end_sec,
        "start_time": _fmt_time(start_sec),
        "end_time": _fmt_time(end_sec),
        "repeat_days": repeat_days,
        "min_peak_power_w": min_peak_power,
        "created_ts": created_ts,
        "_trailing": _trailing,
    }


def _build_schedule_payload(
    schedule_id: int,
    all_day: bool,
    start_seconds: int,
    end_seconds: int,
    repeat_days: int,
    min_peak_power_w: int,
    trailing: bytes = b"",
) -> bytes:
    """Build the 0x5A schedule SET payload (variable length).

    Layout: id(1B) + all_day(1B) + start(4B LE) + end(4B LE)
            + days(1B) + power(2B LE) + pad(2B) [+ trailing]
    """
    buf = bytes([schedule_id & 0xFF])
    buf += bytes([0x01 if all_day else 0x00])
    buf += struct.pack("<I", start_seconds)
    buf += struct.pack("<I", end_seconds)
    buf += bytes([repeat_days])
    buf += struct.pack("<H", min_peak_power_w)
    buf += bytes(2)  # padding
    if trailing:
        buf += trailing
    return buf


def read_peak_shaving(
    e2e_creds: dict,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> dict:
    """Read peak shaving config and schedule via E2E.

    Returns a dict with ``config`` (from 0x5B) and ``schedule`` (from 0x5C).
    Either value may be *None* if the response could not be parsed.
    """
    session_nonce = generate_nonce()
    chat_secret = e2e_creds["chat_secret"]

    sub_config = build_subscription_packet(
        e2e_creds, _PS_TYPE_GET_CONFIG, session_nonce,
    )
    sub_schedule = build_subscription_packet(
        e2e_creds, _PS_TYPE_GET_SCHEDULE, session_nonce,
    )

    def _accept_any(payload: bytes) -> bool:
        return True

    results = _run_session(
        e2e_creds,
        [("Subscribe(config)", sub_config),
         ("Subscribe(schedule)", sub_schedule)],
        timeout=timeout,
        log=log,
    )

    config = None
    schedule = None

    for label, resp in results:
        if resp is None:
            continue
        dec = decrypt_response(resp, chat_secret, payload_validator=_accept_any)
        if dec is None:
            continue
        if "config" in label.lower() and len(dec) >= 20:
            config = parse_peak_shaving_config(dec)
        elif "schedule" in label.lower() and len(dec) >= 16:
            schedule = parse_peak_shaving_schedule(dec)

    return {"config": config, "schedule": schedule}


def toggle_peak_shaving(
    e2e_creds: dict,
    enabled: bool,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Toggle peak shaving on or off (type 0x57).

    Returns *True* if the server acknowledged.
    """
    session_nonce = generate_nonce()
    payload = bytes([0x01 if enabled else 0x00])
    pkt = build_subscription_packet(
        e2e_creds, _PS_TYPE_TOGGLE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Toggle peak shaving", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def set_peak_shaving_points(
    e2e_creds: dict,
    peak_reserve_pct: int,
    ups_reserve_pct: int,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Set peak shaving reserve percentages (type 0x58).

    Args:
        peak_reserve_pct: Fixed peak reserve percentage (0-100).
        ups_reserve_pct: UPS reserve percentage (0-100).

    Returns *True* if the server acknowledged.
    """
    session_nonce = generate_nonce()
    payload = bytes([peak_reserve_pct, ups_reserve_pct])
    pkt = build_subscription_packet(
        e2e_creds, _PS_TYPE_SET_POINTS, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Set peak shaving points", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def set_peak_shaving_schedule(
    e2e_creds: dict,
    schedule_id: int,
    start_seconds: int,
    end_seconds: int,
    repeat_days: int,
    min_peak_power_w: int,
    *,
    all_day: bool = False,
    trailing: bytes = b"",
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Add or modify a peak shaving schedule (type 0x5A).

    Args:
        schedule_id: Schedule identifier.
        start_seconds: Start time as seconds from midnight.
        end_seconds: End time as seconds from midnight.
        repeat_days: Day-of-week bitmask.
        min_peak_power_w: Minimum peak power in watts.
        all_day: If *True*, ignore start/end times and run all day.
        trailing: Optional trailing bytes (timestamp + metadata from
            an existing schedule; omit for new schedules).

    Returns *True* if the server acknowledged.
    """
    session_nonce = generate_nonce()
    payload = _build_schedule_payload(
        schedule_id, all_day, start_seconds, end_seconds,
        repeat_days, min_peak_power_w, trailing,
    )
    pkt = build_subscription_packet(
        e2e_creds, _PS_TYPE_ADD_SCHEDULE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Set peak shaving schedule", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def set_peak_shaving_redundancy(
    e2e_creds: dict,
    redundancy: int,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Set peak shaving redundancy value (type 0x77).

    Returns *True* if the server acknowledged.
    """
    session_nonce = generate_nonce()
    payload = bytes([redundancy])
    pkt = build_subscription_packet(
        e2e_creds, _PS_TYPE_SET_REDUNDANCY, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Set peak shaving redundancy", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


# ---------------------------------------------------------------------------
# Persistent E2E Session (for real-time polling)
# ---------------------------------------------------------------------------

class PersistentE2ESession:
    """Long-lived E2E session that keeps a UDP socket open for fast polling.

    The default helpers (``read_power_flow``, ``read_overrides``, etc.) open a
    new UDP socket and run the full alive→heartbeat handshake for every call.
    For real-time monitoring this is too expensive.

    ``PersistentE2ESession`` performs the handshake once, then keeps the
    session alive with periodic keepalives. Subsequent action calls reuse the
    same socket and complete in a single request/response round trip.

    The session expires on the relay server after a few minutes without
    keepalive (status 21204). Call :meth:`keepalive` periodically (every
    ~15 seconds) from a background thread or asyncio task to prevent this.
    The session automatically re-runs the handshake on :meth:`read_power_flow`
    if a 21204 error is detected.

    Typical usage (synchronous)::

        from emaldo import EmaldoClient
        from emaldo.e2e import PersistentE2ESession

        client = EmaldoClient()
        client.login(email, password)
        creds = client.e2e_login(home_id, device_id, model)

        session = PersistentE2ESession(creds)
        session.connect()
        try:
            data = session.read_power_flow()  # fast — reuses socket
            print(data)
        finally:
            session.close()

    Typical usage (with background keepalive)::

        import threading

        session = PersistentE2ESession(creds)
        session.connect()

        def _keepalive_loop():
            while not session.closed:
                time.sleep(15)
                session.keepalive()

        threading.Thread(target=_keepalive_loop, daemon=True).start()
    """

    #: Keepalive interval in seconds. The relay server times out idle sessions
    #: after ~3 minutes; 15 seconds provides a generous safety margin.
    DEFAULT_KEEPALIVE_INTERVAL = 15

    #: Status code returned when the relay has dropped the session.
    SESSION_EXPIRED_STATUS = 21204

    def __init__(
        self,
        e2e_creds: dict,
        *,
        timeout: float = 5.0,
        log: Callable[..., None] | None = None,
    ) -> None:
        self._creds = e2e_creds
        self._timeout = timeout
        self._log = log
        self._sock: socket.socket | None = None
        self._addr: tuple[str, int] | None = None
        self._session_nonce: str | None = None
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._closed

    @property
    def connected(self) -> bool:
        """True when the session has an open socket and valid handshake."""
        return self._sock is not None and not self._closed

    def connect(self) -> None:
        """Open the UDP socket and run the alive+heartbeat handshake."""
        if self._sock is not None:
            return

        host, port = _resolve_host(self._creds["host"])
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self._timeout)

        self._session_nonce = generate_nonce()
        self._do_handshake()

    def _do_handshake(self) -> None:
        """Run alive(home) + alive(device) + heartbeat."""
        home_alive = build_alive_packet(
            sender_end_id=self._creds["home_end_id"],
            sender_group_id=self._creds["home_group_id"],
            end_secret=self._creds["home_end_secret"],
        )
        dev_alive = build_alive_packet(
            sender_end_id=self._creds["sender_end_id"],
            sender_group_id=self._creds["sender_group_id"],
            end_secret=self._creds["sender_end_secret"],
        )
        heartbeat = build_heartbeat_packet(self._creds, self._session_nonce)

        self._send_raw(home_alive, "Alive(home)")
        self._send_raw(dev_alive, "Alive(device)")
        self._send_raw(heartbeat, "Heartbeat")
        time.sleep(0.2)

    def keepalive(self) -> bool:
        """Send a fresh alive+heartbeat to keep the session alive.

        Returns:
            True on success, False if the session has been dropped or the
            socket is closed.
        """
        if self._sock is None or self._closed:
            return False

        try:
            dev_alive = build_alive_packet(
                sender_end_id=self._creds["sender_end_id"],
                sender_group_id=self._creds["sender_group_id"],
                end_secret=self._creds["sender_end_secret"],
            )
            heartbeat = build_heartbeat_packet(self._creds, self._session_nonce)
            self._send_raw(dev_alive, "Keepalive(alive)")
            self._send_raw(heartbeat, "Keepalive(heartbeat)")
            return True
        except Exception as err:  # noqa: BLE001 - best-effort keepalive
            if self._log:
                self._log(f"Keepalive failed: {err}")
            return False

    def read_power_flow(self) -> dict | None:
        """Read realtime power flow (0x30) over the existing session.

        Automatically re-runs the handshake if the relay has dropped the
        session (status 21204).
        """
        if self._sock is None or self._closed:
            raise EmaldoE2EError("Session is not connected")

        for attempt in range(2):
            power_pkt = build_subscription_packet(
                self._creds, 0x30, self._session_nonce, payload=bytes([0x01]),
            )
            resp = self._send_raw(power_pkt, "PowerFlow(0x30)")
            if resp is None:
                # Timeout — maybe session expired. Try reconnect once.
                if attempt == 0:
                    self._reconnect()
                    continue
                return None

            # Check for session-expired status
            if self._is_session_expired(resp):
                if self._log:
                    self._log("Session expired, reconnecting")
                if attempt == 0:
                    self._reconnect()
                    continue
                return None

            decrypted = decrypt_response(
                resp, self._creds["chat_secret"],
                payload_validator=_is_power_flow_payload,
            )
            result = parse_power_flow(decrypted)
            if result is not None:
                return result

            # Drain a few more in case we got an echo/ACK first
            for _ in range(5):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                    decrypted = decrypt_response(
                        more_resp, self._creds["chat_secret"],
                        payload_validator=_is_power_flow_payload,
                    )
                    result = parse_power_flow(decrypted)
                    if result is not None:
                        return result
                except socket.timeout:
                    break

            return None

        return None

    def close(self) -> None:
        """Close the socket and mark the session closed."""
        self._closed = True
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None

    def _send_raw(self, pkt: bytes, label: str) -> bytes | None:
        """Send a packet and read one response (no reconnect logic)."""
        if self._sock is None or self._addr is None:
            return None
        self._sock.sendto(pkt, self._addr)
        try:
            resp, _ = self._sock.recvfrom(4096)
            if self._log:
                self._log(f"{label}: sent {len(pkt)}B → got {len(resp)}B")
            return resp
        except socket.timeout:
            if self._log:
                self._log(f"{label}: sent {len(pkt)}B → no response")
            return None

    def _reconnect(self) -> None:
        """Close and re-open the session (used on 21204 or timeout)."""
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None
        host, port = _resolve_host(self._creds["host"])
        self._addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(self._timeout)
        self._session_nonce = generate_nonce()
        self._do_handshake()

    @classmethod
    def _is_session_expired(cls, resp: bytes) -> bool:
        """Check if a response contains the 21204 (session expired) status."""
        if resp is None or len(resp) < 2:
            return False
        # Parse the OPTION_STATUS (0xC0) field if present.
        pos = 1
        options = 0
        if resp[0] & 1:
            while pos + 1 < len(resp):
                length_byte = resp[pos]
                vl = length_byte & 0x7F
                has_more = bool(length_byte & 0x80)
                if pos + 2 + vl > len(resp):
                    break
                opt_type = resp[pos + 1]
                if opt_type == 0xC0 and vl == 2:
                    status = int.from_bytes(resp[pos + 2:pos + 4], "big")
                    return status == cls.SESSION_EXPIRED_STATUS
                pos += 2 + vl
                options += 1
                if not has_more:
                    break
        return False
