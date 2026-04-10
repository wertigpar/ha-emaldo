"""The Emaldo Battery integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .coordinator import EmaldoCoordinator
from .schedule_coordinator import EmaldoScheduleCoordinator
from .services import async_register_services, async_unregister_services

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Emaldo from a config entry."""
    power_coordinator = EmaldoCoordinator(hass, entry)
    await power_coordinator.async_config_entry_first_refresh()

    schedule_coordinator = EmaldoScheduleCoordinator(hass, entry)
    await schedule_coordinator.async_config_entry_first_refresh()
    schedule_coordinator.async_setup_listeners()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "power": power_coordinator,
        "schedule": schedule_coordinator,
    }

    entry.async_on_unload(
        entry.add_update_listener(_async_options_updated)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    return True


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — restart schedule listeners."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if data:
        schedule_coordinator: EmaldoScheduleCoordinator = data["schedule"]
        schedule_coordinator.async_setup_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        data["schedule"].async_shutdown()
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok
