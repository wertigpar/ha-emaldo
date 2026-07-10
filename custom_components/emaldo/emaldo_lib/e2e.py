"""E2E (UDP) protocol for direct device communication.

Used for reading and writing charge/discharge override schedules
and for retrieving realtime power flow data directly from the device.
The protocol uses AES-256-CBC encryption over UDP.
"""

import json
import logging
import random
import socket
import string
import struct
import threading
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

_LOGGER = logging.getLogger(__name__)

# Rate-limited diagnostic for DECRYPTED-BUT-REJECTED events in decrypt_response.
# A "decrypted but rejected" event is when AES decrypts cleanly but the payload
# validator / accepted headers reject it — the #41 signal. The search loop
# produces one such event per (nonce, offset) candidate, so per-offset logging
# floods the log on every non-power-flow packet (ACKs, status pushes). We
# coalesce into a single periodic line that preserves the signal (event count +
# a sample payload) without the flood. The first event in a window is logged
# immediately so a genuine single rejection is never swallowed; subsequent
# events in the same window are counted and flushed at window expiry.
_decrypt_rejected_lock = threading.Lock()
_decrypt_rejected_count = 0
_decrypt_rejected_window_start = 0.0
_decrypt_rejected_sample: tuple[str, int, int, str] | None = None
_DECRYPT_REJECTED_WINDOW_S = 60.0


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
    battery_range_override: bool = False,
) -> bytes:
    """Build an E2E override UDP packet (type 0x1a).

    Args:
        e2e_creds: Credentials dict from :func:`login`.
        slot_values: 96 or 192 bytes of slot override values.
        nonce: 16-char session nonce (generated if *None*).
        msg_id: 27-char message ID (generated if *None*).
        high_marker: High battery marker percentage (default 72).
        low_marker: Low battery marker percentage (default 20).
        battery_range_override: When ``True`` sets byte 2 to 0x01 — this is
            the "AI Battery Range = override" flag that the app's Battery
            Range save sends (BmtCmd.SET_RESERVE_MODE_AI). When ``False``
            (default) byte 2 is 0x00 → Battery Range stays in AI mode and
            only the per-slot overrides apply.

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
    # Header: [high_marker, low_marker, enable_flag, slot_count]
    enable_byte = 0x01 if battery_range_override else 0x00
    override_payload = bytes([high_marker, low_marker, enable_byte, n_slots]) + slot_values
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

    # Option order matches module_bmt/yd/e.java:93:
    # END_ID, GROUP_ID, RECEIVER_ID, AES, PROXY(1B), APP_ID, METHOD(2B), MSGID, CT
    pkt = bytes([0xD9, 0xA0, 0xA0])                 # header + END_ID (cont)
    pkt += e2e_creds["sender_end_id"].encode()
    pkt += bytes([0xA0, 0xA1])                      # GROUP_ID (cont)
    pkt += e2e_creds["sender_group_id"].encode()
    pkt += bytes([0xA0, 0xA2])                      # RECEIVER_ID (cont)
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])                      # AES nonce (cont)
    pkt += nonce.encode()
    pkt += bytes([0x81, 0xF1, 0x01])                # PROXY (1B, cont) — was 4B
    pkt += bytes([0xA0, 0xB5])                      # APP_ID (cont)
    pkt += get_app_id().encode()
    pkt += bytes([0x82, 0xF5, msg_type, mode_byte]) # METHOD (2B, cont)
    pkt += bytes([0x9B, 0xF6])                      # MSGID (cont)
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])                      # CT (LAST)
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
    """Build a heartbeat packet.

    Option order mirrors module_bmt/yd/e.java:231 exactly:
        END_ID, GROUP_ID, RECEIVER_ID, AES, PROXY(1B), METHOD, APP_ID, MSGID, CT
    The previous 4-byte PROXY (0x84 0xF1 00 00 00 01) and RECEIVER_ID-before-AES
    layout caused the relay to reject commands with status 0x52D4.
    """
    if msg_id is None:
        msg_id = generate_msg_id()

    assert len(session_nonce) == 16
    assert len(msg_id) == 27

    payload_json = json.dumps(
        {"__time": int(time.time())}, separators=(",", ":")
    ).encode()
    encrypted = encrypt_payload(payload_json, e2e_creds["chat_secret"], session_nonce)

    pkt = bytes([0xD9, 0xA0, 0xA0])                 # header + END_ID (len 32, cont)
    pkt += e2e_creds["sender_end_id"].encode()
    pkt += bytes([0xA0, 0xA1])                      # GROUP_ID (len 32, cont)
    pkt += e2e_creds["sender_group_id"].encode()
    pkt += bytes([0xA0, 0xA2])                      # RECEIVER_ID (len 32, cont)
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])                      # AES nonce (len 16, cont)
    pkt += session_nonce.encode()
    pkt += bytes([0x81, 0xF1, 0x01])                # PROXY (len 1, cont) — was 4B
    pkt += bytes([0x89, 0xF5])                      # METHOD "heartbeat" (len 9, cont)
    pkt += b"heartbeat"
    pkt += bytes([0xA0, 0xB5])                      # APP_ID (len 32, cont)
    pkt += get_app_id().encode()
    pkt += bytes([0x9B, 0xF6])                      # MSGID (len 27, cont)
    pkt += msg_id.encode()
    pkt += bytes([0x10, 0xB7])                      # CT (len 16, LAST)
    pkt += b"application/json"
    pkt += encrypted
    return pkt


def build_wake_packet(
    e2e_creds: dict,
    session_nonce: str,
    msg_id: str | None = None,
) -> bytes:
    """Build a wake packet — nudges the relay's per-session routing table for
    the device. Fire-and-forget; a status=1 reply is tolerated.
    """
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
    pkt += bytes([0xA0, 0xA2])
    pkt += e2e_creds["recipient_end_id"].encode()
    pkt += bytes([0x90, 0xA3])
    pkt += session_nonce.encode()
    pkt += bytes([0x81, 0xF1, 0x01])
    pkt += bytes([0x84, 0xF5])                      # METHOD "wake" (len 4, cont)
    pkt += b"wake"
    pkt += bytes([0xA0, 0xB5])
    pkt += get_app_id().encode()
    pkt += bytes([0x1B, 0xF6])                      # MSGID (len 27, LAST)
    pkt += msg_id.encode()
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
    fallback_ivs: list[bytes] | None = None,
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
        fallback_ivs: Optional list of additional IVs to try after marker-
            extracted nonces (#47 beta15h).  Used when the relay response
            format changed and no longer includes ``\\x10\\xa3`` markers.

    Returns:
        Decrypted payload bytes, or *None* on failure.
    """
    global _decrypt_rejected_count, _decrypt_rejected_window_start, _decrypt_rejected_sample

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
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "decrypt_response: no nonce markers (90a3/10a3) found "
                "in %d-byte response, hex=%s",
                len(data), data[:128].hex(),
            )
        # Try fallback IVs even without markers (#47 beta15h)
        if fallback_ivs:
            nonces = list(fallback_ivs)
        else:
            return None

    # Include caller-provided fallback IVs if response markers exist
    all_ivs = list(nonces)
    if fallback_ivs:
        for fb in fallback_ivs:
            if fb not in all_ivs:
                all_ivs.append(fb)

    for nonce in all_ivs:
        for offset in range(len(data) - 16, 39, -1):
            remaining = len(data) - offset
            if remaining % 16 != 0:
                continue
            try:
                cipher = AES.new(key_bytes, AES.MODE_CBC, iv=nonce)
                decrypted = unpad(cipher.decrypt(data[offset:]), AES.block_size)
                if payload_validator is not None:
                    valid = payload_validator(decrypted)
                else:
                    valid = len(decrypted) >= 2 and (decrypted[0], decrypted[1]) in accepted_headers
                if valid:
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "decrypt_response: SUCCESS nonce=%s offset=%d "
                            "key_len=%d payload=%dB resp_full_hex=%s",
                            nonce.hex(), offset, len(key),
                            len(decrypted), data.hex(),
                        )
                    return decrypted
                # Decryption succeeded but the payload validator (or accepted
                # headers) rejected it. This is the critical signal for the
                # load-dependent #41 stall: the AES layer is fine (the key works
                # — battery % keeps updating) yet the 0x30 power-flow payload
                # format/values fall outside what the validator accepts under
                # high load. We must NOT lose this signal, but per-offset logging
                # floods the log on every non-power-flow packet (ACKs, status
                # pushes). Coalesce into the rate-limited diagnostic below.
                if _LOGGER.isEnabledFor(logging.DEBUG):
                    with _decrypt_rejected_lock:
                        is_first = _decrypt_rejected_count == 0
                        _decrypt_rejected_count += 1
                        _decrypt_rejected_sample = (
                            nonce.hex(), len(key),
                            len(decrypted), decrypted.hex(),
                        )
                        now = time.monotonic()
                        if is_first or (
                            now - _decrypt_rejected_window_start
                            >= _DECRYPT_REJECTED_WINDOW_S
                        ):
                            n = _decrypt_rejected_count
                            sample = _decrypt_rejected_sample
                            _decrypt_rejected_count = 0
                            _decrypt_rejected_window_start = now
                            _decrypt_rejected_sample = None
                            _flush = True
                        else:
                            n = None
                            sample = None
                            _flush = False
                    if _flush and sample is not None:
                        _LOGGER.debug(
                            "decrypt_response: DECRYPTED-BUT-REJECTED x%d "
                            "(window=%.0fs, sample nonce=%s key_len=%d "
                            "payload_len=%d payload_hex=%s)",
                            n, _DECRYPT_REJECTED_WINDOW_S,
                            sample[0], sample[1], sample[2], sample[3],
                        )
            except (ValueError, KeyError):
                continue

    if _LOGGER.isEnabledFor(logging.DEBUG) and nonces:
        def _positions(marker: bytes) -> list[int]:
            out = []
            idx = 0
            while idx < len(data) - 1:
                pos = data.find(marker, idx)
                if pos < 0:
                    break
                out.append(pos)
                idx = pos + 1
            return out
        _LOGGER.debug(
            "decrypt_response: %d nonce(s) tried but decryption/validation "
            "failed for all offsets (key_len=%d, resp %dB, "
            "90a3_pos=%s 10a3_pos=%s, nonces=%s resp_full_hex=%s)",
            len(all_ivs), len(key), len(data),
            _positions(b"\x90\xa3"), _positions(b"\x10\xa3"),
            [n.hex() for n in all_ivs],
            data.hex(),
        )
    return None


def parse_override_state(payload: bytes) -> dict | None:
    """Parse a type 0x1b response payload into override state.

    Payload format:
        Byte 0:   high battery marker (percentage)
        Byte 1:   low battery marker (percentage)
        Byte 2:   battery-range override-enable flag (0 = AI, 1 = override)
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
        ``low_marker``, and ``battery_range_override`` (bool); or *None*
        on invalid input.
    """
    if payload is None or len(payload) < 105:
        return None
    n_slots = payload[8]
    if n_slots not in (0x60, 0xC0):
        return None
    if len(payload) < 9 + n_slots:
        return None
    # Markers are battery percentages (0-100). A corrupt/garbage byte here
    # (seen historically during 21204 reconnect recovery) would otherwise be
    # exposed as a >100 number entity and break apply_bulk_schedule, which
    # validates 0-100 (#45). Treat an out-of-range marker as a corrupt frame
    # and reject the whole read so the coordinator keeps the last good values.
    if payload[0] > 100 or payload[1] > 100:
        return None
    return {
        "high_marker": payload[0],
        "low_marker": payload[1],
        "battery_range_override": payload[2] != 0,
        "slots": list(payload[9 : 9 + n_slots]),
    }


def parse_battery_data(payload: bytes) -> dict | None:
    """Parse a type 0x06 battery-info response payload.

    The decrypted payload starts with the 2-byte battery signature
    (``0x03 0x00``) which the HP5000 firmware also uses as ``state_flags``.
    Fixed battery fields follow, then length-prefixed strings, and finally
    the trailing cabinet/position/index/capacity fields.

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
    16-17   2     current_energy_wh_raw      LE uint16 in 0.5 Wh ticks
    18-19   2     full_energy_wh_raw         LE uint16 in 0.5 Wh ticks
    20-21   2     cycle_count                LE uint16
    22-23   2     soh                        LE uint16 percent
    24      1     id_info_len                length N₁ (usually 0)
    25…     N₁    id_info                    ASCII (optional)
    +1      1     version_len                length N₂
    …       N₂    version                    ASCII model string
    +1      1     barcode_len                length N₃
    …       N₃    barcode                    ASCII serial number
    +1      1     cabinet_index              cabinet slot index
    +1      1     cabinet_position_index     position within cabinet
    +1      1     index                      battery instance index
    +2      2     capacity                   LE uint16
    ======  ====  =========================  ================================

    Returns:
        Dict with decoded fields, or *None* if payload is invalid.
    """
    if payload is None:
        _LOGGER.debug("parse_battery_data: payload is None")
        return None
    if len(payload) < 26:
        _LOGGER.debug("parse_battery_data: payload too short (%d bytes)", len(payload))
        return None
    if payload[0] != HEADER_BATTERY[0] or payload[1] != HEADER_BATTERY[1]:
        _LOGGER.debug(
            "parse_battery_data: wrong start marker (got 0x%02x 0x%02x, want 0x%02x 0x%02x)",
            payload[0], payload[1], HEADER_BATTERY[0], HEADER_BATTERY[1],
        )
        return None
    _LOGGER.debug("parse_battery_data: raw payload length %d", len(payload))

    def _deci_kelvin_to_c(raw: int) -> float:
        return round(raw / 10.0 - 273.15, 1)

    # Fixed fields.  The first two bytes pass the HEADER_BATTERY check but are
    # also interpreted as state_flags by the HP5000 firmware.
    state_flags = struct.unpack_from("<H", payload, 0)[0]
    bms_temp_raw = struct.unpack_from("<H", payload, 2)[0]
    electrode_a_raw = struct.unpack_from("<H", payload, 4)[0]
    electrode_b_raw = struct.unpack_from("<H", payload, 6)[0]
    voltage_mv = struct.unpack_from("<H", payload, 8)[0]
    current_ma = struct.unpack_from("<i", payload, 10)[0]
    soc = struct.unpack_from("<H", payload, 14)[0]
    # Battery energy fields are encoded in 0.5 Wh ticks.
    current_energy_raw = struct.unpack_from("<H", payload, 16)[0]
    full_energy_raw = struct.unpack_from("<H", payload, 18)[0]
    current_energy_wh = int(round(current_energy_raw * 0.5))
    full_energy_wh = int(round(full_energy_raw * 0.5))
    cycle_count = struct.unpack_from("<H", payload, 20)[0]
    soh = struct.unpack_from("<H", payload, 22)[0]

    # Variable-length strings: id_info, version (model), barcode (serial)
    pos = 24
    id_info_len = payload[pos]; pos += 1
    id_info = payload[pos : pos + id_info_len].decode("ascii", errors="replace") if id_info_len else ""
    pos += id_info_len
    _LOGGER.debug(
        "parse_battery_data: id_info_len=%d pos=%d payload_len=%d",
        id_info_len, pos, len(payload),
    )

    if pos >= len(payload):
        _LOGGER.debug("parse_battery_data: abort after id_info (pos=%d >= len=%d)", pos, len(payload))
        return None
    version_len = payload[pos]; pos += 1
    model = payload[pos : pos + version_len].decode("ascii", errors="replace") if version_len else ""
    pos += version_len
    _LOGGER.debug(
        "parse_battery_data: version_len=%d pos=%d payload_len=%d",
        version_len, pos, len(payload),
    )

    if pos >= len(payload):
        _LOGGER.debug("parse_battery_data: abort after version (pos=%d >= len=%d)", pos, len(payload))
        return None
    barcode_len = payload[pos]; pos += 1
    serial = payload[pos : pos + barcode_len].decode("ascii", errors="replace") if barcode_len else ""
    pos += barcode_len
    _LOGGER.debug(
        "parse_battery_data: barcode_len=%d pos=%d payload_len=%d",
        barcode_len, pos, len(payload),
    )

    # Trailing fixed fields follow the variable-length strings directly.
    # Verified layout on HP5000: after barcode comes cabinet_index (1 byte),
    # cabinet_position (1 byte), battery instance index (1 byte), then
    # capacity (2 bytes LE).
    cabinet_index = payload[pos] if pos < len(payload) else 0; pos += 1
    cabinet_position = payload[pos] if pos < len(payload) else 0; pos += 1
    index = payload[pos] if pos < len(payload) else 0; pos += 1
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


_POWER_FLOW_MAX_RAW_HECTOWATTS = 2000  # 200 kW per channel; filters bogus multi-MW spikes


def _has_reasonable_power_flow_values(payload: bytes) -> bool:
    """Return True when the raw 0x30 fields look plausible.

    The protocol reports powers in units of 100 W. Real systems are expected
    to stay far below 200 kW per channel, so values beyond that are treated as
    corrupt or misclassified payloads rather than published as multi-megawatt
    sensor spikes.
    """
    signed_offsets = (0, 2, 4, 6, 8, 10)
    for offset in signed_offsets:
        if abs(struct.unpack_from("<h", payload, offset)[0]) > _POWER_FLOW_MAX_RAW_HECTOWATTS:
            return False

    unsigned_offsets = (12, 14)
    for offset in unsigned_offsets:
        if struct.unpack_from("<H", payload, offset)[0] > _POWER_FLOW_MAX_RAW_HECTOWATTS:
            return False

    if len(payload) >= 18:
        if payload[16] not in (0, 1) or payload[17] not in (0, 1):
            return False
    if len(payload) >= 20 and payload[19] not in (0, 1):
        return False
    if len(payload) >= 22:
        if abs(struct.unpack_from("<h", payload, 20)[0]) > _POWER_FLOW_MAX_RAW_HECTOWATTS:
            return False

    return True


def _is_power_flow_payload(payload: bytes) -> bool:
    """Check if decrypted payload looks like a power flow response.

    Power flow responses are 16–24 bytes of signed-short watt values.
    Heuristic: correct length range, plausible per-channel raw values,
    and boolean flags using valid 0/1 encodings.
    """
    if len(payload) < 16 or len(payload) > 24:
        return False
    return _has_reasonable_power_flow_values(payload)


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
    if not _is_power_flow_payload(payload):
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
    wake = build_wake_packet(e2e_creds, session_nonce)

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
        _send(wake, "Wake")
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
    wake = build_wake_packet(e2e_creds, session_nonce)
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
        _send(wake, "Wake")
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
    probe_timeout: float = 1.5,
    slots: list[int] | None = None,
    known_serial_slots: dict[str, int] | None = None,
    log: Callable[..., None] | None = None,
) -> list[dict]:
    """Read battery cell info via E2E request (type 0x06).

    Performs the full session flow (alive → heartbeat) then probes cabinet slot
    indices.

    The handshake uses ``timeout`` (the relay round trip can be slow), while
    each per-slot probe uses the shorter ``probe_timeout``: an installed module
    answers within tens of milliseconds, so the full handshake timeout is
    wasteful for empty/non-existent cabinet slots that simply never reply.

    Battery slots are addressed by physical cabinet position: a module
    installed in the third slot answers at probe index 2 while indices 0–1 stay
    silent. Discovery therefore cannot assume modules start at index 0, so the
    full scan walks every tier.

    Args:
        timeout: Socket timeout (seconds) for the handshake packets.
        probe_timeout: Socket timeout (seconds) for each 0x06 slot probe.
        slots: When given, probe exactly these cabinet slot indices (a fast
            re-scan of previously discovered slots). When ``None`` (default),
            run a full cabinet discovery across all known tiers.
        known_serial_slots: Optional serial -> slot-index map from previous
            scans. A probe reply whose serial is already known to belong to a
            *different* slot is a stray late datagram that leaked into this
            slot's receive window (e.g. the rightful slot timed out this round),
            so it is rejected rather than misassigned. Independent of the
            device's own index fields, so it is safe across cabinets (#44).
        log: Optional log callback.

    Returns:
        List of battery-info dicts (one per module), possibly empty.
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
    wake = build_wake_packet(e2e_creds, session_nonce)

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

    def _probe_slot(idx: int) -> str | dict:
        """Probe a single cabinet slot.

        Returns the parsed battery dict on success, ``"timeout"`` if the slot
        did not reply at all, or ``"empty"`` for a present-but-non-battery
        reply (short ACK or unparseable payload).
        """
        # Drain any stale UDP datagrams (late replies from previous probes)
        # before sending this probe.  Without draining, a slow-responding
        # module at slot N-1 can deliver its reply during slot N's receive
        # window, causing slot N's _probe_slot to return slot N-1's data and
        # assign it scan_index=N — producing the cascading +1 shift reported
        # in issue #44.
        _cur_timeout = sock.gettimeout()
        sock.settimeout(0)
        while True:
            try:
                sock.recvfrom(4096)
            except OSError:
                break
        sock.settimeout(_cur_timeout)

        req = build_subscription_packet(
            e2e_creds, 0x06, session_nonce,
            payload=bytes([idx]),
            request_mode=True,
        )
        resp_raw = _send(req, f"Battery(idx={idx})")

        if not resp_raw:
            return "timeout"

        # Responses shorter than the AES framing overhead (~50 B) cannot
        # possibly contain a valid encrypted payload, so skip them as clearly
        # empty or corrupt.  Everything ≥ 50 B is handed to decrypt + parse so
        # the protocol header check (HEADER_BATTERY) decides, not a magic size
        # threshold.  Previous threshold of 250 was too aggressive for HP5000
        # firmware whose valid responses are ~243 B (#23).
        if len(resp_raw) < 50:
            if log:
                log(f"Battery(idx={idx}): too-short reply {len(resp_raw)}B — skipped")
            return "empty"

        info = _try_parse_battery(resp_raw)
        if info is None:
            # First packet may be a subscription ACK — try one more.
            try:
                resp2, _ = sock.recvfrom(4096)
                if log:
                    log(f"Battery(idx={idx}) follow-up: {len(resp2)}B")
                info = _try_parse_battery(resp2)
            except socket.timeout:
                if log:
                    log(f"Battery(idx={idx}) follow-up: timeout")

        if info and info["serial"] not in seen_serials:
            seen_serials.add(info["serial"])
            info["scan_index"] = info.get("index", idx)
            return info
        return "empty"

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        # Per-slot probes use the shorter timeout: an installed module answers
        # in tens of milliseconds, so empty/non-existent slots must not cost
        # the full handshake timeout each.
        sock.settimeout(probe_timeout)

        if slots is not None:
            # Fast re-scan: probe exactly the requested (previously discovered)
            # slots.  No tier-abort heuristics — the list is short and already
            # known-good, and a transient miss on one slot must not stop the
            # others from being read.
            for idx in slots:
                result = _probe_slot(idx)
                if isinstance(result, dict):
                    batteries.append(result)
            return batteries

        # Full discovery. HP5000 systems can report empty slots at low indices
        # while valid modules live later in the range, so short empty replies
        # must not stop discovery. True timeouts are the expensive failure
        # signal: two in a row end the *current* tier (not the whole scan) so a
        # second cabinet addressed at a higher tier is still discovered.
        TIERS = [(0, 3), (3, 5), (8, 5)]

        for tier_start, tier_size in TIERS:
            consecutive_timeouts = 0
            for idx in range(tier_start, tier_start + tier_size):
                result = _probe_slot(idx)

                if result == "timeout":
                    # Device not responding for this slot; abort this tier on
                    # 2 in a row but still try the remaining tiers.
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= 2:
                        break
                    continue

                consecutive_timeouts = 0
                if isinstance(result, dict):
                    batteries.append(result)

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


# ---------------------------------------------------------------------------
# Grid frequency regulation state (FCR / mFRR balancing)
# ---------------------------------------------------------------------------

# E2E type for GET_REGULATE_FREQUENCY_STATE
_REGULATE_FREQ_TYPE = 0x45

# RegulateFrequencyStateType enum values (Mcu.java)
_REGULATE_FREQ_STATES: dict[int, str] = {
    0: "Idle",
    1: "OnHold",
    2: "FcrN",
    3: "FcrDUp",
    4: "FcrDDown",
    5: "FcrDUpDown",
    6: "MFRRUp",
    7: "MFRRDown",
}


def _is_regulate_frequency_payload(payload: bytes) -> bool:
    """Check if decrypted payload is a regulate-frequency-state response.

    Response is 2 or 4 bytes: state(LE u16) [+ has_error(LE u16)].
    State value must be in 0..7.
    """
    if payload is None or len(payload) not in (2, 4):
        return False
    state = struct.unpack_from("<H", payload, 0)[0]
    return state in _REGULATE_FREQ_STATES


def parse_regulate_frequency_state(payload: bytes) -> dict | None:
    """Parse a GET_REGULATE_FREQUENCY_STATE response payload.

    Payload layout:
        bytes [0..1]  state      LE uint16 — RegulateFrequencyStateType value
        bytes [2..3]  has_error  LE uint16 — present only when len > 2

    Returns:
        Dict with:
            ``state`` (int): raw enum value 0–7
            ``state_name`` (str): human-readable name (e.g. "FcrN")
            ``has_error`` (bool | None): True when device reports error;
                None when not present in payload
            ``display`` (str): "idle" | "pre_balancing" | "fcr_n" |
                "fcr_d_up" | "fcr_d_down" | "fcr_d_up_down" |
                "mfrr_up" | "mfrr_down" | "balancing_failed"
        or *None* on invalid input.
    """
    if payload is None or len(payload) < 2:
        return None

    state = struct.unpack_from("<H", payload, 0)[0]
    if state not in _REGULATE_FREQ_STATES:
        return None

    has_error: bool | None = None
    if len(payload) >= 4:
        raw_error = struct.unpack_from("<H", payload, 2)[0]
        has_error = raw_error != 1

    if has_error is True:
        display = "balancing_failed"
    elif state == 0:
        display = "idle"
    elif state == 1:
        display = "pre_balancing"
    elif state == 2:
        display = "fcr_n"
    elif state == 3:
        display = "fcr_d_up"
    elif state == 4:
        display = "fcr_d_down"
    elif state == 5:
        display = "fcr_d_up_down"
    elif state == 6:
        display = "mfrr_up"
    else:  # state == 7
        display = "mfrr_down"

    return {
        "state": state,
        "state_name": _REGULATE_FREQ_STATES[state],
        "has_error": has_error,
        "display": display,
    }


def read_regulate_frequency_state(
    e2e_creds: dict,
    *,
    timeout: float = 5.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read real-time FCR/mFRR grid frequency regulation state (E2E type 0x45).

    Sends GET_REGULATE_FREQUENCY_STATE to the device and returns a dict
    with ``state``, ``state_name``, ``has_error``, and ``display``.

    ``display`` is one of:
      - ``"idle"``             — no balancing activity
      - ``"pre_balancing"``    — device is on hold, balancing imminent
      - ``"fcr_n"``            — actively providing FCR-N
      - ``"fcr_d_up"``         — actively providing FCR-D Up
      - ``"fcr_d_down"``       — actively providing FCR-D Down
      - ``"fcr_d_up_down"``    — actively providing FCR-D Up+Down
      - ``"mfrr_up"``          — providing mFRR Up
      - ``"mfrr_down"``        — providing mFRR Down
      - ``"balancing_failed"`` — device reported an error

    Returns *None* if the device did not respond or payload was unreadable.
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
    req_pkt = build_subscription_packet(
        e2e_creds, _REGULATE_FREQ_TYPE, session_nonce,
        request_mode=True,
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

        resp = _send(req_pkt, "RegulateFrequencyState(0x45)")
        if not resp:
            return None

        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_regulate_frequency_payload,
        )
        result = parse_regulate_frequency_state(decrypted)
        if result is not None:
            return result

        # First response may be an echo/ACK; try a few more
        for _ in range(5):
            try:
                resp, _ = sock.recvfrom(4096)
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_regulate_frequency_payload,
                )
                result = parse_regulate_frequency_state(decrypted)
                if result is not None:
                    return result
            except socket.timeout:
                break

        return None
    finally:
        sock.close()


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
    wake = build_wake_packet(e2e_creds, session_nonce)
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
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(power_pkt, "PowerFlow(0x30)")
        if resp and log:
            log(f"  raw ({len(resp)}B): {resp[:128].hex()}")
            has_90a3 = b"\x90\xa3" in resp
            has_10a3 = b"\x10\xa3" in resp
            log(f"  nonce markers: 90a3={has_90a3} 10a3={has_10a3}")
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "read_power_flow: full resp (%dB) hex=%s",
                    len(resp), resp.hex(),
                )
        if not resp:
            return None

        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_power_flow_payload,
            fallback_ivs=[session_nonce.encode()],
        )
        if decrypted is None:
            # Fallback: home-level chat_secret (#47 beta15h)
            home_secret = e2e_creds.get("home_chat_secret", "")
            if home_secret and home_secret != e2e_creds.get("chat_secret", ""):
                if log:
                    log("  decrypt with chat_secret failed, trying home_chat_secret")
                decrypted = decrypt_response(
                    resp, home_secret,
                    payload_validator=_is_power_flow_payload,
                    fallback_ivs=[session_nonce.encode()],
                )
        if decrypted is not None and log:
            _log_power_flow_raw(decrypted, log)
        elif decrypted is None and log:
            log(f"  decrypt_response returned None (resp {len(resp)}B)")
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "read_power_flow: decrypt None resp (%dB) hex=%s",
                    len(resp), resp.hex(),
                )
        result = parse_power_flow(decrypted)
        if result is not None:
            return result
        if decrypted is not None and log:
            log(f"  parse_power_flow FAILED on decrypted payload ({len(decrypted)}B)")

        # First response may be an echo/ACK; try a few more
        drain_idx = 0
        for _ in range(5):
            try:
                resp, _ = sock.recvfrom(4096)
                drain_idx += 1
                if log:
                    log(f"  legacy drain #{drain_idx} ({len(resp)}B): {resp[:128].hex()}")
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_power_flow_payload,
                    fallback_ivs=[session_nonce.encode()],
                )
                if decrypted is None:
                    home_secret = e2e_creds.get("home_chat_secret", "")
                    if home_secret and home_secret != e2e_creds.get("chat_secret", ""):
                        decrypted = decrypt_response(
                            resp, home_secret,
                            payload_validator=_is_power_flow_payload,
                            fallback_ivs=[session_nonce.encode()],
                        )
                if decrypted is not None and log:
                    _log_power_flow_raw(decrypted, log)
                elif decrypted is None and log:
                    log(f"  legacy drain #{drain_idx}: decrypt=None")
                    if _LOGGER.isEnabledFor(logging.DEBUG):
                        _LOGGER.debug(
                            "read_power_flow: legacy drain #%d resp (%dB) hex=%s",
                            drain_idx, len(resp), resp.hex(),
                        )
                result = parse_power_flow(decrypted)
                if result is not None:
                    return result
                if decrypted is not None and log:
                    log(f"  legacy drain #{drain_idx}: parse FAILED")
            except socket.timeout:
                if log:
                    log(f"  legacy drain #{drain_idx}: timeout (no more packets)")
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
    battery_range_override: bool = False,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Send override slot values via E2E protocol.

    Performs the full session flow and sends the override packet.
    Returns *True* if the server acknowledged the override. Set
    ``battery_range_override=True`` to also activate the app's
    "Battery Range = override" mode (byte 2 of payload).
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
    wake = build_wake_packet(e2e_creds, session_nonce)
    override_pkt = build_override_packet(
        e2e_creds, slot_values, nonce=session_nonce,
        high_marker=high_marker, low_marker=low_marker,
        battery_range_override=battery_range_override,
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
        _send(wake, "Wake")
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
    wake = build_wake_packet(e2e_creds, session_nonce)
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
        _send(wake, "Wake")
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
    wake = build_wake_packet(e2e_creds, session_nonce)
    cancel_pkt = build_subscription_packet(
        e2e_creds, 0x01, session_nonce,
        payload=payload,
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _is_emergency() -> bool:
        return "emergency" in label.lower() or "charge" in label.lower()

    def _send(pkt: bytes, label: str) -> bytes | None:
        t0 = time.monotonic()
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            elapsed = (time.monotonic() - t0) * 1000
            if _is_emergency():
                _LOGGER.debug(
                    "[EmergencyCharge] %s: sent %dB \u2192 got %dB (rtt=%.1fms) resp_hex=%s",
                    label, len(pkt), len(resp), elapsed, resp[:64].hex(),
                )
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 got {len(resp)}B")
            return resp
        except socket.timeout:
            elapsed = (time.monotonic() - t0) * 1000
            if _is_emergency():
                _LOGGER.debug(
                    "[EmergencyCharge] %s: sent %dB \u2192 timeout (%.1fms)",
                    label, len(pkt), elapsed,
                )
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 no response")
            return None

    try:
        if _send(home_alive, "Alive(home)") is None: return False
        if _send(dev_alive, "Alive(device)") is None: return False
        if _send(wake, "Wake") is None: return False
        if _send(heartbeat, "Heartbeat") is None: return False
        time.sleep(0.2)

        resp = _send(cancel_pkt, label)
        if _is_emergency():
            _LOGGER.debug(
                "[EmergencyCharge] cancel result=%s resp_len=%s",
                resp is not None, len(resp) if resp else None,
            )
        if resp is None:
            return False
        if b"CONN_NOT_ESTABLISHED" in resp:
            if _is_emergency():
                _LOGGER.debug("[EmergencyCharge] cancel rejected: CONN_NOT_ESTABLISHED")
            return False
        return True
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# Emergency charge (pull energy FROM grid) and Manual selling (push TO grid)
# ---------------------------------------------------------------------------
#
# NOTE on the legacy `send_sell` / `cancel_sell` above: those send opcode 0x01A0,
# which the firmware calls SET_EMERGENCY_CHARGE — i.e. charge the battery from the
# grid, NOT sell to the grid. The functions below use the correct semantics.
# Opcodes:
#   0x01A0  set_emergency_charge   write [on u8, start u32le, end u32le]  9B
#   0x80A0  set_manual_selling     write [on u8, target_kwh u32le, expand u8] 6B
#   0x81A0  get_manual_selling     read  [firstUse u8, enabled u8,
#                                         target_0.1kWh u32le, sold_0.1kWh u32le] 10B


def set_emergency_charge(
    e2e_creds: dict,
    on: bool,
    *,
    start_unix: int | None = None,
    end_unix: int | None = None,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Enable/disable emergency charging over a time window.

    When enabling, the default window is now → top-of-current-hour + 48 h,
    matching the firmware default.
    """
    if on:
        if start_unix is None:
            start_unix = int(time.time())
        if end_unix is None:
            now = time.time()
            end_unix = int(now - (int(now) % 3600)) + 172800
        payload = struct.pack("<BII", 1, start_unix, end_unix)
        _LOGGER.debug(
            "[EmergencyCharge] enabling: start=%d end=%d payload_hex=%s",
            start_unix, end_unix, payload.hex(),
        )
    else:
        payload = bytes(9)  # 9 zeros
        _LOGGER.debug(
            "[EmergencyCharge] disabling: payload_hex=%s", payload.hex(),
        )

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
    wake = build_wake_packet(e2e_creds, session_nonce)
    cmd_pkt = build_subscription_packet(
        e2e_creds, 0x01, session_nonce, payload=payload,
    )

    host, port = _resolve_host(e2e_creds["host"])
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    addr = (host, port)

    def _send(pkt: bytes, label: str) -> bytes | None:
        t0 = time.monotonic()
        sock.sendto(pkt, addr)
        try:
            resp, _ = sock.recvfrom(4096)
            elapsed = (time.monotonic() - t0) * 1000
            _LOGGER.debug(
                "[EmergencyCharge] %s: sent %dB \u2192 got %dB (rtt=%.1fms) resp_hex=%s",
                label, len(pkt), len(resp), elapsed, resp[:64].hex(),
            )
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 got {len(resp)}B")
            return resp
        except socket.timeout:
            elapsed = (time.monotonic() - t0) * 1000
            _LOGGER.debug(
                "[EmergencyCharge] %s: sent %dB \u2192 timeout (%.1fms)",
                label, len(pkt), elapsed,
            )
            if log:
                log(f"{label}: sent {len(pkt)}B \u2192 no response")
            return None

    try:
        if _send(home_alive, "Alive(home)") is None: return False
        if _send(dev_alive, "Alive(device)") is None: return False
        if _send(wake, "Wake") is None: return False
        if _send(heartbeat, "Heartbeat") is None: return False
        time.sleep(0.2)
        resp = _send(cmd_pkt, "EmergencyCharge")
        if resp is None:
            _LOGGER.debug("[EmergencyCharge] result=False (no response)")
            return False
        if b"CONN_NOT_ESTABLISHED" in resp:
            _LOGGER.debug("[EmergencyCharge] command rejected: CONN_NOT_ESTABLISHED")
            return False
        _LOGGER.debug(
            "[EmergencyCharge] result=True resp_len=%s", len(resp),
        )
        return True
    finally:
        sock.close()


def set_manual_selling(
    e2e_creds: dict,
    on: bool,
    target_energy_kwh: int | float = 0,
    *,
    expand: bool = False,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Start/stop grid-export (manual selling) with a cumulative kWh target.

    The inverter exports until ``target_energy_kwh`` total have been sold,
    then stops automatically. Use :func:`get_manual_selling` to poll
    progress.

    Wire:
        [on u8, target_kwh u32 LE, isExpandSelling u8] — 6 bytes total.
    """
    if on and target_energy_kwh <= 0:
        raise ValueError("target_energy_kwh must be > 0 when enabling")
    target = round(target_energy_kwh) if on else 0
    payload = struct.pack("<BIB", 1 if on else 0, target & 0xFFFFFFFF, 1 if expand else 0)

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
    wake = build_wake_packet(e2e_creds, session_nonce)
    cmd_pkt = build_subscription_packet(
        e2e_creds, 0x80, session_nonce, payload=payload,
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
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)
        resp = _send(cmd_pkt, "ManualSelling")
        return resp is not None
    finally:
        sock.close()


def parse_manual_selling_response(payload: bytes | None) -> dict | None:
    """Decode the 10-byte GET_MANUAL_SELLING response payload.

    Returns dict with ``enabled``, ``first_use``, ``target_energy_kwh``,
    ``sold_so_far_kwh``, ``remaining_kwh``.
    """
    if payload is None or len(payload) < 10:
        return None
    first_use = payload[0] == 1
    enabled = payload[1] == 1
    target_deci = int.from_bytes(payload[2:6], "little", signed=False)
    sold_deci = int.from_bytes(payload[6:10], "little", signed=False)
    return {
        "first_use": first_use,
        "enabled": enabled,
        "target_energy_kwh": round(target_deci / 10.0, 2),
        "sold_so_far_kwh": round(sold_deci / 10.0, 2),
        "remaining_kwh": round(max(0, target_deci - sold_deci) / 10.0, 2),
    }


def get_manual_selling(
    e2e_creds: dict,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read current manual-selling state + energy counters (opcode 0x81A0).

    See :func:`parse_manual_selling_response` for field semantics. Returns
    *None* if the session handshake or decryption fails.
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
    wake = build_wake_packet(e2e_creds, session_nonce)
    cmd_pkt = build_subscription_packet(
        e2e_creds, 0x81, session_nonce, payload=b"",
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
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)
        resp = _send(cmd_pkt, "GetManualSelling")
        if not resp:
            return None
        # Match the pattern used by other readers: decrypt with chat_secret,
        # accept any payload >= 10 bytes.
        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=lambda b: len(b) >= 10,
        )
        return parse_manual_selling_response(decrypted)
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
# EV charging mode
# ---------------------------------------------------------------------------
#
# The app's EV panel exposes two top-level groups:
#
#   Smart Charge
#     ├── Lowest Price   (mode 1, "lowerUtilityRate")
#     ├── Solar Only     (mode 2, "solarOnly")
#     └── Scheduled      (mode 3, "scheduled")
#   Instant Charge
#     ├── Until Fully Charged  (mode 4, "instantChargeFull")
#     └── Fixed X kWh          (mode 5, "instantChargeFixed")
#
# Setting a Smart mode and an Instant mode uses DIFFERENT wire commands.
# Both commands encode the mode enum (1–5), but the payload shapes differ
# because Smart modes carry an optional 24h×2 weekday/weekend schedule
# while Instant modes carry only a "fixed" kWh amount.
#
# Wire bytes confirmed by packet capture from the Android app:
#
#   0x22 = SET_EV_CHARGING_MODE       (Smart modes 1–3, 9-byte payload)
#   0x31 = SET_EVCHARGINGMODE_INSTANT (Instant modes 4–5, 4-byte payload)
#   0x29 = SET_EVCHARGINGMODE_INSTANTCHARGE (simple Smart↔Instant toggle, 1 byte)
#
# The corresponding Android MSCT OPTION_METHOD shorts (little-endian on
# the wire, matching the observed `82 f5 XX A0` header sequence):
#
#   (short) -24542 = 0xA022 → wire bytes "22 A0" → our msg_type byte 0x22
#   (short) -24527 = 0xA031 → wire bytes "31 A0" → our msg_type byte 0x31
#   (short) -24535 = 0xA029 → wire bytes "29 A0" → our msg_type byte 0x29
#
# The READ command (``get_current_evchargingmode``, case 62 in j.java,
# short -24544 = 0xA0E0) is rejected by the cloud relay with "cmd not
# allowed" — the phone app appears to get its current state from the
# aggregated panel-load burst or a subscription channel we haven't
# unwrapped yet. For now, treat all EV setters as fire-and-forget and
# track state optimistically in the caller.

EV_MODE_LOWEST_PRICE = 1       # Smart: charge during cheapest grid hours
EV_MODE_SOLAR_ONLY = 2         # Smart: charge only from surplus PV (not
                               # surfaced in current app UI; accepted by
                               # the wire protocol but treat as unsupported
                               # unless verified on your specific hardware)
EV_MODE_SCHEDULED = 3          # Smart: charge on a weekday/weekend schedule
EV_MODE_INSTANT_FULL = 4       # Instant: charge flat-out until the car is full
EV_MODE_INSTANT_FIXED = 5      # Instant: charge exactly ``fixed`` kWh then stop

_EV_TYPE_SET_SMART = 0x22      # SET_EV_CHARGING_MODE (9-byte payload)
_EV_TYPE_SET_INSTANT = 0x31    # SET_EVCHARGINGMODE_INSTANT (4-byte payload)
_EV_TYPE_TOGGLE_INSTANTCHARGE = 0x29  # simple Smart↔Instant toggle (1 byte)


def _pack_ev_schedule(hours: list[int] | None) -> bytes:
    """Pack a list of 24 hour flags into 3 bytes (LSB = hour 0).

    Matches the Android app's ``Integer.parseInt(a(list, 0, 7), 2)`` loop:
    each 8-hour chunk becomes one byte, earliest hour in the low bit.
    Returns ``b"\\x00\\x00\\x00"`` if *hours* is *None* or < 24 entries.
    """
    if hours is None or len(hours) < 24:
        return b"\x00\x00\x00"
    out = bytearray(3)
    for i in range(24):
        if hours[i]:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def set_ev_charging_mode_smart(
    e2e_creds: dict,
    mode: int,
    *,
    weekdays: list[int] | None = None,
    weekend: list[int] | None = None,
    sync: bool = False,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Set a Smart-Charge sub-mode (Lowest Price / Solar Only / Scheduled).

    Wire command 0x22 (SET_EV_CHARGING_MODE). Payload is 9 bytes:

        byte 0    = mode - 1                    (0 = Lowest Price, 1 = Solar
                                                  Only, 2 = Scheduled)
        byte 1    = 1 if no schedule, 0 if the caller supplied hour bitmaps
        byte 2..4 = weekday 24-hour bitmap (3 bytes, LSB = hour 0)
        byte 5..7 = weekend 24-hour bitmap (3 bytes)
        byte 8    = sync flag (1 = sync to other devices in the home)

    Args:
        mode: One of :data:`EV_MODE_LOWEST_PRICE`, :data:`EV_MODE_SOLAR_ONLY`,
            :data:`EV_MODE_SCHEDULED`.
        weekdays: Optional 24-element list of 0/1 flags (1 = charge allowed
            in that hour). Only meaningful for ``EV_MODE_SCHEDULED``.
        weekend: Same as *weekdays* but for Sat/Sun.
        sync: Mirror the app's "Sync" toggle.

    Returns *True* if the relay acknowledged the write.
    """
    if mode not in (EV_MODE_LOWEST_PRICE, EV_MODE_SOLAR_ONLY, EV_MODE_SCHEDULED):
        raise ValueError(
            f"mode {mode} is not a Smart sub-mode; use set_ev_charging_mode_instant"
        )

    has_schedule = (
        weekdays is not None and len(weekdays) >= 24
        and weekend is not None and len(weekend) >= 24
    )
    payload = bytes([mode - 1, 0 if has_schedule else 1])
    payload += _pack_ev_schedule(weekdays if has_schedule else None)
    payload += _pack_ev_schedule(weekend if has_schedule else None)
    payload += bytes([1 if sync else 0])

    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _EV_TYPE_SET_SMART, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Set EV smart mode", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def set_ev_charging_mode_instant(
    e2e_creds: dict,
    mode: int,
    *,
    fixed_kwh: int = 0,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Set an Instant-Charge sub-mode (Until Full / Fixed kWh).

    Wire command 0x31 (SET_EVCHARGINGMODE_INSTANT). Payload is 4 bytes:

        byte 0    = mode - 1         (3 = Until Full, 4 = Fixed)
        byte 1    = 0 if mode == 5, else 1
                    (derived in the Android app as ``i2 == 5 ? 0 : 1``;
                     probably a "consume fixed value" flag)
        byte 2..3 = fixed kWh amount as little-endian u16
                    (0 when mode == 4; value from slider when mode == 5)

    Args:
        mode: :data:`EV_MODE_INSTANT_FULL` or :data:`EV_MODE_INSTANT_FIXED`.
        fixed_kwh: Charge amount in kWh. Required when *mode* is
            ``EV_MODE_INSTANT_FIXED``; ignored for ``EV_MODE_INSTANT_FULL``.

    Returns *True* if the relay acknowledged the write.
    """
    if mode not in (EV_MODE_INSTANT_FULL, EV_MODE_INSTANT_FIXED):
        raise ValueError(
            f"mode {mode} is not an Instant sub-mode; use set_ev_charging_mode_smart"
        )
    if mode == EV_MODE_INSTANT_FULL:
        fixed_kwh = 0  # field ignored by the device in Full mode

    payload = bytes([
        mode - 1,
        0 if mode == EV_MODE_INSTANT_FIXED else 1,
        fixed_kwh & 0xFF,
        (fixed_kwh >> 8) & 0xFF,
    ])

    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _EV_TYPE_SET_INSTANT, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Set EV instant mode", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def toggle_ev_instantcharge(
    e2e_creds: dict,
    instant_on: bool,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Flip the simple Smart↔Instant switch on the EV panel.

    Wire command 0x29 (SET_EVCHARGINGMODE_INSTANTCHARGE). Payload is 1 byte:
    ``0x01`` enables Instant, ``0x00`` returns to the previously selected
    Smart sub-mode. This is the command the phone app fires when the user
    taps the single "Instant Charge" toggle switch (as opposed to drilling
    into the mode selector and picking a specific variant).

    Returns *True* if the relay acknowledged the write.
    """
    payload = bytes([1 if instant_on else 0])
    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _EV_TYPE_TOGGLE_INSTANTCHARGE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("Toggle EV instant charge", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


# Wire bytes observed in the panel-load burst when the Android app opens
# the EV screen. The app fires all of these in parallel and builds the
# screen from whichever response carries each field it needs.
#
# The dedicated ``get_current_evchargingmode`` (wire 0xE0 = short -24544)
# is rejected by the cloud relay with "cmd not allowed". Instead the EV
# mode is returned as a *subset* of the panel-load burst — specifically
# wire byte ``0x20`` returns the 6-byte payload documented in
# ``module_bmt/zd/m0.java``: ``[mode-1, fixed_lo, fixed_hi,
# fixedFull_lo, fixedFull_hi, pricePercent]``.
#
# Capture source: logs/ev_mode2.pcap (see history 2026-04-11).
_EV_TYPE_GET_STATE = 0x20  # returns the 6-byte EV charging mode payload

_EV_PANEL_LOAD_BYTES: tuple[int, ...] = (
    # Data-bearing commands (observed 211B responses):
    0x02, 0x04, 0x05, 0x30, 0x33,
    # Empty-ACK and small-data commands (observed 195B responses):
    0x07, 0x11, 0x16, 0x17, 0x18, 0x20, 0x25, 0x27, 0x43, 0x45,
    0x50, 0x5D, 0x81,
)


def parse_ev_charging_info(payload: bytes | None) -> dict | None:
    """Parse a 6-byte EV charging mode response payload.

    Decoded from ``module_bmt/zd/m0.java``'s ``onAckEvent`` handler for
    ``GET_CURRENT_EV_CHARGING_MODE``:

        byte 0    = mode - 1                           (enum 1–5)
        byte 1..2 = fixed       (little-endian u16)    (kWh slider value)
        byte 3..4 = fixedFull   (little-endian u16)    (kWh slider max)
        byte 5    = pricePercent                       (semantics unknown)

    Args:
        payload: Decrypted 6-byte payload, or *None*.

    Returns:
        Parsed dict with keys ``mode``, ``fixed_kwh``, ``fixed_full_kwh``,
        ``price_percent``, or *None* if *payload* is None / too short.
    """
    if payload is None or len(payload) < 6:
        return None
    return {
        "mode": payload[0] + 1,                          # 1–5
        "fixed_kwh": payload[1] | (payload[2] << 8),     # LE u16
        "fixed_full_kwh": payload[3] | (payload[4] << 8),
        "price_percent": payload[5],
    }


def read_ev_charging_mode(
    e2e_creds: dict,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read the current EV charging mode and fixed-charge slider state.

    Sends wire byte :data:`_EV_TYPE_GET_STATE` (0x20), which returns a
    6-byte payload the Android app uses to render the EV page. The
    dedicated ``get_current_evchargingmode`` command (wire 0xE0) is
    blocked by the cloud relay; 0x20 is the only byte in the panel-load
    burst that returns a matching 6-byte struct, so we use it as the
    de-facto read command.

    Returns:
        Dict from :func:`parse_ev_charging_info`, or *None* on failure.
    """
    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _EV_TYPE_GET_STATE, session_nonce, payload=b"",
    )
    results = _run_session(
        e2e_creds, [("Read EV charging mode", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    if resp is None:
        return None
    decrypted = decrypt_response(
        resp, e2e_creds["chat_secret"],
        # Permissive validator: any 6-byte payload is acceptable here
        # because the response has no distinctive 2-byte header.
        payload_validator=lambda p: len(p) == 6,
    )
    return parse_ev_charging_info(decrypted)


def load_ev_page_data(
    e2e_creds: dict,
    *,
    bytes_to_send: tuple[int, ...] | None = None,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> dict[int, bytes]:
    """Replay the EV panel load burst and collect decrypted responses.

    The Android app does not use a dedicated ``get_current_evchargingmode``
    command over the cloud relay — that call is rejected with
    ``"cmd not allowed"``. Instead the app opens the EV screen by firing
    ~16 different wire bytes in parallel and composes the UI from the
    aggregate of responses. This helper replays that burst and returns
    every response we could decrypt.

    Use it as a discovery tool: inspect the returned dict to figure out
    which wire byte returns a 6-byte payload shaped like the known EV
    mode response (``[mode-1, fixed_lo, fixed_hi, fixedFull_lo,
    fixedFull_hi, pricePercent]``, documented in ``module_bmt/zd/m0.java``).

    Args:
        e2e_creds: Credentials from :func:`EmaldoClient.e2e_login`.
        bytes_to_send: Optional override of the byte set. Defaults to
            :data:`_EV_PANEL_LOAD_BYTES` (observed from packet capture).
        timeout: Per-packet receive timeout.
        log: Optional logging callback.

    Returns:
        Dict mapping each wire byte to its decrypted response payload
        (or ``None`` if the send failed / the response couldn't be
        decrypted).
    """
    probe_bytes = bytes_to_send or _EV_PANEL_LOAD_BYTES

    # Build one packet per byte, each with its own nonce so the device
    # side doesn't treat them as duplicates of one another.
    packets: list[tuple[str, bytes]] = []
    nonces: list[str] = []
    for b in probe_bytes:
        nonce = generate_nonce()
        nonces.append(nonce)
        # Empty payload: safe for reads, ignored by writes that expect
        # structured input (they'll just error rather than mutate state).
        pkt = build_subscription_packet(
            e2e_creds, b, nonce, payload=b"",
        )
        packets.append((f"PanelLoad(0x{b:02x})", pkt))

    results = _run_session(e2e_creds, packets, timeout=timeout, log=log)

    out: dict[int, bytes] = {}
    for (b, _), (_, resp) in zip(zip(probe_bytes, nonces), results):
        if resp is None:
            continue
        # Try our session's chat_secret first; responses without a
        # recognized payload shape will come back as ``None`` from
        # ``decrypt_response``, but we still keep the raw response for
        # callers that want to inspect plaintext error strings.
        decrypted = decrypt_response(resp, e2e_creds["chat_secret"])
        if decrypted is not None:
            out[b] = decrypted
        else:
            out[b] = resp  # raw — caller can sniff for "cmd not allowed"
    return out


# ---------------------------------------------------------------------------
# Third-party PV
# ---------------------------------------------------------------------------
#
# The official app exposes a boolean "Third-Party PV" switch under Energy
# Settings. The state is returned in the 0x30 power-flow response (byte 19)
# and can be changed with opcode 0x41 (SET_THIRDPARTYPV_ON).
#
# Wire: 0x41 A0 (subscribe mode), payload = 1 byte: 0x01=on, 0x00=off.
# Fire-and-forget; no response payload to parse.

_THIRDPARTY_PV_SET_TYPE = 0x41  # set_thirdpartypv_on


def set_thirdparty_pv(
    e2e_creds: dict,
    enabled: bool,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Enable or disable third-party PV input (type 0x41).

    Sends ``SET_THIRDPARTYPV_ON`` with a 1-byte boolean payload.
    This is a fire-and-forget command; the updated state is visible
    in the next :func:`read_power_flow` response (byte 19).

    Returns *True* if the server acknowledged the command.
    """
    session_nonce = generate_nonce()
    payload = bytes([0x01 if enabled else 0x00])
    pkt = build_subscription_packet(
        e2e_creds, _THIRDPARTY_PV_SET_TYPE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("SetThirdpartyPV(0x41)", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


# ---------------------------------------------------------------------------
# Selling protection (Sell Limit: 0x5E/0x5F)
# and Sell Back to Grid / Virtual Power Plant (0x05/0x06)
# ---------------------------------------------------------------------------
#
# "Selling protection" caps or blocks daily grid export.
#   0x5E A0  set_sellingprotection  write [on u8, threshold u32le]  5 bytes
#   0x5F A0  get_sellingprotection  subscribe  empty payload
#
# "Sell Back to Grid" (Virtual Power Plant) enables/disables grid export.
#   0x05 A0  set_virtualpowerplant  write [on u8, len u8, user_id utf8]
#   0x06 A0  get_virtualpowerplant  subscribe  empty payload

_SELLING_PROTECTION_SET_TYPE = 0x5E
_SELLING_PROTECTION_GET_TYPE = 0x5F
_VIRTUALPOWERPLANT_SET_TYPE  = 0x05
_VIRTUALPOWERPLANT_GET_TYPE  = 0x06


def _build_vpp_payload(enabled: bool, user_id: str) -> bytes:
    """Build the set_virtualpowerplant (0x05) payload.

    When *user_id* is non-empty: ``[on(1B), len(1B), utf8_user_id(N B)]``.
    When *user_id* is empty: ``[on(1B)]`` only.
    """
    on_byte = bytes([0x01 if enabled else 0x00])
    if not user_id:
        return on_byte
    uid_utf8 = user_id.encode("utf-8")
    return on_byte + bytes([len(uid_utf8) & 0xFF]) + uid_utf8


def set_selling_protection(
    e2e_creds: dict,
    enabled: bool,
    threshold_w: int = 0,
    *,
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Enable/disable selling protection (grid-export cap). 0x5E fire-and-forget.

    When *enabled* is ``True`` grid export is blocked/capped; ``False`` allows it.
    *threshold_w* is the daily export cap in kWh (0 = safe default; max 300).
    """
    payload = struct.pack("<BI", 0x01 if enabled else 0x00, threshold_w & 0xFFFFFFFF)
    if log:
        log(f"SetSellingProtection payload ({len(payload)}B): {payload.hex()}")
    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _SELLING_PROTECTION_SET_TYPE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("SetSellingProtection(0x5E)", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def _is_selling_protection_payload(payload: bytes) -> bool:
    """Check if decrypted payload is a get_sellingprotection response.

    Actual device layout: [extra_byte(1B), on(1B), threshold_u32le(4B), ...]
    Minimum 6 bytes. Reject power-flow (≥20 B) and other large frames.
    The extra leading byte appears to be a firstUse/status byte (always 0x00);
    the on flag is at offset 1.
    """
    return (
        payload is not None
        and 6 <= len(payload) <= 16
        and payload[1] in (0, 1)
    )


def parse_selling_protection_response(payload: bytes | None) -> dict | None:
    """Decode a get_sellingprotection (0x5F) response payload.

    Actual device payload layout (confirmed by protocol capture):
        byte 0:     extra/status byte (firstUse flag, always 0x00)
        byte 1:     on (1 = selling protection enabled = export blocked/capped)
        bytes 2-5:  threshold as LE uint32 in kWh/day

    Returns:
        Dict with ``selling_protection_on`` (bool) and ``threshold_kwh`` (int);
        or *None* on invalid input.
    """
    if payload is None or len(payload) < 6:
        return None
    return {
        "selling_protection_on": bool(payload[1]),
        "threshold_kwh": struct.unpack_from("<I", payload, 2)[0],
    }


def get_selling_protection(
    e2e_creds: dict,
    *,
    timeout: float = 5.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read current selling-protection state (0x5F) with a fresh handshake.

    Returns a dict with ``selling_protection_on`` (bool) and ``threshold_kwh``
    (int); or *None* on failure.  With *log* set every packet is hex-dumped,
    including all drained frames (useful for protocol debugging).
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
    wake = build_wake_packet(e2e_creds, session_nonce)
    sub_pkt = build_subscription_packet(
        e2e_creds, _SELLING_PROTECTION_GET_TYPE, session_nonce,
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
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B | {resp.hex()}")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → timeout")
            return None

    def _try_decrypt_verbose(resp: bytes, label: str) -> bytes | None:
        """Decrypt without validator (for debug), log raw payload."""
        try:
            raw = decrypt_response(resp, e2e_creds["chat_secret"])
            if log and raw is not None:
                b0 = f"0x{raw[0]:02x}" if raw else "N/A"
                log(f"  {label} raw decrypted ({len(raw)}B): {raw.hex()} | byte[0]={b0}")
            return raw
        except Exception:  # noqa: BLE001
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(sub_pkt, "GetSellingProtection(0x5F)")
        if not resp:
            return None

        _try_decrypt_verbose(resp, "GetSellingProtection[0]")
        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_selling_protection_payload,
        )
        result = parse_selling_protection_response(decrypted)
        if result is not None:
            return result

        for i in range(10):
            try:
                resp, _ = sock.recvfrom(4096)
                if log:
                    log(f"Drain[{i}] ({len(resp)}B): {resp.hex()}")
                _try_decrypt_verbose(resp, f"Drain[{i}]")
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_selling_protection_payload,
                )
                result = parse_selling_protection_response(decrypted)
                if result is not None:
                    return result
            except socket.timeout:
                if log:
                    log("Drain: no more packets (timeout)")
                break

        return None
    finally:
        sock.close()


def _is_virtualpowerplant_payload(payload: bytes) -> bool:
    """Return True if payload looks like a get_virtualpowerplant (0x06) response.

    1–4 bytes, byte 0 is 0 or 1. The strict size limit avoids confusing this
    with the battery-info 0x06 response (which is much longer).
    """
    return payload is not None and 1 <= len(payload) <= 4 and payload[0] in (0, 1)


def parse_virtualpowerplant_response(payload: bytes | None) -> dict | None:
    """Decode a get_virtualpowerplant (0x06) response payload.

    Payload layout:
        byte 0:  on (1 = sell-back to grid enabled)
    """
    if payload is None or len(payload) < 1:
        return None
    return {"sell_back_to_grid_on": bool(payload[0])}


def set_virtualpowerplant(
    e2e_creds: dict,
    enabled: bool,
    *,
    user_id: str = "",
    timeout: float = 3.0,
    log: Callable[..., None] | None = None,
) -> bool:
    """Set sell-back-to-grid (VPP) state. 0x05 fire-and-forget.

    *user_id* must match the logged-in account's user_id; the device firmware
    uses it for authorisation and ignores the command when it is absent.
    """
    payload = _build_vpp_payload(enabled, user_id)
    if log:
        log(f"SetVirtualPowerPlant payload ({len(payload)}B): {payload.hex()} "
            f"(user_id={user_id!r})")
    session_nonce = generate_nonce()
    pkt = build_subscription_packet(
        e2e_creds, _VIRTUALPOWERPLANT_SET_TYPE, session_nonce, payload=payload,
    )
    results = _run_session(
        e2e_creds, [("SetVirtualPowerPlant(0x05)", pkt)],
        timeout=timeout, log=log,
    )
    _, resp = results[0]
    return resp is not None


def get_virtualpowerplant(
    e2e_creds: dict,
    *,
    timeout: float = 5.0,
    log: Callable[..., None] | None = None,
) -> dict | None:
    """Read sell-back-to-grid state (0x06) with a fresh handshake.

    Returns a dict with ``sell_back_to_grid_on`` (bool); or *None* on failure.
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
    wake = build_wake_packet(e2e_creds, session_nonce)
    sub_pkt = build_subscription_packet(
        e2e_creds, _VIRTUALPOWERPLANT_GET_TYPE, session_nonce,
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
                log(f"{label}: sent {len(pkt)}B → got {len(resp)}B | {resp.hex()}")
            return resp
        except socket.timeout:
            if log:
                log(f"{label}: sent {len(pkt)}B → timeout")
            return None

    def _try_decrypt_verbose(resp: bytes, label: str) -> bytes | None:
        try:
            raw = decrypt_response(resp, e2e_creds["chat_secret"])
            if log and raw is not None:
                b0 = f"0x{raw[0]:02x}" if raw else "N/A"
                log(f"  {label} raw decrypted ({len(raw)}B): {raw.hex()} | byte[0]={b0}")
            return raw
        except Exception:  # noqa: BLE001
            return None

    try:
        _send(home_alive, "Alive(home)")
        _send(dev_alive, "Alive(device)")
        _send(wake, "Wake")
        _send(heartbeat, "Heartbeat")
        time.sleep(0.2)

        resp = _send(sub_pkt, "GetVirtualPowerPlant(0x06)")
        if not resp:
            return None

        _try_decrypt_verbose(resp, "GetVirtualPowerPlant[0]")
        decrypted = decrypt_response(
            resp, e2e_creds["chat_secret"],
            payload_validator=_is_virtualpowerplant_payload,
        )
        result = parse_virtualpowerplant_response(decrypted)
        if result is not None:
            return result

        for i in range(10):
            try:
                resp, _ = sock.recvfrom(4096)
                if log:
                    log(f"Drain[{i}] ({len(resp)}B): {resp.hex()}")
                _try_decrypt_verbose(resp, f"Drain[{i}]")
                decrypted = decrypt_response(
                    resp, e2e_creds["chat_secret"],
                    payload_validator=_is_virtualpowerplant_payload,
                )
                result = parse_virtualpowerplant_response(decrypted)
                if result is not None:
                    return result
            except socket.timeout:
                if log:
                    log("Drain: no more packets (timeout)")
                break

        return None
    finally:
        sock.close()


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

    The session expires on the relay server after ~10 seconds without
    keepalive (status 21204). Call :meth:`keepalive` periodically (every
    ~7 seconds) from a background thread or asyncio task to prevent this.
    The session automatically re-handshakes in place on :meth:`read_power_flow`
    and :meth:`keepalive` if a 21204 error is detected.

    Typical usage (synchronous)::

        from emaldo import EmaldoClient
        from emaldo.e2e import PersistentE2ESession

        client = EmaldoClient()
        client.login(email, password)
        creds = client.e2e_login(home_id, device_id, model)

        session = PersistentE2ESession(creds, home_id=home_id)
        session.connect()
        try:
            data = session.read_power_flow()  # fast — reuses socket
            print(data)
        finally:
            session.close()

    Typical usage (with background keepalive)::

        import threading

        session = PersistentE2ESession(creds, home_id=home_id)
        session.connect()

        def _keepalive_loop():
            while not session.closed:
                time.sleep(7)
                session.keepalive()

        threading.Thread(target=_keepalive_loop, daemon=True).start()
    """

    #: Keepalive interval in seconds.  The relay server times out idle
    #: sessions after ~10 s; the coordinator's ``KEEPALIVE_INTERVAL``
    #: (7 s) must stay below that to prevent premature expiry.  This class
    #: constant is the documented reference for standalone usage.
    DEFAULT_KEEPALIVE_INTERVAL = 7

    #: Status code returned when the relay has dropped the session.
    SESSION_EXPIRED_STATUS = 21204

    #: Backoff (seconds) before re-handshaking after a 21204 (session expired).
    #: The relay rejects re-handshakes made immediately after expiry, so wait
    #: briefly before rebuilding the session.
    RECONNECT_BACKOFF_SECONDS = 2.0

    #: Extra packet drain budget after a power-flow request when the first
    #: response is not the power payload itself (for example subscription ACKs
    #: or unrelated pushes arriving first on a healthy session).
    POWER_FLOW_DRAIN_PACKETS = 5
    POWER_FLOW_DRAIN_TIMEOUT_SECONDS = 0.5

    # -- Cross-device key registry (home_id → {device_id → chat_secret}) ------
    # Populated by each coordinator during _ensure_session(). Used by the
    # stream drain loop to try every known device secret on each received
    # datagram, so packets from ALL devices on the same account are decrypted
    # instead of landing as unparsed (#47 beta15f).
    _device_key_registry: dict[str, dict[str, str]] = {}

    @classmethod
    def register_device_key(cls, home_id: str, device_id: str, chat_secret: str) -> None:
        """Register a device's chat_secret for cross-device decryption."""
        cls._device_key_registry.setdefault(home_id, {})[device_id] = chat_secret

    @classmethod
    def unregister_device_key(cls, home_id: str, device_id: str) -> None:
        """Remove a device's chat_secret from the registry."""
        cls._device_key_registry.get(home_id, {}).pop(device_id, None)

    def rekey_home(self, home_data: dict) -> None:
        """Update home-level credentials after a shared secret rotation (#47).

        Called by the home secret callback when another device on the same
        account has rotated the home ``end_secret`` (``/home/e2e-login/``).

        Updates the in-memory ``home_end_id``, ``home_group_id``,
        ``home_end_secret``, and ``home_chat_secret`` in ``self._creds``.
        The next keepalive will pick up the new values automatically — no
        UDP re-handshake needed since the relay-side ledger is already
        updated by the ``/home/e2e-login/`` call that triggered this re-key.
        """
        old_id = self._creds.get("home_end_id", "?")
        new_id = home_data.get("end_id", "")
        with self._lock:
            self._creds["home_end_id"] = new_id
            self._creds["home_group_id"] = home_data.get("group_id", "")
            self._creds["home_end_secret"] = home_data.get("end_secret", "")
            self._creds["home_chat_secret"] = home_data.get("chat_secret", "")
        if self._log:
            self._log(
                f"Rekeyed home credentials for home_id={self._home_id}: "
                f"end_id {old_id} -> {new_id}"
            )

    def __init__(
        self,
        e2e_creds: dict,
        *,
        home_id: str,
        timeout: float = 5.0,
        log: Callable[..., None] | None = None,
        creds_provider: Callable[..., dict] | None = None,
    ) -> None:
        self._home_id = home_id
        self._creds = e2e_creds
        self._timeout = timeout
        self._log = log
        # Optional callback returning fresh E2E credentials (latest, shared
        # chat_secret). Called on reconnect so the stream picks up a rotated
        # secret instead of re-handshaking with a stale one (which would 21204
        # forever). Signature: creds_provider(*, force_refresh: bool) -> dict.
        self._creds_provider = creds_provider
        self._sock: socket.socket | None = None
        self._addr: tuple[str, int] | None = None
        self._session_nonce: str | None = None
        self._closed = False
        self._regulate_frequency_cache: dict | None = None
        self._last_rtt_ms: float | None = None
        self._last_keepalive_failure_reason: str | None = None
        self._last_handshake_monotonic: float | None = None
        # Outcome of the most recent handshake: "ok" (relay replied), "no_response"
        # (silent socket rebuild — a "reconnect" that re-established nothing), or
        # "session_expired_21204". The handshake is otherwise fire-and-forget, so
        # reconnect counters alone say nothing about whether a session actually
        # came back; this exposes that for diagnostics.
        self._last_handshake_response: str | None = None
        self._last_keepalive_monotonic: float | None = None
        self._last_21204_monotonic: float | None = None
        self._last_21204_stage: str | None = None
        self._last_power_flow_diag: dict[str, int | bool] = {
            "initial_timeout": 0,
            "initial_session_expired": 0,
            "initial_nonmatching": 0,
            "drain_packets_seen": 0,
            "drain_regfreq_hits": 0,
            "drain_powerflow_hits": 0,
            "drain_session_expired": 0,
            "drain_timeout": 0,
            "drain_socket_error": 0,
            "drain_exhausted": 0,
        }
        self._lock = threading.Lock()
        # -- Subscribe-and-stream receiver state (beta13d) -------------------
        self._stream_thread: threading.Thread | None = None
        self._stream_stop = threading.Event()
        # Per-device cache: device_id → power_flow_data / monotonic_timestamp.
        # Enables cross-device decryption (#47 beta15f): each coordinator reads
        # only its own device's cached frame.
        self._latest_power_flow: dict[str, dict] = {}
        self._latest_power_flow_monotonic: dict[str, float] = {}
        self._last_subscribe_monotonic: float | None = None
        self._stream_needs_reconnect = False
        self._stream_needs_creds_refresh = False
        self._stream_resubscribe_interval = 12.0
        self._stream_keepalive_interval = 7.0
        self._stream_drain_timeout = 0.4
        self._stream_poll_sleep = 0.1
        # Adaptive resubscribe: recover a single dropped subscribe-response fast
        # without sustaining a tight cadence (which triggers relay 21204s).
        self._stream_frame_gap_resubscribe = 7.0
        self._stream_min_resubscribe_gap = 5.0
        self._stream_stale_after = 20.0
        # Long-stall watchdog: rebuild the session in place if frames stop for
        # this long (the only stream teardown path — the coordinator no longer
        # reconnects on staleness in stream mode).
        self._stream_long_stall = 45.0
        self._stream_started_monotonic: float | None = None
        self._stream_frames_received = 0
        self._stream_resubscribes = 0
        self._stream_reconnects = 0
        # Diagnostics: what triggered each stream reconnect (so a storm can be
        # diagnosed from the sensor attributes without debug logging).
        self._stream_reconnect_reasons: dict[str, int] = {}
        self._stream_last_reconnect_reason: str | None = None
        self._stream_drain_packets = 0
        self._stream_drain_unparsed = 0
        # Escalating reconnect backoff. The streak resets when a frame arrives
        # OR when a handshake succeeds (beta13e), so a single competing read
        # cannot ratchet the backoff up to the 30 s ceiling.
        self._stream_reconnect_streak = 0
        self._stream_reconnect_backoff_anchor_frames = 0
        self._stream_reconnect_backoff_max = 30.0
        # Monotonic deadline for the next reconnect attempt. The wait is served
        # by the loop's poll-sleep (lock released between iterations) instead of
        # time.sleep() under the lock, so reads never stall behind the backoff.
        self._stream_reconnect_not_before: float | None = None

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._closed

    @property
    def regulate_frequency_cache(self) -> dict | None:
        """Last 0x45 state passively captured during power-flow polling."""
        return self._regulate_frequency_cache

    @property
    def connected(self) -> bool:
        """True when the session has an open socket and valid handshake."""
        return self._sock is not None and not self._closed

    @property
    def last_rtt_ms(self) -> float | None:
        """Latest UDP request/response RTT in milliseconds."""
        return self._last_rtt_ms

    @property
    def last_keepalive_failure_reason(self) -> str | None:
        """Last keepalive failure reason for diagnostics."""
        return self._last_keepalive_failure_reason

    @property
    def last_handshake_response(self) -> str | None:
        """Outcome of the most recent handshake (ok/no_response/21204)."""
        return self._last_handshake_response

    @property
    def last_power_flow_diag(self) -> dict[str, int | bool]:
        """Diagnostics from the latest power-flow read attempt."""
        return dict(self._last_power_flow_diag)

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
        """Run alive(home) + alive(device) + wake + heartbeat."""
        started = time.perf_counter()
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
        wake = build_wake_packet(self._creds, self._session_nonce)

        r_home = self._send_raw(home_alive, "Alive(home)")
        r_dev = self._send_raw(dev_alive, "Alive(device)")
        self._send_raw(wake, "Wake")
        r_hb = self._send_raw(heartbeat, "Heartbeat")
        time.sleep(0.2)
        # Record whether the relay actually answered the handshake so reconnect
        # diagnostics can distinguish a genuinely re-established session from a
        # silent socket rebuild (the handshake is fire-and-forget otherwise).
        responses = [r for r in (r_home, r_dev, r_hb) if r is not None]
        if any(self._is_session_expired(r) for r in responses):
            self._last_handshake_response = "session_expired_21204"
        elif responses:
            self._last_handshake_response = "ok"
        else:
            self._last_handshake_response = "no_response"
        self._last_handshake_monotonic = time.perf_counter()
        if self._log:
            elapsed_ms = (self._last_handshake_monotonic - started) * 1000.0
            self._log(
                f"Handshake complete in {elapsed_ms:.1f}ms "
                f"(response={self._last_handshake_response})"
            )

    def keepalive(self) -> bool:
        """Send a fresh alive+heartbeat to keep the session alive.

        This must stay fast and non-blocking: the keepalive task is created
        via ``async_create_task`` and tracked by Home Assistant's bootstrap as
        a pending startup task, so it must not perform long operations such as
        ``time.sleep`` or a full re-handshake.  Recovery from a 21204 (session
        expired) is therefore handled by :meth:`read_power_flow`, which runs on
        a dedicated poll, not here.

        Returns:
            True if the keepalive packets were sent, False if the session has
            been dropped/expired (21204) or the socket is closed.  Returning
            False on 21204 lets the coordinator's keepalive loop tear the dead
            session down so :meth:`read_power_flow` rebuilds it next poll.
        """
        with self._lock:
            if self._sock is None or self._closed:
                self._last_keepalive_failure_reason = "closed"
                return False

            self._last_keepalive_failure_reason = None

            try:
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
                wake = build_wake_packet(self._creds, self._session_nonce)
                heartbeat = build_heartbeat_packet(self._creds, self._session_nonce)
                resp = self._send_raw(home_alive, "Keepalive(home_alive)")
                self._send_raw(dev_alive, "Keepalive(dev_alive)")
                self._send_raw(wake, "Keepalive(wake)")
                self._send_raw(heartbeat, "Keepalive(heartbeat)")
                self._last_keepalive_monotonic = time.perf_counter()
                # If the relay reports the session as expired, do NOT reconnect
                # here (would block the startup-tracked keepalive task).  Signal
                # failure so the loop tears the session down; read_power_flow
                # will rebuild it on the next poll.
                if resp is not None and self._is_session_expired(resp):
                    if self._log:
                        self._log("Keepalive saw 21204 — session expired")
                    self._last_keepalive_failure_reason = "session_expired_21204"
                    return False
                if resp is None:
                    # A healthy relay echoes the alive packet. No reply means
                    # the relay is not answering; treating this as success (the
                    # previous behaviour) masked relay unresponsiveness and fed
                    # false positives into the coordinator's healthy-keepalive
                    # reconnect-deferral. Report it as a distinct failure so a
                    # single stray timeout is tolerated (loop needs 2 in a row)
                    # but a silent relay no longer looks healthy.
                    if self._log:
                        self._log("Keepalive got no response — relay silent")
                    self._last_keepalive_failure_reason = "response_timeout"
                    return False
                return True
            except Exception as err:  # noqa: BLE001 - best-effort keepalive
                if self._log:
                    self._log(f"Keepalive failed: {err}")
                self._last_keepalive_failure_reason = "exception"
                return False

    def read_power_flow(self) -> dict | None:
        """Read realtime power flow (0x30) over the existing session.

        Returns the power flow dict, or *None* if data is not available
        (session expired, timeout, or unparseable).  On a 21204 (session
        expired) the session is re-handshaked in place with a short backoff,
        then the method returns *None* so the next scheduled poll reads from
        the refreshed session.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            return self._read_power_flow_locked(reconnect_on_expiry=True)

    def read_power_flow_for_creds(self, target_creds: dict) -> dict | None:
        """Read power flow using *target_creds* (secondary device).

        Uses the established session socket but sends the 0x30 subscription
        with *target_creds* (different ``sender_end_id`` / ``chat_secret``)
        instead of the session's own credentials.  Does NOT send
        ``Alive(home)`` — the session must already be alive via the primary
        device.

        On 21204 (session expired) returns *None* without reconnecting; the
        primary device handles re-handshake.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")
            return self._read_power_flow_locked(
                reconnect_on_expiry=False, creds=target_creds
            )

    def _read_power_flow_locked(
        self, *, reconnect_on_expiry: bool, creds: dict | None = None
    ) -> dict | None:
        """Power-flow read body.  Caller must hold ``self._lock``.

        When *reconnect_on_expiry* is *True* (first attempt), a 21204 response
        triggers an in-place re-handshake. We then return *None* and let the
        coordinator's next poll read using the refreshed session.

        *creds* — when provided (secondary device), use these credentials for
        the 0x30 subscription instead of ``self._creds`` and skip reconnect
        on 21204 (only the primary device re-handshakes).
        """
        actual_creds = creds if creds is not None else self._creds
        is_own_creds = creds is None
        self._last_power_flow_diag = {
            "initial_timeout": 0,
            "initial_session_expired": 0,
            "initial_nonmatching": 0,
            "drain_packets_seen": 0,
            "drain_regfreq_hits": 0,
            "drain_powerflow_hits": 0,
            "drain_session_expired": 0,
            "drain_timeout": 0,
            "drain_socket_error": 0,
            "drain_exhausted": 0,
            "predrain_packets": 0,
        }

        # Drain stale push frames or leftover alive responses from the socket
        # buffer before sending the 0x30 subscription.  The persistent session
        # keeps the 0x30 subscription alive between polls, so pushed frames
        # accumulate and pollute the initial recvfrom (#41).
        _predrain_timeout = self._sock.gettimeout()
        self._sock.settimeout(0.05)
        try:
            while True:
                try:
                    _stale, _ = self._sock.recvfrom(4096)
                    self._last_power_flow_diag["predrain_packets"] += 1
                except socket.timeout:
                    break
        except OSError:
            pass
        finally:
            try:
                self._sock.settimeout(_predrain_timeout)
            except OSError:
                pass

        power_pkt = build_subscription_packet(
            actual_creds, 0x30, self._session_nonce, payload=bytes([0x01]),
        )
        resp = self._send_raw(power_pkt, "PowerFlow(0x30)")
        if resp is None:
            self._last_power_flow_diag["initial_timeout"] = 1
            return None

        if self._log:
            self._log(
                f"PowerFlow(0x30) raw ({len(resp)}B): {resp[:128].hex()}"
            )
            has_90a3 = b"\x90\xa3" in resp
            has_10a3 = b"\x10\xa3" in resp
            self._log(f"  nonce markers: 90a3={has_90a3} 10a3={has_10a3}")

        # Session expired — reconnect in place and retry once.
        if self._is_session_expired(resp):
            self._last_power_flow_diag["initial_session_expired"] = 1
            now = time.perf_counter()
            self._last_21204_monotonic = now
            self._last_21204_stage = "initial"
            if self._log:
                handshake_age_ms = self._age_ms(self._last_handshake_monotonic, now)
                keepalive_age_ms = self._age_ms(self._last_keepalive_monotonic, now)
                self._log(
                    "Session expired on power-flow read "
                    f"(age_since_handshake={handshake_age_ms}, "
                    f"age_since_keepalive={keepalive_age_ms})"
                )
            if is_own_creds and reconnect_on_expiry and self._reconnect_after_expiry():
                # Retry the read immediately on the refreshed session instead
                # of deferring to the next poll. Deferring turned every 21204
                # into a guaranteed empty read (~2.4s wasted per poll) and, when
                # the relay expires the session on each first read, produced an
                # endless empty-read chain (#41). The retry passes
                # reconnect_on_expiry=False, so a second 21204 returns None and
                # cannot loop.
                if self._log:
                    self._log(
                        "Session re-handshaked after 21204; retrying power-flow "
                        "read on the refreshed session"
                    )
                return self._read_power_flow_locked(reconnect_on_expiry=False)
            return None

        result = self._try_parse_power_flow(resp, actual_creds["chat_secret"])
        if result is not None:
            return result
        self._last_power_flow_diag["initial_nonmatching"] = 1
        if self._log:
            self._log(
                f"  initial parse FAILED ({len(resp)}B); "
                f"draining up to {self.POWER_FLOW_DRAIN_PACKETS} packet(s)"
            )

        # First response may be a subscription ACK or another interleaved push;
        # drain a few more packets with a short timeout before declaring the
        # read empty. Also passively cache any 0x45 regulate-frequency pushes.
        prev_timeout = self._sock.gettimeout()
        self._sock.settimeout(self.POWER_FLOW_DRAIN_TIMEOUT_SECONDS)
        try:
            for _ in range(self.POWER_FLOW_DRAIN_PACKETS):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    self._last_power_flow_diag["drain_timeout"] = 1
                    break
                except OSError as err:
                    # Non-timeout socket error mid-drain (connection reset /
                    # closed socket) means the session is broken. Surface a
                    # typed error so the caller tears the session down and
                    # reconnects instead of leaking a raw socket traceback.
                    self._last_power_flow_diag["drain_socket_error"] = 1
                    raise EmaldoE2EError(
                        f"Socket error during power-flow drain: {err}"
                    ) from err
                self._last_power_flow_diag["drain_packets_seen"] += 1
                if self._is_session_expired(more_resp):
                    self._last_power_flow_diag["drain_session_expired"] = 1
                    self._last_21204_monotonic = time.perf_counter()
                    self._last_21204_stage = "drain"
                    if self._log:
                        self._log("Session expired mid-drain")
                    break
                result = self._try_parse_power_flow(more_resp, actual_creds["chat_secret"])
                if result is not None:
                    self._last_power_flow_diag["drain_powerflow_hits"] += 1
                    if self._log:
                        self._log(
                            f"  drain packet #{self._last_power_flow_diag['drain_packets_seen']}: "
                            f"parse OK"
                        )
                    return result
                if self._log:
                    self._log(
                        f"  drain packet #{self._last_power_flow_diag['drain_packets_seen']} "
                        f"({len(more_resp)}B): {more_resp[:128].hex()}"
                    )
                rf = self._try_parse_regulate_frequency(more_resp, actual_creds["chat_secret"])
                if rf is not None:
                    self._last_power_flow_diag["drain_regfreq_hits"] += 1
                    self._regulate_frequency_cache = rf
                    if self._log:
                        self._log(f"  drain: passive 0x45 push captured: {rf}")
        finally:
            try:
                self._sock.settimeout(prev_timeout)
            except OSError:
                pass

        self._last_power_flow_diag["drain_exhausted"] = 1

        return None

    # -- Subscribe-and-stream receiver (beta13d) ---------------------------- #

    def start_stream(
        self,
        *,
        resubscribe_interval: float = 12.0,
        keepalive_interval: float = 7.0,
        drain_timeout: float = 0.4,
        poll_sleep: float = 0.1,
        frame_gap_resubscribe: float = 7.0,
        min_resubscribe_gap: float = 5.0,
        stale_after: float = 20.0,
        long_stall: float = 45.0,
    ) -> None:
        """Start the background power-flow stream receiver.

        Subscribes once to the 0x30 power-flow stream and keeps a dedicated
        thread draining the pushed frames, re-subscribing every
        *resubscribe_interval* seconds and sending keepalives every
        *keepalive_interval* seconds (matching the official app's cadence).

        To recover quickly from an occasional dropped subscribe-response
        without sustaining a tight cadence (which makes the relay return
        21204), an *adaptive* resubscribe fires when no frame has arrived for
        *frame_gap_resubscribe* seconds — but never closer together than
        *min_resubscribe_gap* seconds, and only while the cached frame is still
        within *stale_after* (beyond that the coordinator's reconnect path
        takes over instead of hammering subscribes).

        The freshest decoded frame is available via
        :meth:`get_latest_power_flow`. This single thread owns all socket reads;
        other request/response helpers continue to work because they hold the
        same lock, serialising access.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")
            if self._stream_thread is not None and self._stream_thread.is_alive():
                return
            self._stream_resubscribe_interval = resubscribe_interval
            self._stream_keepalive_interval = keepalive_interval
            self._stream_drain_timeout = drain_timeout
            self._stream_poll_sleep = poll_sleep
            self._stream_frame_gap_resubscribe = frame_gap_resubscribe
            self._stream_min_resubscribe_gap = min_resubscribe_gap
            self._stream_stale_after = stale_after
            self._stream_long_stall = long_stall
            self._stream_stop.clear()
            self._last_subscribe_monotonic = None  # subscribe immediately
            self._last_keepalive_monotonic = time.perf_counter()
            self._stream_started_monotonic = time.perf_counter()
            self._stream_needs_reconnect = False
            self._stream_reconnect_not_before = None
            self._stream_thread = threading.Thread(
                target=self._stream_loop,
                name="emaldo-e2e-stream",
                daemon=True,
            )
            self._stream_thread.start()
    def stop_stream(self) -> None:
        """Signal the stream receiver to stop and join it (best effort)."""
        self._stream_stop.set()
        thread = self._stream_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._stream_thread = None

    @property
    def streaming(self) -> bool:
        """True while the background stream receiver thread is running."""
        t = self._stream_thread
        return t is not None and t.is_alive()

    def get_latest_power_flow(
        self, max_age: float | None = None, device_id: str | None = None,
    ) -> dict | None:
        """Return the freshest streamed power-flow frame, or *None*.

        If *device_id* is given, return only that device's cached frame.
        If *None* (backward compat), return the latest frame across all
        devices (useful for the startup frame-wait loop).

        If *max_age* is given and the cached frame is older than that many
        seconds, *None* is returned so the caller's stale/reconnect path can
        rebuild a stalled stream.
        """
        with self._lock:
            if device_id is not None:
                data = self._latest_power_flow.get(device_id)
                ts = self._latest_power_flow_monotonic.get(device_id)
                if data is None:
                    return None
                if max_age is not None and ts is not None and (time.perf_counter() - ts) > max_age:
                    return None
                return dict(data)
            # No device_id: return the most recent across all devices.
            best_data = None
            best_ts = 0.0
            now = time.perf_counter()
            for dev_id, data in self._latest_power_flow.items():
                ts = self._latest_power_flow_monotonic.get(dev_id, 0.0)
                if max_age is not None and (now - ts) > max_age:
                    continue
                if ts > best_ts:
                    best_ts = ts
                    best_data = data
            return dict(best_data) if best_data else None

    def _stream_loop(self) -> None:
        """Background receiver: subscribe, drain pushes, re-subscribe, keepalive."""
        while not self._stream_stop.is_set():
            try:
                with self._lock:
                    if self._closed or self._sock is None:
                        break
                    if self._stream_needs_reconnect:
                        # While a reconnect is pending (or its backoff window is
                        # still open) skip subscribe/keepalive/drain on the dead
                        # socket. The loop's poll-sleep below paces the backoff.
                        self._stream_reconnect_locked()
                    else:
                        now = time.perf_counter()
                        self._stream_watchdog_locked(now)
                        self._stream_maybe_subscribe_locked(now)
                        self._stream_maybe_keepalive_locked(now)
                        self._stream_drain_locked()
            except Exception as err:  # noqa: BLE001 - keep the loop alive
                if self._log:
                    self._log(f"Stream loop error: {err}")
                self._stream_flag_reconnect(f"loop_exception:{type(err).__name__}")
            self._stream_stop.wait(self._stream_poll_sleep)

    def _stream_flag_reconnect(self, reason: str) -> None:
        """Mark the stream for an in-place reconnect, recording why.

        The reason counters are exposed via the diagnostic sensor so a reconnect
        storm can be diagnosed from attributes alone (no debug logging needed).

        Only force-refresh E2E credentials when the reconnect is caused by a
        21204 (session expired).  Other reasons (long_stall, socket errors) do
        NOT rotate chat_secret, breaking the death spiral where every reconnect
        creates undecryptable pending packets (#47 beta15f).
        """
        if not self._stream_needs_reconnect:
            self._stream_last_reconnect_reason = reason
            self._stream_reconnect_reasons[reason] = (
                self._stream_reconnect_reasons.get(reason, 0) + 1
            )
        if "21204" in reason:
            self._stream_needs_creds_refresh = True
        self._stream_needs_reconnect = True

    def stream_diagnostics(self) -> dict:
        """Snapshot of stream counters for the diagnostic sensor."""
        with self._lock:
            return {
                "frames": self._stream_frames_received,
                "resubscribes": self._stream_resubscribes,
                "reconnects": self._stream_reconnects,
                "drain_packets": self._stream_drain_packets,
                "drain_unparsed": self._stream_drain_unparsed,
                "last_reconnect_reason": self._stream_last_reconnect_reason,
                "reconnect_reasons": dict(self._stream_reconnect_reasons),
                "creds_refresh_queued": self._stream_needs_creds_refresh,
            }

    def _stream_watchdog_locked(self, now: float) -> None:
        """Force an in-place reconnect if the stream has stalled for too long.

        This is the *only* stream teardown path. Short gaps are recovered by
        the adaptive resubscribe without a handshake; only a prolonged stall
        (frames stopped, or none ever arrived after start) rebuilds the
        session — avoiding the fresh-handshake startup penalty on every minor
        gap that the coordinator-level reconnect used to incur.

        Uses the most recent timestamp across ALL devices' per-device caches
        (#47 beta15f): if ANY device is still receiving frames, the stream is
        considered alive.
        """
        if self._stream_needs_reconnect:
            return
        # Latest frame across all devices (per-device cache, beta15f)
        latest_ts = max(self._latest_power_flow_monotonic.values()) if self._latest_power_flow_monotonic else None
        reference = latest_ts if latest_ts is not None else self._stream_started_monotonic
        if reference is None:
            return
        if (now - reference) > self._stream_long_stall:
            if self._log:
                kind = (
                    "no frame since start"
                    if latest_ts is None
                    else "frames stopped"
                )
                self._log(
                    f"Stream long-stall ({kind}, "
                    f"{now - reference:.0f}s) — forcing in-place reconnect"
                )
            self._stream_flag_reconnect("long_stall")

    def _stream_maybe_subscribe_locked(self, now: float) -> None:
        """Send the 0x30 subscribe on the calm periodic schedule only.

        The relay returns 21204 when 0x30 subscribes are spaced tighter than
        ~10s, so we deliberately do NOT resubscribe adaptively to chase a
        dropped frame — that triggered a reconnect storm (every early subscribe
        landed inside the spacing wall, got 21204, forced a reconnect, and
        restarted the device's ~15-20s stream-startup delay, so a frame never
        arrived). Instead the interval stays comfortably above the wall, a
        dropped subscribe-response is bridged by the wider stale window, and a
        genuine outage is rebuilt by the long-stall watchdog.
        """
        last_sub = self._last_subscribe_monotonic
        since_sub = float("inf") if last_sub is None else now - last_sub

        # Hard rate limit — never violate the relay's ~10s spacing wall.
        if since_sub < self._stream_min_resubscribe_gap:
            return
        # Periodic only: subscribe immediately after a (re)connect, then once
        # per interval thereafter.
        if last_sub is not None and since_sub < self._stream_resubscribe_interval:
            return
        if self._sock is None or self._addr is None:
            return
        pkt = build_subscription_packet(
            self._creds, 0x30, self._session_nonce, payload=bytes([0x01]),
        )
        try:
            self._sock.sendto(pkt, self._addr)
        except OSError as err:
            if self._log:
                self._log(f"Stream subscribe send failed: {err}")
            self._stream_flag_reconnect("subscribe_send_error")
            return
        self._last_subscribe_monotonic = now
        self._stream_resubscribes += 1

    def _stream_maybe_keepalive_locked(self, now: float) -> None:
        """Send alive+wake+heartbeat (fire-and-forget) on the keepalive cadence."""
        if (
            self._last_keepalive_monotonic is not None
            and (now - self._last_keepalive_monotonic) < self._stream_keepalive_interval
        ):
            return
        if self._sock is None or self._addr is None:
            return
        try:
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
            wake = build_wake_packet(self._creds, self._session_nonce)
            heartbeat = build_heartbeat_packet(self._creds, self._session_nonce)
            for pkt in (home_alive, dev_alive, wake, heartbeat):
                self._sock.sendto(pkt, self._addr)
        except OSError as err:
            if self._log:
                self._log(f"Stream keepalive send failed: {err}")
            self._stream_flag_reconnect("keepalive_send_error")
            return
        self._last_keepalive_monotonic = now

    def _stream_drain_locked(self, budget: int = 24) -> None:
        """Drain currently-buffered datagrams, caching the freshest power flow.

        Tries every known device secret on each datagram (#47 beta15f). When a
        packet was encrypted for a different device on the same home, the own-
        secret decrypt fails but the alt-key loop finds the right one.  Stores
        per-device so each coordinator reads only its own data.
        """
        if self._sock is None:
            return
        alt_keys = dict(self._device_key_registry.get(self._home_id, {}))
        # Resolve API device_id from registry (reverse lookup by chat_secret)
        # so frames are stored under the key the coordinator uses to read.
        # Fallback to sender_end_id if registry not yet populated (#47 beta15f).
        own_chat_secret = self._creds.get("chat_secret", "")
        own_device_id = self._creds.get("sender_end_id", "")
        for _api_dev_id, _secret in alt_keys.items():
            if _secret == own_chat_secret:
                own_device_id = _api_dev_id
                break
        prev_timeout = self._sock.gettimeout()
        self._sock.settimeout(self._stream_drain_timeout)
        try:
            for _ in range(budget):
                try:
                    resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    break
                except OSError as err:
                    if self._log:
                        self._log(f"Stream drain socket error: {err}")
                    self._stream_flag_reconnect("drain_socket_error")
                    break
                self._stream_drain_packets += 1
                if self._is_session_expired(resp):
                    if self._log:
                        self._log("Stream saw 21204 — flagging reconnect")
                    self._last_21204_monotonic = time.perf_counter()
                    self._last_21204_stage = "stream"
                    self._stream_flag_reconnect("session_expired_21204")
                    break

                # 1) Try own chat_secret first (fast path — most packets)
                pf = self._try_parse_power_flow(resp)
                dev_id = own_device_id
                alt_hit = False

                # 2) Alt-key loop: try every other device's secret (#47 beta15f)
                if pf is None and alt_keys:
                    for alt_dev_id, alt_secret in alt_keys.items():
                        if alt_secret == self._creds.get("chat_secret"):
                            continue  # already tried via own-creds path
                        pf = self._try_parse_power_flow(resp, chat_secret=alt_secret)
                        if pf is not None:
                            dev_id = alt_dev_id
                            alt_hit = True
                            if self._log:
                                self._log(
                                    f"[E2E] multi-key decrypt: device={alt_dev_id} "
                                    f"works (alt_key, TLV raw={resp[:48].hex()})"
                                )
                            break

                if pf is not None:
                    self._latest_power_flow[dev_id] = pf
                    self._latest_power_flow_monotonic[dev_id] = time.perf_counter()
                    self._stream_frames_received += 1
                    continue

                rf = self._try_parse_regulate_frequency(resp)
                if rf is not None:
                    self._regulate_frequency_cache = rf
                    continue

                # Force-logout detection (#47): the relay may send an encrypted
                # JSON datagram {"cmd":"force-logout"} when the home end_secret
                # was rotated by another device.  Decrypt with home_end_secret
                # and flag a reconnect + home-level refresh.
                home_secret = self._creds.get("home_end_secret", "")
                if home_secret:
                    try:
                        decoded = decrypt_response(
                            resp,
                            home_secret,
                            payload_validator=lambda d: (
                                b"logout" in d
                            ),
                        )
                        if decoded is not None:
                            text = decoded.decode("utf-8", errors="replace")
                            if self._log:
                                self._log(
                                    f"Force-logout from relay: {text}"
                                )
                            self._stream_flag_reconnect("force_logout")
                            self._stream_needs_creds_refresh = True
                            break
                    except Exception:
                        pass

                # A datagram we received but could not classify — track it so a
                # "frames=0 but packets>0" situation is visible in diagnostics.
                self._stream_drain_unparsed += 1
        finally:
            try:
                self._sock.settimeout(prev_timeout)
            except OSError:
                pass

    def _refresh_creds_locked(self) -> None:
        """Pull the latest (shared) credentials before re-handshaking.

        A 21204 means the device ``chat_secret`` was rotated (typically by a
        concurrent REST ``e2e_login``). Re-handshaking with the stale secret
        would just 21204 again, so fetch the current secret via the provider.
        Best-effort: on any failure keep the existing creds and let the
        handshake proceed (it may still fail and retry).

        Only calls the provider with ``force_refresh=True`` when the reconnect
        was triggered by a 21204 (``_stream_needs_creds_refresh``). Other
        reconnect reasons (``long_stall``, socket errors) refresh via normal
        TTL expiry, avoiding the chat_secret rotation that turns every reconnect
        into a self-inflicted decrypt failure (#47 beta15f).
        """
        if self._creds_provider is None:
            return
        needs_force = self._stream_needs_creds_refresh
        self._stream_needs_creds_refresh = False
        # Release lock before calling creds_provider to prevent self-deadlock:
        # _creds_provider → _get_home_e2e → fires home_secret_callbacks →
        # rekey_home() → tries self._lock (already held by stream thread).
        # Re-acquire after the call completes (#47).
        self._lock.release()
        try:
            fresh = self._creds_provider(force_refresh=needs_force)
        except Exception as err:  # noqa: BLE001 - best effort
            self._lock.acquire()
            if self._closed:
                return
            if self._log:
                self._log(f"Stream creds refresh failed: {err}")
            # Re-arm the flag so the next reconnect retries the forced refresh.
            if needs_force:
                self._stream_needs_creds_refresh = True
            return
        self._lock.acquire()
        if self._closed:
            return
        if fresh:
            self._creds = fresh
            if self._log:
                why = " (forced by 21204)" if needs_force else ""
                self._log(f"Stream creds refreshed{why}")

    def _stream_reconnect_locked(self) -> None:
        """Rebuild the session after a 21204/socket error and re-subscribe.

        The backoff wait is NON-BLOCKING: instead of ``time.sleep(backoff)``
        while holding ``self._lock`` (which stalled every concurrent read for up
        to the 30 s ceiling), the first call schedules a monotonic deadline and
        returns. The stream loop releases the lock between its 0.1 s poll-sleeps,
        so reads stay responsive while the backoff elapses. When the deadline
        passes a later call performs the actual rebuild.

        Escalation only grows across consecutive FAILED handshakes (a genuinely
        penalized relay). A successful handshake — or any received frame —
        resets the streak so a single competing read cannot ratchet the backoff
        up to the ceiling (beta13e).
        """
        if self._closed:
            return
        now = time.perf_counter()

        # First call after the reconnect was flagged: schedule the backoff.
        if self._stream_reconnect_not_before is None:
            # A frame since the last reconnect means the relay is healthy again.
            if (
                self._stream_frames_received
                != self._stream_reconnect_backoff_anchor_frames
            ):
                self._stream_reconnect_streak = 0
                self._stream_reconnect_backoff_anchor_frames = (
                    self._stream_frames_received
                )
            backoff = min(
                self.RECONNECT_BACKOFF_SECONDS * (2 ** self._stream_reconnect_streak),
                self._stream_reconnect_backoff_max,
            )
            self._stream_reconnect_streak += 1
            self._stream_reconnect_not_before = now + backoff
            if self._log:
                self._log(
                    f"Stream reconnect scheduled in {backoff:.1f}s "
                    f"(streak={self._stream_reconnect_streak}, "
                    f"stage={self._last_21204_stage or 'unknown'})"
                )
            return

        # Backoff window still open — let the loop keep cycling (lock released).
        if now < self._stream_reconnect_not_before:
            return

        # Deadline reached: perform the actual rebuild.
        self._stream_reconnect_not_before = None
        try:
            self._refresh_creds_locked()
            self._reconnect()
            self._stream_needs_reconnect = False
            self._last_subscribe_monotonic = None  # force immediate re-subscribe
            self._last_keepalive_monotonic = time.perf_counter()
            self._stream_started_monotonic = time.perf_counter()  # reset watchdog
            self._stream_reconnects += 1
            # A successful handshake clears the escalation: the next 21204 starts
            # fresh at the base backoff instead of inheriting a tall streak.
            self._stream_reconnect_streak = 0
            self._stream_reconnect_backoff_anchor_frames = self._stream_frames_received
        except Exception as err:  # noqa: BLE001 - best-effort reconnect
            if self._log:
                self._log(f"Stream reconnect failed: {err}")
            # Re-arm so the next loop iteration schedules a fresh (escalated)
            # backoff window; the flag stays set so the loop keeps retrying.
            self._stream_reconnect_not_before = None

    def read_regulate_frequency_state(self) -> dict | None:
        """Read FCR/mFRR frequency regulation state (0x45) over the existing session.

        Reuses the persistent socket/session so it does not conflict with the
        concurrent power-flow subscription.  Returns *None* on timeout or when
        the payload cannot be decrypted.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            req_pkt = build_subscription_packet(
                self._creds, _REGULATE_FREQ_TYPE, self._session_nonce,
                request_mode=False,  # subscription mode (0xA0) — device rejects direct-request (0x10)
            )
            resp = self._send_raw(req_pkt, "RegulateFrequencyState(0x45)")
            if resp is None:
                return self._regulate_frequency_cache

            if self._is_session_expired(resp):
                return self._regulate_frequency_cache

            result = self._try_parse_regulate_frequency(resp)
            if result is not None:
                self._regulate_frequency_cache = result
                return result

            # Drain a few extra packets (keepalive/subscription echoes)
            for _ in range(5):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    break
                if self._is_session_expired(more_resp):
                    break
                result = self._try_parse_regulate_frequency(more_resp)
                if result is not None:
                    self._regulate_frequency_cache = result
                    return result

            # No explicit response — fall back to any passively-captured push.
            return self._regulate_frequency_cache

    def _try_parse_regulate_frequency(self, resp: bytes, chat_secret: str | None = None) -> dict | None:
        """Decrypt+parse a response as a regulate-frequency payload. Returns None on mismatch.

        chat_secret — when provided (secondary device read), use this key for
        AES-CBC decryption instead of self._creds["chat_secret"].
        """
        key = chat_secret if chat_secret is not None else self._creds["chat_secret"]
        try:
            decrypted = decrypt_response(
                resp, key,
                payload_validator=_is_regulate_frequency_payload,
            )
        except Exception:  # noqa: BLE001 - best-effort parse
            return None
        return parse_regulate_frequency_state(decrypted)

    def _try_parse_power_flow(self, resp: bytes, chat_secret: str | None = None) -> dict | None:
        """Decrypt+parse a response as a power flow payload. Returns None on mismatch.

        chat_secret — when provided (secondary device read), use this key for
        AES-CBC decryption instead of self._creds["chat_secret"].
        """
        key = chat_secret if chat_secret is not None else self._creds["chat_secret"]
        try:
            decrypted = decrypt_response(
                resp, key,
                payload_validator=_is_power_flow_payload,
                fallback_ivs=[self._session_nonce.encode()],
            )
        except Exception as exc:  # noqa: BLE001 - best-effort parse
            if self._log:
                self._log(
                    f"  _try_parse_power_flow: decrypt_response raised: {exc}"
                )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "  _try_parse_power_flow: decrypt exception resp (%dB) hex=%s",
                    len(resp), resp.hex(),
                )
            return None
        if decrypted is None:
            # Fallback: home-level chat_secret (#47 beta15h)
            home_secret = self._creds.get("home_chat_secret", "")
            if home_secret and home_secret != key:
                if self._log:
                    self._log(
                        f"  _try_parse_power_flow: trying home_chat_secret "
                        f"(device key failed, resp {len(resp)}B)"
                    )
                try:
                    decrypted = decrypt_response(
                        resp, home_secret,
                        payload_validator=_is_power_flow_payload,
                        fallback_ivs=[self._session_nonce.encode()],
                    )
                except Exception:  # noqa: BLE001 - best-effort parse
                    pass
        if decrypted is None:
            if self._log:
                self._log(
                    f"  _try_parse_power_flow: decrypt_response=None "
                    f"(resp {len(resp)}B)"
                )
            if _LOGGER.isEnabledFor(logging.DEBUG):
                _LOGGER.debug(
                    "  _try_parse_power_flow: decrypt=None resp (%dB) hex=%s",
                    len(resp), resp.hex(),
                )
            return None
        if self._log:
            self._log(
                f"  _try_parse_power_flow: decrypted OK ({len(decrypted)}B): "
                f"{decrypted[:48].hex()}"
            )
        result = parse_power_flow(decrypted)
        if result is None and self._log:
            self._log(
                f"  _try_parse_power_flow: parse_power_flow FAILED "
                f"(decrypted {len(decrypted)}B)"
            )
        return result

    def read_selling_protection(self) -> dict | None:
        """Read selling-protection state (0x5F) over the existing session.

        Returns a dict with ``selling_protection_on`` (bool) and
        ``threshold_kwh`` (int); or *None* on timeout / parse failure.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            req_pkt = build_subscription_packet(
                self._creds, _SELLING_PROTECTION_GET_TYPE, self._session_nonce,
            )
            resp = self._send_raw(req_pkt, "GetSellingProtection(0x5F)")
            if resp is None:
                return None

            if self._is_session_expired(resp):
                return None

            try:
                decrypted = decrypt_response(
                    resp, self._creds["chat_secret"],
                    payload_validator=_is_selling_protection_payload,
                )
            except Exception:  # noqa: BLE001
                decrypted = None

            result = parse_selling_protection_response(decrypted)
            if result is not None:
                return result

            for _ in range(5):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    break
                if self._is_session_expired(more_resp):
                    break
                try:
                    decrypted = decrypt_response(
                        more_resp, self._creds["chat_secret"],
                        payload_validator=_is_selling_protection_payload,
                    )
                except Exception:  # noqa: BLE001
                    continue
                result = parse_selling_protection_response(decrypted)
                if result is not None:
                    return result

            return None

    def read_virtualpowerplant(self) -> dict | None:
        """Read sell-back-to-grid state (0x06) over the existing session.

        Returns a dict with ``sell_back_to_grid_on`` (bool); or *None* on
        timeout / parse failure.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            req_pkt = build_subscription_packet(
                self._creds, _VIRTUALPOWERPLANT_GET_TYPE, self._session_nonce,
            )
            resp = self._send_raw(req_pkt, "GetVirtualPowerPlant(0x06)")
            if resp is None:
                return None

            if self._is_session_expired(resp):
                return None

            try:
                decrypted = decrypt_response(
                    resp, self._creds["chat_secret"],
                    payload_validator=_is_virtualpowerplant_payload,
                )
            except Exception:  # noqa: BLE001
                decrypted = None

            result = parse_virtualpowerplant_response(decrypted)
            if result is not None:
                return result

            for _ in range(5):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    break
                if self._is_session_expired(more_resp):
                    break
                try:
                    decrypted = decrypt_response(
                        more_resp, self._creds["chat_secret"],
                        payload_validator=_is_virtualpowerplant_payload,
                    )
                except Exception:  # noqa: BLE001
                    continue
                result = parse_virtualpowerplant_response(decrypted)
                if result is not None:
                    return result

            return None

    def read_manual_selling(self) -> dict | None:
        """Read manual-selling state (0x81) over the existing session.

        Returns a dict with ``enabled`` (bool), ``target_energy_kwh`` (float),
        ``sold_so_far_kwh`` (float), ``remaining_kwh`` (float); or *None* on
        timeout / parse failure.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            req_pkt = build_subscription_packet(
                self._creds, 0x81, self._session_nonce,
            )
            resp = self._send_raw(req_pkt, "GetManualSelling(0x81)")
            if resp is None:
                return None

            if self._is_session_expired(resp):
                return None

            try:
                decrypted = decrypt_response(
                    resp, self._creds["chat_secret"],
                    payload_validator=lambda b: len(b) >= 10,
                )
            except Exception:  # noqa: BLE001
                decrypted = None

            result = parse_manual_selling_response(decrypted)
            if result is not None:
                return result

            for _ in range(5):
                try:
                    more_resp, _ = self._sock.recvfrom(4096)
                except socket.timeout:
                    break
                if self._is_session_expired(more_resp):
                    break
                try:
                    decrypted = decrypt_response(
                        more_resp, self._creds["chat_secret"],
                        payload_validator=lambda b: len(b) >= 10,
                    )
                except Exception:  # noqa: BLE001
                    continue
                result = parse_manual_selling_response(decrypted)
                if result is not None:
                    return result

            return None

    def read_battery_info(self) -> list[dict]:
        """Read per-module battery info (type 0x06) over the existing session.

        Probes all known cabinet slot indices. Empty slots return short replies
        cheaply, so they do not stop discovery; only consecutive *timeouts*
        abort the scan because each timeout costs the full socket timeout.

        Returns:
            List of battery-info dicts (one per module), possibly empty.
            Each dict contains: ``soc``, ``soh``, ``serial``, ``model``,
            ``index``, ``cabinet_index``, ``cabinet_position``,
            ``bms_temp_c``, ``electrode_a_temp_c``, ``electrode_b_temp_c``,
            ``voltage_v``, ``current_a``, ``current_energy_wh``,
            ``full_energy_wh``, ``cycle_count``, ``capacity``.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")

            batteries: list[dict] = []
            seen_serials: set[str] = set()

            TIERS = [(0, 3), (3, 5), (8, 5)]

            prev_timeout = self._sock.gettimeout()
            self._sock.settimeout(max(prev_timeout, 3.0))
            if self._log:
                self._log(
                    "BatteryInfo scan start: tiers=%s timeout=%.1fs"
                    % (TIERS, self._sock.gettimeout() or 0.0)
                )
            try:
                for tier_start, tier_size in TIERS:
                    found_in_tier = 0
                    consecutive_timeouts = 0
                    if self._log:
                        self._log(
                            f"BatteryInfo tier start: indices={tier_start}-{tier_start + tier_size - 1}"
                        )
                    for idx in range(tier_start, tier_start + tier_size):
                        if self._log:
                            self._log(f"BatteryInfo(idx={idx}): probing")
                        req_pkt = build_subscription_packet(
                            self._creds, 0x06, self._session_nonce,
                            payload=bytes([idx]),
                            request_mode=True,
                        )
                        resp = self._send_raw(req_pkt, f"BatteryInfo(idx={idx})")
                        if resp is None:
                            # True timeout — device not responding.
                            consecutive_timeouts += 1
                            if self._log:
                                self._log(
                                    f"BatteryInfo(idx={idx}): timeout "
                                    f"(consecutive={consecutive_timeouts})"
                                )
                            if consecutive_timeouts >= 2:
                                if self._log:
                                    self._log(
                                        "BatteryInfo scan aborting after consecutive timeouts; "
                                        f"modules={len(batteries)} serials={list(seen_serials)}"
                                    )
                                return batteries
                            continue

                        consecutive_timeouts = 0
                        if self._log:
                            self._log(f"BatteryInfo(idx={idx}): response {len(resp)}B")

                        # Responses shorter than the AES framing overhead (~50 B)
                        # cannot contain a valid encrypted payload — skip them.
                        # Previously used 250 B which silently discarded valid
                        # HP5000 battery responses (~243 B, #23).
                        if len(resp) < 50:
                            if self._log:
                                self._log(f"BatteryInfo(idx={idx}): too-short {len(resp)}B — skipped")
                            continue

                        info = self._try_parse_battery(resp)
                        if info is None:
                            # First packet may be a subscription ACK — try one more.
                            try:
                                extra, _ = self._sock.recvfrom(4096)
                                if self._log:
                                    self._log(f"BatteryInfo(idx={idx}) follow-up: {len(extra)}B")
                                info = self._try_parse_battery(extra)
                            except socket.timeout:
                                if self._log:
                                    self._log(f"BatteryInfo(idx={idx}) follow-up: timeout")

                        if info is None:
                            if self._log:
                                self._log(f"BatteryInfo(idx={idx}): parse failed")
                            continue

                        serial = info.get("serial") or ""
                        if serial in seen_serials:
                            if self._log:
                                self._log(
                                    f"BatteryInfo(idx={idx}): duplicate serial {serial!r}; skipped"
                                )
                            continue

                        seen_serials.add(serial)
                        info["scan_index"] = info.get("index", idx)
                        batteries.append(info)
                        found_in_tier += 1
                        if self._log:
                            self._log(
                                "BatteryInfo(idx=%s): parsed serial=%r model=%r "
                                "payload_index=%s cabinet_index=%s cabinet_position=%s soc=%s"
                                % (
                                    idx,
                                    serial,
                                    info.get("model"),
                                    info.get("index"),
                                    info.get("cabinet_index"),
                                    info.get("cabinet_position"),
                                    info.get("soc"),
                                )
                            )

                    if self._log:
                        self._log(
                            f"BatteryInfo tier complete: indices={tier_start}-{tier_start + tier_size - 1} "
                            f"found={found_in_tier}/{tier_size}"
                        )
            finally:
                self._sock.settimeout(prev_timeout)

            if self._log:
                self._log(
                    f"BatteryInfo scan complete: modules={len(batteries)} serials={list(seen_serials)}"
                )
            return batteries

    def _try_parse_battery(self, resp: bytes) -> dict | None:
        """Decrypt+parse a response as a battery-info payload. Returns None on mismatch."""
        try:
            decrypted = decrypt_response(
                resp, self._creds["chat_secret"],
                accepted_headers={HEADER_BATTERY},
            )
        except Exception:  # noqa: BLE001 - best-effort parse
            return None
        return parse_battery_data(decrypted)

    def send_command(self, msg_type: int, payload: bytes) -> bytes | None:
        """Send a single write command over the existing session socket.

        Uses the session's established nonce and socket so the relay sees the
        command on the same connection it already knows about.  This avoids the
        session-conflict that arises when ``_run_session`` opens a competing
        second socket while the persistent session is active.

        Args:
            msg_type: E2E message type byte (e.g. 0x38 for SET_THIRDPARTYPV_ON).
            payload:  Raw command payload bytes.

        Returns:
            The relay's response bytes, or *None* on timeout / closed session.
        """
        with self._lock:
            if self._sock is None or self._closed:
                raise EmaldoE2EError("Session is not connected")
            pkt = build_subscription_packet(
                self._creds, msg_type, self._session_nonce, payload=payload,
            )
            return self._send_raw(pkt, f"Command(0x{msg_type:02x})")

    def close(self) -> None:
        """Close the socket and mark the session closed."""
        # Signal the stream receiver to stop before taking the lock so it can
        # exit its loop; it is a daemon thread and will not block shutdown.
        self._stream_stop.set()
        with self._lock:
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
        started = time.perf_counter()
        try:
            resp, _ = self._sock.recvfrom(4096)
            self._last_rtt_ms = (time.perf_counter() - started) * 1000.0
            if self._log:
                self._log(
                    f"{label}: sent {len(pkt)}B → got {len(resp)}B "
                    f"(rtt={self._last_rtt_ms:.1f}ms)"
                )
            return resp
        except socket.timeout:
            if self._log:
                self._log(f"{label}: sent {len(pkt)}B → no response")
            return None

    def _reconnect(self) -> None:
        """Close and re-open the session socket and re-run the handshake.

        Raises whatever ``_do_handshake`` raises on failure (caller decides
        whether to suppress).  Generates a fresh session nonce.
        """
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

    def _reconnect_after_expiry(self) -> bool:
        """Re-handshake after a 21204 (session expired), honoring backoff.

        Returns *True* if the new handshake completed, *False* if it failed
        or the session has been closed concurrently.  Must be called while
        already holding ``self._lock``.
        """
        if self._closed:
            return False
        reconnect_started = time.perf_counter()
        expiry_age_ms = self._age_ms(self._last_21204_monotonic, reconnect_started)
        if self._log:
            self._log(
                "Session expired (21204) "
                f"stage={self._last_21204_stage or 'unknown'} "
                f"age_since_21204={expiry_age_ms} — reconnecting after "
                f"{self.RECONNECT_BACKOFF_SECONDS:.1f}s backoff"
            )
        time.sleep(self.RECONNECT_BACKOFF_SECONDS)
        try:
            self._reconnect()
            if self._log:
                elapsed_ms = (time.perf_counter() - reconnect_started) * 1000.0
                self._log(f"Reconnect after 21204 completed in {elapsed_ms:.1f}ms")
            return True
        except Exception as err:  # noqa: BLE001 - best-effort reconnect
            if self._log:
                self._log(f"Reconnect failed: {err}")
            return False

    @staticmethod
    def _age_ms(event_ts: float | None, now_ts: float) -> str:
        """Format age from event timestamp to ``now_ts`` for debug logs."""
        if event_ts is None:
            return "n/a"
        return f"{(now_ts - event_ts) * 1000.0:.1f}ms"


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
