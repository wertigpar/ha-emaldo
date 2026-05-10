"""Select platform for Emaldo integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    EV_UNSUPPORTED_MODELS,
)
from .coordinator import EmaldoCoordinator

_LOGGER = logging.getLogger(__name__)

# EV mode option keys and display names
EV_MODE_OPTIONS: dict[str, int] = {
    "lowest_price": 1,
    "solar_only": 2,
    "scheduled": 3,
    "instant_full": 4,
    "instant_fixed": 5,
}
EV_MODE_BY_INT: dict[int, str] = {v: k for k, v in EV_MODE_OPTIONS.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo select entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    power_coordinator: EmaldoCoordinator = data["power"]

    entities: list[SelectEntity] = []

    model = power_coordinator.device_model or ""
    if model not in EV_UNSUPPORTED_MODELS:
        entities.append(EmaldoEVChargeModeSelect(power_coordinator))

    async_add_entities(entities)


class EmaldoEVChargeModeSelect(CoordinatorEntity[EmaldoCoordinator], SelectEntity):
    """Select entity to read and write the EV charging mode."""

    _attr_has_entity_name = True
    _attr_name = "EV charge mode"
    _attr_options = list(EV_MODE_OPTIONS.keys())
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_ev_charge_mode"

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
    def current_option(self) -> str | None:
        if self.coordinator.data is None:
            return None
        ev = self.coordinator.data.get("ev")
        if not isinstance(ev, dict):
            return None
        mode_int = ev.get("mode")
        return EV_MODE_BY_INT.get(mode_int)

    async def async_select_option(self, option: str) -> None:
        """Write the selected EV mode to the device."""
        mode_int = EV_MODE_OPTIONS.get(option)
        if mode_int is None:
            _LOGGER.error("Unknown EV mode option: %s", option)
            return
        ev_data = (self.coordinator.data or {}).get("ev") or {}
        fixed_kwh = int(ev_data.get("fixed_kwh", 0))
        await self.hass.async_add_executor_job(
            self.coordinator._write_ev_mode, mode_int, fixed_kwh
        )
        await self.coordinator.async_request_refresh()
