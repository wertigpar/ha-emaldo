"""Switch platform for Emaldo integration — Third-party PV."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmaldoRealtimeCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo switch entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    realtime_coordinator: EmaldoRealtimeCoordinator = data["realtime"]

    async_add_entities([EmaldoThirdPartyPVSwitch(realtime_coordinator)])


class EmaldoThirdPartyPVSwitch(
    CoordinatorEntity[EmaldoRealtimeCoordinator], SwitchEntity
):
    """Switch entity for enabling/disabling Third-Party PV (3rd-party solar).

    The current state is read from the E2E power-flow response (type 0x30,
    byte 19). Toggling the switch sends a SET_THIRDPARTYPV_ON command
    (type 0x41) to the device.
    """

    _attr_has_entity_name = True
    _attr_name = "Third-party PV"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: EmaldoRealtimeCoordinator) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_thirdparty_pv_on"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when third-party PV is enabled."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("thirdparty_pv_on")

    async def async_turn_on(self, **kwargs) -> None:
        """Enable third-party PV."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_thirdparty_pv, True  # noqa: SLF001
        )
        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs) -> None:
        """Disable third-party PV."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_thirdparty_pv, False  # noqa: SLF001
        )
        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()
