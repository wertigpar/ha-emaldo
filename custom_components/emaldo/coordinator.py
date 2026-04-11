"""DataUpdateCoordinators for Emaldo.

Two coordinators:
* :class:`EmaldoCoordinator` — slow REST + battery details (60s interval)
* :class:`EmaldoRealtimeCoordinator` — fast E2E power flow via a persistent
  UDP session (10s interval).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .emaldo_lib import (
    EmaldoClient,
    EmaldoAuthError,
    EmaldoConnectionError,
    PersistentE2ESession,
)
from .emaldo_lib.const import set_params

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_SCAN_INTERVAL,
    REALTIME_SCAN_INTERVAL,
    KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class EmaldoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Slow coordinator for REST battery/power data (60s)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._entry = entry
        self._client: EmaldoClient | None = None
        self._device_id: str | None = None
        self._model: str | None = None
        self._device_name: str | None = None

    @property
    def home_id(self) -> str:
        """Return the configured home ID."""
        return self._entry.data[CONF_HOME_ID]

    @property
    def device_id(self) -> str | None:
        """Return the discovered device ID."""
        return self._device_id

    @property
    def device_model(self) -> str | None:
        """Return the discovered device model."""
        return self._model

    @property
    def device_name(self) -> str | None:
        """Return the discovered device name."""
        return self._device_name

    def _ensure_client(self) -> EmaldoClient:
        """Create and authenticate the client if needed."""
        data = self._entry.data

        # Always set app params before any client operation
        set_params(data[CONF_APP_ID], data[CONF_APP_SECRET], data[CONF_APP_VERSION])

        if self._client is None or not self._client.is_authenticated:
            self._client = EmaldoClient(app_version=data[CONF_APP_VERSION])
            self._client.login(data[CONF_EMAIL], data[CONF_PASSWORD])

        if self._device_id is None:
            did, model, name = self._client.find_device(self.home_id)
            self._device_id = did
            self._model = model
            self._device_name = name

        return self._client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch battery + power data from the REST API."""
        for attempt in range(2):
            try:
                client = await self.hass.async_add_executor_job(self._ensure_client)
                battery = await self.hass.async_add_executor_job(
                    client.get_battery, self.home_id, self._device_id, self._model
                )
                power = await self.hass.async_add_executor_job(
                    client.get_power, self.home_id, self._device_id, self._model
                )
                break
            except EmaldoAuthError:
                self._client = None
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    continue
                raise UpdateFailed("Authentication failed after retry") from None
            except EmaldoConnectionError as err:
                raise UpdateFailed(f"Connection error: {err}") from err
            except Exception as err:
                raise UpdateFailed(f"Error fetching Emaldo data: {err}") from err

        return {
            "battery": battery,
            "power": power,
        }


class EmaldoRealtimeCoordinator(DataUpdateCoordinator[dict[str, Any] | None]):
    """Fast coordinator for E2E real-time power flow (10s).

    Uses :class:`PersistentE2ESession` to keep a UDP socket open across polls,
    reducing latency from ~500ms to ~85ms per read. A background task sends
    keepalive messages every 15 seconds to prevent the relay server from
    dropping the session.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        parent: EmaldoCoordinator,
    ) -> None:
        """Initialize the realtime coordinator.

        Args:
            hass: Home Assistant instance.
            entry: Config entry.
            parent: The slow :class:`EmaldoCoordinator` — used to share the
                authenticated REST client and device discovery.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_realtime",
            update_interval=timedelta(seconds=REALTIME_SCAN_INTERVAL),
        )
        self._entry = entry
        self._parent = parent
        self._session: PersistentE2ESession | None = None
        self._keepalive_task: asyncio.Task | None = None

    # -- Proxy properties so sensors can share one class across coordinators --

    @property
    def home_id(self) -> str:
        return self._parent.home_id

    @property
    def device_id(self) -> str | None:
        return self._parent.device_id

    @property
    def device_model(self) -> str | None:
        return self._parent.device_model

    @property
    def device_name(self) -> str | None:
        return self._parent.device_name

    async def async_shutdown(self) -> None:
        """Cancel keepalive and close the UDP session."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        if self._session is not None:
            await self.hass.async_add_executor_job(self._session.close)
            self._session = None

    def _ensure_session(self) -> PersistentE2ESession:
        """Create and connect the persistent E2E session if needed."""
        if self._session is not None and not self._session.closed:
            return self._session

        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")

        creds = client.e2e_login(home_id, device_id, model)
        self._session = PersistentE2ESession(creds)
        self._session.connect()
        return self._session

    def _read_power_flow(self) -> dict | None:
        """Synchronous helper that runs in the executor."""
        session = self._ensure_session()
        data = session.read_power_flow()
        if data is None and session.closed:
            # Session died mid-read — force recreation on next call
            self._session = None
        return data

    async def _async_update_data(self) -> dict[str, Any] | None:
        """Fetch realtime power flow via the persistent E2E session."""
        try:
            data = await self.hass.async_add_executor_job(self._read_power_flow)
        except EmaldoAuthError as err:
            # Token expired — force REST re-login and E2E reconnect
            self._parent._client = None  # noqa: SLF001
            await self._close_session()
            raise UpdateFailed(f"E2E auth failed: {err}") from err
        except Exception as err:
            await self._close_session()
            raise UpdateFailed(f"E2E power flow read failed: {err}") from err

        if data is None:
            await self._close_session()
            raise UpdateFailed("No power flow data returned")

        # Ensure keepalive task is running
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.hass.async_create_task(
                self._keepalive_loop(), name=f"{DOMAIN}_keepalive"
            )

        return data

    async def _close_session(self) -> None:
        """Close the current session (if any)."""
        if self._session is not None:
            try:
                await self.hass.async_add_executor_job(self._session.close)
            except Exception:  # noqa: BLE001
                pass
            self._session = None

    async def _keepalive_loop(self) -> None:
        """Periodically send alive+heartbeat to keep the relay session alive."""
        fail_count = 0
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._session is None or self._session.closed:
                    return
                try:
                    ok = await self.hass.async_add_executor_job(self._session.keepalive)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Keepalive error: %s", err)
                    ok = False
                if ok:
                    fail_count = 0
                else:
                    fail_count += 1
                    _LOGGER.debug("Keepalive fail #%d", fail_count)
                    if fail_count >= 2:
                        _LOGGER.info("Keepalive failed twice, closing session for reconnect")
                        await self._close_session()
                        return
        except asyncio.CancelledError:
            pass
