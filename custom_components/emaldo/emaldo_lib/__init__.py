"""Emaldo battery system API client (bundled for HA integration)."""

from .client import EmaldoClient
from .e2e import PersistentE2ESession
from .exceptions import (
    EmaldoError,
    EmaldoAuthError,
    EmaldoAPIError,
    EmaldoConnectionError,
    EmaldoE2EError,
    EmaldoE2ETimeout,
    EmaldoE2ESessionExpired,
    EmaldoE2EProtocolError,
    EmaldoE2EDecryptError,
)

__all__ = [
    "EmaldoClient",
    "PersistentE2ESession",
    "EmaldoError",
    "EmaldoAuthError",
    "EmaldoAPIError",
    "EmaldoConnectionError",
    "EmaldoE2EError",
    "EmaldoE2ETimeout",
    "EmaldoE2ESessionExpired",
    "EmaldoE2EProtocolError",
    "EmaldoE2EDecryptError",
]
