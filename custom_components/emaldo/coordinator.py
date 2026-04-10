"""DataUpdateCoordinator for Emaldo."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .emaldo_lib import EmaldoClient, EmaldoAuthError, EmaldoConnectionError
from .emaldo_lib.const import set_params

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_SCAN_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class EmaldoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator to poll Emaldo API."""

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
        """Fetch data from Emaldo API."""
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

        # E2E power flow (best-effort, don't fail the whole update)
        power_flow = None
        try:
            power_flow = await self.hass.async_add_executor_job(
                client.get_power_flow,
                self.home_id, self._device_id, self._model,
            )
        except Exception as err:
            _LOGGER.debug("E2E power flow read failed: %s", err)

        return {
            "battery": battery,
            "power": power,
            "power_flow": power_flow,
        }
