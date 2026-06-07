"""The Emaldo Battery integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_DEVICE_ID,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_APP_VERSION,
)
from .coordinator import EmaldoCoordinator, EmaldoRealtimeCoordinator
from .schedule_coordinator import EmaldoScheduleCoordinator
from .shared_client import async_acquire_shared_client, async_release_shared_client
from .services import async_register_services, async_unregister_services

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.TIME,
]


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entries to newer schema versions."""
    if entry.version < 2:
        migrated = dict(entry.data)
        migrated.setdefault(CONF_APP_ID, DEFAULT_APP_ID)
        migrated.setdefault(CONF_APP_SECRET, DEFAULT_APP_SECRET)
        migrated.setdefault(CONF_APP_VERSION, DEFAULT_APP_VERSION)
        hass.config_entries.async_update_entry(entry, data=migrated, version=2)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Emaldo from a config entry."""
    shared_client = async_acquire_shared_client(hass, entry)
    try:
        power_coordinator = EmaldoCoordinator(hass, entry, shared_client)
        await power_coordinator.async_config_entry_first_refresh()

        selected_device_id = entry.data.get(CONF_DEVICE_ID)
        devices_to_setup: list[dict[str, str | None]] = [
            {
                "id": power_coordinator.device_id,
                "model": power_coordinator.device_model,
                "name": power_coordinator.device_name,
            }
        ]

        if not selected_device_id:
            def _list_devices() -> list[dict]:
                client = power_coordinator._ensure_client()  # noqa: SLF001
                return client.list_devices(entry.data[CONF_HOME_ID])

            discovered_devices = await hass.async_add_executor_job(_list_devices)
            seen_ids = {power_coordinator.device_id}
            for device in discovered_devices:
                did = str(device.get("id", "")) or None
                if did is None or did in seen_ids:
                    continue
                devices_to_setup.append(
                    {
                        "id": did,
                        "model": device.get("model"),
                        "name": device.get("name", did),
                    }
                )
                seen_ids.add(did)

        coordinator_sets: list[dict[str, object]] = []

        for i, device in enumerate(devices_to_setup):
            if i == 0:
                power = power_coordinator
                setattr(power, "_legacy_uid_mode", True)
            else:
                power = EmaldoCoordinator(
                    hass,
                    entry,
                    shared_client,
                    device_id=device["id"],
                    device_model=device["model"],
                    device_name=device["name"],
                    persist_device_binding=False,
                )
                setattr(power, "_legacy_uid_mode", False)
                await power.async_config_entry_first_refresh()

            realtime = EmaldoRealtimeCoordinator(hass, entry, power)
            setattr(realtime, "_legacy_uid_mode", getattr(power, "_legacy_uid_mode", False))
            # Best-effort first refresh — if E2E fails, keep the integration
            # working with the slower REST power data.
            try:
                await realtime.async_config_entry_first_refresh()
            except Exception:  # noqa: BLE001
                pass

            schedule = EmaldoScheduleCoordinator(hass, entry, power)
            setattr(schedule, "_legacy_uid_mode", getattr(power, "_legacy_uid_mode", False))
            await schedule.async_config_entry_first_refresh()
            schedule.async_setup_listeners()

            coordinator_sets.append(
                {
                    "power": power,
                    "realtime": realtime,
                    "schedule": schedule,
                }
            )

        primary = coordinator_sets[0]
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
            "power": primary["power"],
            "realtime": primary["realtime"],
            "schedule": primary["schedule"],
            "devices": coordinator_sets,
        }
    except Exception:
        async_release_shared_client(hass, entry)
        raise

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
        devices = data.get("devices") or [data]
        for item in devices:
            schedule_coordinator: EmaldoScheduleCoordinator = item["schedule"]
            schedule_coordinator.async_setup_listeners()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        data = hass.data[DOMAIN].pop(entry.entry_id)
        devices = data.get("devices") or [data]
        for item in devices:
            item["schedule"].async_shutdown()
            await item["realtime"].async_shutdown()
        async_release_shared_client(hass, entry)
        if not hass.data[DOMAIN]:
            async_unregister_services(hass)
    return unload_ok
