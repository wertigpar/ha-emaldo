"""Number platform for Emaldo integration — EV fixed charge amount."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EV_UNSUPPORTED_MODELS
from .coordinator import EmaldoCoordinator
from .emaldo_lib.e2e import EV_MODE_INSTANT_FIXED

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo number entities from a config entry."""
    coordinator: EmaldoCoordinator = hass.data[DOMAIN][entry.entry_id]["power"]

    model = coordinator.device_model or ""
    if model not in EV_UNSUPPORTED_MODELS:
        async_add_entities([EmaldoEVFixedChargeAmount(coordinator)])


class EmaldoEVFixedChargeAmount(CoordinatorEntity[EmaldoCoordinator], NumberEntity):
    """Number entity for the EV fixed charge amount (Instant Fixed mode)."""

    _attr_has_entity_name = True
    _attr_name = "EV fixed charge amount"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_native_min_value = 1
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:battery-charging-outline"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_ev_fixed_charge_amount"

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        ev = self.coordinator.data.get("ev")
        if not isinstance(ev, dict):
            return None
        kwh = ev.get("fixed_kwh")
        # Use the device's slider max as the entity max when available
        full = ev.get("fixed_full_kwh")
        if full and full > self._attr_native_max_value:
            self._attr_native_max_value = float(full)
        return float(kwh) if kwh is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write the fixed charge amount to the device (activates Instant Fixed mode)."""
        fixed_kwh = int(value)
        await self.hass.async_add_executor_job(
            self.coordinator._write_ev_mode, EV_MODE_INSTANT_FIXED, fixed_kwh
        )
        await self.coordinator.async_request_refresh()
