"""The Emaldo Battery integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

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

_LOGGER = logging.getLogger(__name__)

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
    _LOGGER.debug(
        "Setting up Emaldo config entry: entry_id=%s home_id=%s pinned_device_id=%s",
        entry.entry_id,
        entry.data.get(CONF_HOME_ID),
        entry.data.get(CONF_DEVICE_ID),
    )
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

            _LOGGER.debug(
                "Emaldo config entry discovered %d device coordinator set(s): %s",
                len(devices_to_setup),
                [device.get("id") for device in devices_to_setup],
            )

        coordinator_sets: list[dict[str, object]] = []

        # Detect whether this config entry was originally set up with the legacy
        # home_id-based unique ID scheme. Multiple config entries can share the
        # same home_id (e.g. two batteries on one account), so legacy mode must
        # be scoped to the entry's own entity registry — not assumed True.
        ent_reg = er.async_get(hass)
        home_id = entry.data[CONF_HOME_ID]
        existing_uids = {
            e.unique_id
            for e in er.async_entries_for_config_entry(ent_reg, entry.entry_id)
        }
        has_legacy_uids = f"{home_id}_battery_soc" in existing_uids

        for i, device in enumerate(devices_to_setup):
            if i == 0:
                power = power_coordinator
                setattr(power, "_legacy_uid_mode", has_legacy_uids)
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
            # Start the realtime coordinator in the background. Its E2E UDP
            # handshake can block for several seconds (or retry) and must not
            # delay HA bootstrap. Failures are non-fatal — sensors fall back to
            # the slower REST power data until the next successful refresh.
            entry.async_create_background_task(
                hass,
                _background_first_refresh(realtime, "realtime"),
                f"{DOMAIN}_realtime_first_refresh",
            )

            schedule = EmaldoScheduleCoordinator(hass, entry, power)
            setattr(schedule, "_legacy_uid_mode", getattr(power, "_legacy_uid_mode", False))
            # Schedule first refresh is also best-effort; the E2E override read
            # in particular can stall. Start it in the background and set up
            # the time-based listeners immediately so scheduling works.
            entry.async_create_background_task(
                hass,
                _background_first_refresh(schedule, "schedule"),
                f"{DOMAIN}_schedule_first_refresh",
            )
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

    async def _async_close_sessions_on_stop(_event: Any) -> None:
        """Close persistent E2E sessions early during HA shutdown (#46).

        The config entry only unloads late in the shutdown sequence, so
        without this the realtime coordinator's background stream receiver and
        keepalive threads stay blocked in ``recvfrom()`` on the open socket and
        HA reports them as "still running after final writes". Closing the
        sessions when ``EVENT_HOMEASSISTANT_STOP`` fires interrupts the socket
        reads promptly. ``async_shutdown`` is idempotent, so the later unload
        call is a harmless no-op.
        """
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if not data:
            return
        for item in data.get("devices") or [data]:
            item["schedule"].async_shutdown()
            await item["realtime"].async_shutdown()

    entry.async_on_unload(
        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, _async_close_sessions_on_stop
        )
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    async_register_services(hass)
    return True


async def _background_first_refresh(
    coordinator: DataUpdateCoordinator[Any],
    name: str,
) -> None:
    """Run a coordinator's first refresh without blocking config entry setup.

    Exceptions are swallowed so a transient E2E/REST failure during startup
    does not fail the whole integration setup.
    """
    _LOGGER.debug(
        "EMALDO_DEBUG[background_first_refresh_start] name=%s "
        "last_update_success=%s",
        name,
        getattr(coordinator, "last_update_success", "n/a"),
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:  # noqa: BLE001
        _LOGGER.debug(
            "%s first refresh failed in background", name, exc_info=True
        )
    _LOGGER.debug(
        "EMALDO_DEBUG[background_first_refresh_done] name=%s "
        "last_update_success=%s data_is_none=%s",
        name,
        getattr(coordinator, "last_update_success", "n/a"),
        coordinator.data is None,
    )


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options update — restart schedule listeners."""
    data = hass.data[DOMAIN].get(entry.entry_id)
    if not data:
        return
    devices = data.get("devices") or [data]
    # A realtime-mode change (stream <-> poll, #41) alters how the realtime
    # coordinator is built, so it only takes effect on a full entry reload.
    from .const import CONF_REALTIME_STREAM_MODE, REALTIME_STREAM_MODE

    desired_stream_mode = bool(
        entry.options.get(CONF_REALTIME_STREAM_MODE, REALTIME_STREAM_MODE)
    )
    for item in devices:
        realtime = item.get("realtime")
        if realtime is not None and getattr(
            realtime, "_stream_mode", REALTIME_STREAM_MODE
        ) != desired_stream_mode:
            await hass.config_entries.async_reload(entry.entry_id)
            return

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
