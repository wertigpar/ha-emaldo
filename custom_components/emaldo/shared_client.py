"""Shared REST client management for Emaldo accounts."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from collections.abc import Callable
from typing import Any

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
    # 21204 storm guard (Phase 2.3): per-home storm-state holder resolver
    # injected by the coordinator; forwarded to every EmaldoClient created
    # by this shared instance.
    storm_state_provider: Callable[[str], Any] | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def ensure_client(self) -> EmaldoClient:
        """Return an authenticated client, logging in on demand.

        Side-effect free with respect to global state: the emaldo_lib app-id
        module globals used by the E2E packet builders are written once per
        process by the owning entry's ``async_setup_entry``, never per call.
        """
        with self._lock:
            if self.client is None or not self.client.is_authenticated:
                self.client = EmaldoClient(
                    app_id=self.app_id,
                    app_secret=self.app_secret,
                    app_version=self.app_version,
                    storm_state_provider=self.storm_state_provider,
                )
                self.client.login(self.email, self.password)
            return self.client

    def reset_auth(self) -> None:
        """Drop only the REST session/token; keep E2E caches and storm guards."""
        with self._lock:
            if self.client is not None:
                self.client.invalidate_auth()   # new, see 1.2

    def reset(self) -> None:   # unchanged hard reset, now used only for auth errors
        """Drop the shared client so the next operation re-authenticates.

        The outgoing client's HTTP session is closed before the reference is
        dropped — otherwise every hard reset (reached from ~12 transient-error
        paths in the coordinators) leaked one ``requests.Session`` plus its
        urllib3 connection pool until the next unload. ``ensure_client()``
        rebuilds transparently on the next call.
        """
        with self._lock:
            if self.client is not None:
                self.client.close()
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
        # Last entry on this account is going away: close the HTTP session
        # before the store entry (and the last reference to the client) is
        # dropped, so a full unload leaks no urllib3 pool either.
        with shared_client._lock:
            if shared_client.client is not None:
                shared_client.client.close()
            shared_client.client = None
        store.pop(key, None)
    if not store:
        hass.data.pop(_SHARED_CLIENTS_DATA_KEY, None)