"""Shared REST client management for Emaldo accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_APP_VERSION,
)
from .emaldo_lib import EmaldoClient
from .emaldo_lib.const import set_params

_SHARED_CLIENTS_DATA_KEY = f"{DOMAIN}_shared_clients"


@dataclass(slots=True)
class SharedEmaldoClient:
    """Reference-counted REST client shared across matching config entries."""

    email: str
    password: str
    app_id: str
    app_secret: str
    app_version: str
    ref_count: int = 0
    client: EmaldoClient | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def ensure_client(self) -> EmaldoClient:
        """Return an authenticated client, logging in on demand."""
        with self._lock:
            # E2E packet builders still consume global app-id from emaldo_lib.const.
            # Keep it synchronized with this shared account tuple for now.
            set_params(self.app_id, self.app_secret, self.app_version)
            if self.client is None or not self.client.is_authenticated:
                self.client = EmaldoClient(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    app_version=self.app_version,
                )
                self.client.login(self.email, self.password)
            return self.client

    def reset(self) -> None:
        """Drop the shared client so the next operation re-authenticates."""
        with self._lock:
            self.client = None


def _shared_client_key(entry: ConfigEntry) -> tuple[str, str, str, str, str]:
    data = entry.data
    return (
        data[CONF_EMAIL],
        data[CONF_PASSWORD],
        data.get(CONF_APP_ID, DEFAULT_APP_ID),
        data.get(CONF_APP_SECRET, DEFAULT_APP_SECRET),
        data.get(CONF_APP_VERSION, DEFAULT_APP_VERSION),
    )


def async_acquire_shared_client(
    hass: HomeAssistant, entry: ConfigEntry
) -> SharedEmaldoClient:
    """Get or create a shared client for this account/app tuple."""
    store: dict[tuple[str, str, str, str, str], SharedEmaldoClient] = hass.data.setdefault(
        _SHARED_CLIENTS_DATA_KEY, {}
    )
    key = _shared_client_key(entry)
    shared_client = store.get(key)
    if shared_client is None:
        shared_client = SharedEmaldoClient(*key)
        store[key] = shared_client
    shared_client.ref_count += 1
    return shared_client


def async_release_shared_client(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Release a shared client reference for this config entry."""
    store: dict[tuple[str, str, str, str, str], SharedEmaldoClient] | None = hass.data.get(
        _SHARED_CLIENTS_DATA_KEY
    )
    if not store:
        return

    key = _shared_client_key(entry)
    shared_client = store.get(key)
    if shared_client is None:
        return

    shared_client.ref_count = max(0, shared_client.ref_count - 1)
    if shared_client.ref_count == 0:
        store.pop(key, None)
    if not store:
        hass.data.pop(_SHARED_CLIENTS_DATA_KEY, None)