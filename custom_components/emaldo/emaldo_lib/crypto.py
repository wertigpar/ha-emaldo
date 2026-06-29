"""Cryptographic helpers for Emaldo API communication.

The Emaldo API uses RC4 encryption with Snappy compression on responses.
"""

import time

import cramjam
from Crypto.Cipher import ARC4

from .const import get_app_secret


def rc4_crypt(key: bytes, data: bytes) -> bytes:
    """RC4 encrypt/decrypt (symmetric)."""
    cipher = ARC4.new(key)
    return cipher.encrypt(data)


def encrypt_field_with_secret(secret: bytes, plaintext: str) -> str:
    """Encrypt plaintext using an explicit app secret: RC4 -> hex."""
    raw = plaintext.encode("utf-8")
    encrypted = rc4_crypt(secret, raw)
    return encrypted.hex()


def encrypt_field(plaintext: str) -> str:
    """Encrypt a string field for API requests: RC4 → hex."""
    return encrypt_field_with_secret(get_app_secret(), plaintext)


def decrypt_response_with_secret(secret: bytes, hex_str: str) -> str:
    """Decrypt API response using an explicit app secret."""
    raw = bytes.fromhex(hex_str)
    decrypted = rc4_crypt(secret, raw)
    try:
        decompressed = bytes(cramjam.snappy.decompress_raw(decrypted))
        return decompressed.decode("utf-8")
    except Exception:
        # Some responses may not be Snappy-compressed
        return decrypted.decode("utf-8")


def decrypt_response(hex_str: str) -> str:
    """Decrypt an API response field: hex → RC4 → Snappy decompress."""
    return decrypt_response_with_secret(get_app_secret(), hex_str)


def make_gmtime() -> int:
    """Generate timestamp in nanoseconds (matches app's currentTimeMillis * 1000000)."""
    return int(time.time() * 1000) * 1000000
