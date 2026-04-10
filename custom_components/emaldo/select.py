"""Select platform for Emaldo integration."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    CONTROL_PRIORITY_INTERNAL,
    CONTROL_PRIORITY_OVERRIDE,
)
from .schedule_coordinator import EmaldoScheduleCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo select entities from a config entry."""
    schedule_coordinator: EmaldoScheduleCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]["schedule"]

    async_add_entities([EmaldoControlPrioritySelect(schedule_coordinator)])


class EmaldoControlPrioritySelect(SelectEntity):
    """Virtual select entity for controlling override priority."""

    _attr_has_entity_name = True
    _attr_name = "Control priority"
    _attr_options = [CONTROL_PRIORITY_INTERNAL, CONTROL_PRIORITY_OVERRIDE]
    _attr_current_option = CONTROL_PRIORITY_INTERNAL

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.home_id}_control_priority"

    @property
    def device_info(self) -> DeviceInfo:
        c = self._coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    async def async_select_option(self, option: str) -> None:
        """Handle option selection."""
        self._attr_current_option = option
        self.async_write_ha_state()
