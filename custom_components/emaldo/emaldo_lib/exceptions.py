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
    """E2E (UDP) protocol error.

    Base class for all E2E failures. Kept as the catch-all so existing
    ``except EmaldoE2EError`` handlers continue to work; the subclasses
    below allow targeted recovery (e.g. refresh credentials and retry).
    """


class EmaldoE2ETimeout(EmaldoE2EError):
    """E2E UDP request timed out (no response before the deadline)."""


class EmaldoE2ESessionExpired(EmaldoE2EError):
    """E2E relay/device session expired or became invalid (e.g. 21204)."""


class EmaldoE2EProtocolError(EmaldoE2EError):
    """Unexpected, malformed, or unsupported E2E response."""


class EmaldoE2EDecryptError(EmaldoE2EProtocolError):
    """E2E response could not be decrypted or validated."""
