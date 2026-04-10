"""Exceptions for the Emaldo client library."""


class EmaldoError(Exception):
    """Base exception for all Emaldo errors."""


class EmaldoAuthError(EmaldoError):
    """Authentication or session error."""


class EmaldoAPIError(EmaldoError):
    """API request failed."""

    def __init__(self, message: str, status: int = 0, response: dict | None = None):
        super().__init__(message)
        self.status = status
        self.response = response or {}


class EmaldoConnectionError(EmaldoError):
    """Network / TLS connection error."""


class EmaldoE2EError(EmaldoError):
    """E2E (UDP) protocol error."""
