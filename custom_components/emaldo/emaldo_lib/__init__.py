"""Emaldo battery system API client (bundled for HA integration)."""

from .client import EmaldoClient
from .e2e import PersistentE2ESession
from .exceptions import EmaldoError, EmaldoAuthError, EmaldoAPIError, EmaldoConnectionError

__all__ = ["EmaldoClient", "PersistentE2ESession", "EmaldoError", "EmaldoAuthError", "EmaldoAPIError", "EmaldoConnectionError"]
