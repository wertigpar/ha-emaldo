"""Emaldo battery system API client (bundled for HA integration)."""

from .client import EmaldoClient
from .exceptions import EmaldoError, EmaldoAuthError, EmaldoAPIError, EmaldoConnectionError

__all__ = ["EmaldoClient", "EmaldoError", "EmaldoAuthError", "EmaldoAPIError", "EmaldoConnectionError"]
