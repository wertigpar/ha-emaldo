"""Service handlers for Emaldo override commands."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .emaldo_lib.const import (
    SLOT_NO_OVERRIDE,
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    encode_override_action,
)
from .emaldo_lib.e2e import (
    EV_MODE_SCHEDULED,
    set_ev_charging_mode_smart,
)
from .emaldo_lib.exceptions import EmaldoAuthError

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

# Valid action names for set_slot_range
VALID_ACTIONS = [
    "charge-low",
    "charge-high",
    "charge-100",
    "idle",
    "discharge-low",
    "discharge-high",
    "clear",
]

SERVICE_SET_SLOT_RANGE = "set_slot_range"
SERVICE_APPLY_BULK_SCHEDULE = "apply_bulk_schedule"
SERVICE_RESET_TO_INTERNAL = "reset_to_internal"
SERVICE_REFRESH_SCHEDULE = "refresh_schedule"
SERVICE_SET_EV_SCHEDULE = "set_ev_schedule"
SERVICE_BACKFILL_SOLAR = "backfill_solar"
SERVICE_SET_BATTERY_RANGE = "set_battery_range"

SCHEMA_SET_BATTERY_RANGE = vol.Schema(
    {
        vol.Required("smart_pct"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Required("emergency_pct"): vol.All(int, vol.Range(min=0, max=100)),
        vol.Optional("enable", default=True): cv.boolean,
    }
)

SCHEMA_SET_SLOT_RANGE = vol.Schema(
    {
        vol.Required("start_time"): cv.string,
        vol.Required("end_time"): cv.string,
        vol.Required("action"): vol.In(VALID_ACTIONS),
        vol.Optional("high_marker", default=DEFAULT_MARKER_HIGH): vol.All(
            int, vol.Range(min=1, max=100)
        ),
        vol.Optional("low_marker", default=DEFAULT_MARKER_LOW): vol.All(
            int, vol.Range(min=1, max=100)
        ),
    }
)

SCHEMA_APPLY_BULK_SCHEDULE = vol.Schema(
    {
        vol.Required("slots"): vol.All(
            [vol.All(int, vol.Range(min=0, max=255))],
            vol.Length(min=96, max=96),
        ),
        vol.Optional("high_marker", default=DEFAULT_MARKER_HIGH): vol.All(
            int, vol.Range(min=1, max=100)
        ),
        vol.Optional("low_marker", default=DEFAULT_MARKER_LOW): vol.All(
            int, vol.Range(min=1, max=100)
        ),
    }
)

SCHEMA_SET_EV_SCHEDULE = vol.Schema(
    {
        # Lists of hour integers 0-23 when EV charging is allowed.
        # Empty list or omitted key means "no hours selected".
        vol.Optional("weekdays", default=list): vol.All(
            [vol.All(int, vol.Range(min=0, max=23))],
        ),
        vol.Optional("weekend", default=list): vol.All(
            [vol.All(int, vol.Range(min=0, max=23))],
        ),
        vol.Optional("sync", default=False): cv.boolean,
    }
)

SCHEMA_BACKFILL_SOLAR = vol.Schema(
    {
        vol.Optional("days", default=30): vol.All(
            int, vol.Range(min=1, max=90)
        ),
    }
)

SCHEMA_RESET_TO_INTERNAL = vol.Schema(
    {
        vol.Optional("start_time"): cv.string,
        vol.Optional("end_time"): cv.string,
        vol.Optional("all", default=False): cv.boolean,
        vol.Optional("high_marker", default=DEFAULT_MARKER_HIGH): vol.All(
            int, vol.Range(min=1, max=100)
        ),
        vol.Optional("low_marker", default=DEFAULT_MARKER_LOW): vol.All(
            int, vol.Range(min=1, max=100)
        ),
    }
)


def _time_to_slot(time_val) -> int:
    """Convert time to slot index (0-95). Accepts HH:MM string or datetime.time."""
    import datetime
    if isinstance(time_val, datetime.time):
        h, m = time_val.hour, time_val.minute
    else:
        time_str = str(time_val).strip()
        parts = time_str.split(":")
        if len(parts) < 2:
            raise vol.Invalid(f"Invalid time format: {time_str!r}, expected HH:MM")
        h, m = int(parts[0]), int(parts[1])
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise vol.Invalid(f"Invalid time: {h}:{m}")
    return (h * 60 + m) // 15


def _get_coordinator_and_client(hass: HomeAssistant):
    """Get the first available schedule coordinator and its client."""
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise ValueError("No Emaldo integration configured")
    entry_data = next(iter(entries.values()))
    schedule_coord = entry_data["schedule"]
    try:
        client = schedule_coord._ensure_client()
    except Exception:
        # Session may be stale — force re-login
        schedule_coord._client = None
        client = schedule_coord._ensure_client()
    return schedule_coord, client


async def async_handle_set_slot_range(hass: HomeAssistant, call: ServiceCall) -> None:
    """Handle the set_slot_range service call."""
    start_slot = _time_to_slot(call.data["start_time"])
    end_slot = _time_to_slot(call.data["end_time"])
    action = call.data["action"]
    high = call.data["high_marker"]
    low = call.data["low_marker"]

    slot_value = encode_override_action(action, low, high)

    # Read current overrides, then patch the range
    def _do_override():
        for attempt in range(2):
            try:
                coord, client = _get_coordinator_and_client(hass)
                hid, did, model = coord.home_id, coord._device_id, coord._model

                current = client.get_overrides(hid, did, model)
                if current:
                    slots = list(current["slots"])
                else:
                    slots = [SLOT_NO_OVERRIDE] * 96

                for i in range(start_slot, min(end_slot, 96)):
                    slots[i] = slot_value

                ok = client.set_override(
                    hid, did, model, bytes(slots),
                    high_marker=high, low_marker=low,
                )
                if ok:
                    _LOGGER.info(
                        "Override set: slots %d-%d = %s", start_slot, end_slot, action
                    )
                else:
                    _LOGGER.error("Failed to set override")
                return ok
            except EmaldoAuthError:
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    coord._client = None
                else:
                    raise

    await hass.async_add_executor_job(_do_override)

    # Refresh schedule data
    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        await entry_data["schedule"].async_request_refresh()


async def async_handle_apply_bulk_schedule(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the apply_bulk_schedule service call."""
    slot_values = call.data["slots"]
    high = call.data["high_marker"]
    low = call.data["low_marker"]

    def _do_bulk():
        for attempt in range(2):
            try:
                coord, client = _get_coordinator_and_client(hass)
                hid, did, model = coord.home_id, coord._device_id, coord._model

                ok = client.set_override(
                    hid, did, model, bytes(slot_values),
                    high_marker=high, low_marker=low,
                )
                if ok:
                    _LOGGER.info(
                        "Bulk override applied (%d slots)", len(slot_values)
                    )
                else:
                    _LOGGER.error("Failed to apply bulk override")
                return ok
            except EmaldoAuthError:
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    coord._client = None
                else:
                    raise

    await hass.async_add_executor_job(_do_bulk)

    # Refresh schedule data after applying overrides
    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        coord = entry_data["schedule"]
        await coord.async_request_refresh()


async def async_handle_reset_to_internal(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the reset_to_internal service call."""
    reset_all = call.data.get("all", False)
    start_time = call.data.get("start_time")
    end_time = call.data.get("end_time")
    # If no time range given, reset all
    if not start_time and not end_time:
        reset_all = True
    high = call.data["high_marker"]
    low = call.data["low_marker"]

    def _do_reset():
        for attempt in range(2):
            try:
                coord, client = _get_coordinator_and_client(hass)
                hid, did, model = coord.home_id, coord._device_id, coord._model

                if reset_all:
                    ok = client.reset_overrides(
                        hid, did, model, high_marker=high, low_marker=low
                    )
                    if ok:
                        _LOGGER.info("All overrides reset to internal")
                    return ok

                # Partial reset — read current, clear the range
                start_slot = _time_to_slot(start_time)
                end_slot = _time_to_slot(end_time)

                current = client.get_overrides(hid, did, model)
                if current:
                    slots = list(current["slots"])
                else:
                    slots = [SLOT_NO_OVERRIDE] * 96

                for i in range(start_slot, min(end_slot, 96)):
                    slots[i] = SLOT_NO_OVERRIDE

                ok = client.set_override(
                    hid, did, model, bytes(slots),
                    high_marker=high, low_marker=low,
                )
                if ok:
                    _LOGGER.info(
                        "Overrides reset: slots %d-%d", start_slot, end_slot
                    )
                return ok
            except EmaldoAuthError:
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    coord._client = None
                else:
                    raise

    await hass.async_add_executor_job(_do_reset)

    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        coord = entry_data["schedule"]
        await coord.async_request_refresh()


async def async_handle_refresh_schedule(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the refresh_schedule service call."""
    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        coord = entry_data["schedule"]
        _LOGGER.info("Manual schedule refresh requested")
        await coord.async_request_refresh()


async def async_handle_set_ev_schedule(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the set_ev_schedule service call.

    Sets the EV charger's weekday and weekend hour schedule (when
    charging is allowed). This switches the device into
    ``EV_MODE_SCHEDULED`` and writes the 24h×2 hour bitmaps via the
    ``SET_EV_CHARGING_MODE`` command (wire 0x22, 9-byte payload).

    Input lists are hours 0-23 — e.g. ``[6, 7, 22, 23]`` enables
    charging 06:00-08:00 and 22:00-00:00. Empty lists disable all hours
    for that day type (which effectively means "never charge on that
    day"; prefer picking a different mode instead).
    """
    weekday_hours = call.data.get("weekdays", [])
    weekend_hours = call.data.get("weekend", [])
    sync = call.data.get("sync", False)

    weekdays = [0] * 24
    for h in weekday_hours:
        weekdays[h] = 1
    weekend = [0] * 24
    for h in weekend_hours:
        weekend[h] = 1

    def _do_set():
        entries = hass.data.get(DOMAIN, {})
        if not entries:
            raise ValueError("No Emaldo integration configured")
        entry_data = next(iter(entries.values()))
        power_coord = entry_data["power"]
        for attempt in range(2):
            try:
                client = power_coord._ensure_client()  # noqa: SLF001
                creds = client.e2e_login(
                    power_coord.home_id,
                    power_coord._device_id,  # noqa: SLF001
                    power_coord._model,      # noqa: SLF001
                )
                return set_ev_charging_mode_smart(
                    creds, EV_MODE_SCHEDULED,
                    weekdays=weekdays, weekend=weekend, sync=sync,
                )
            except EmaldoAuthError:
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    power_coord._client = None  # noqa: SLF001
                else:
                    raise

    ok = await hass.async_add_executor_job(_do_set)
    if ok:
        _LOGGER.info(
            "EV schedule applied: weekdays=%s, weekend=%s, sync=%s",
            weekday_hours, weekend_hours, sync,
        )
    else:
        _LOGGER.warning("EV schedule write was not acknowledged")

    # Refresh the slow coordinator so the new schedule is reflected in
    # the select / number / sensor entities.
    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        await entry_data["power"].async_request_refresh()


async def async_handle_backfill_solar(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Backfill solar energy statistics from Emaldo API history.

    Fetches up to 90 days of 5-min MPPT data using negative offsets,
    aggregates to hourly statistics, and imports them via
    async_add_external_statistics so they appear in the Energy Dashboard.

    To avoid double-counting, the backfill finds the earliest date the
    live solar_energy_today sensor has statistics for and only imports
    days before that. Any previous backfill data is cleared first.
    """
    from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
    )
    from homeassistant.components.recorder import get_instance

    days = call.data.get("days", 30)
    statistic_id = f"{DOMAIN}:solar_energy_backfill"

    # Find cutoff: earliest date the live solar_energy_today sensor
    # has state history, so we don't overlap with real data.
    cutoff_date = None
    try:
        for state in hass.states.async_all("sensor"):
            if "solar_energy_today" in state.entity_id:
                # Check entity registry for when it was added,
                # or use the entity's first recorded state.
                from homeassistant.components.recorder.history import (
                    get_significant_states,
                )
                now = datetime.now(timezone.utc)
                start = now - timedelta(days=days + 1)
                history = await get_instance(hass).async_add_executor_job(
                    get_significant_states,
                    hass, start, now, [state.entity_id],
                )
                if history.get(state.entity_id):
                    first_ts = history[state.entity_id][0].last_changed
                    cutoff_date = first_ts.date()
                    _LOGGER.info(
                        "Backfill solar: live sensor %s first seen %s, "
                        "will only backfill before that date",
                        state.entity_id, cutoff_date,
                    )
                break
    except Exception:
        _LOGGER.debug("Backfill: could not determine live sensor cutoff", exc_info=True)

    # Clear previous backfill data
    try:
        recorder_instance = get_instance(hass)
        # Try the available clear API
        try:
            from homeassistant.components.recorder.statistics import (
                clear_statistics,
            )
            await recorder_instance.async_add_executor_job(
                clear_statistics, recorder_instance, [statistic_id]
            )
        except ImportError:
            from homeassistant.components.recorder.statistics import (
                async_clear_statistics,
            )
            async_clear_statistics(recorder_instance, [statistic_id])
        _LOGGER.info("Backfill solar: cleared previous backfill data")
    except Exception:
        _LOGGER.debug(
            "Backfill: no previous data to clear or clear not available",
            exc_info=True,
        )

    # Get client
    entries = hass.data.get(DOMAIN, {})
    if not entries:
        raise ValueError("No Emaldo integration configured")
    entry_data = next(iter(entries.values()))
    power_coord = entry_data["power"]

    today = datetime.now().date()

    def _fetch_history():
        client = power_coord._ensure_client()  # noqa: SLF001
        hid = power_coord.home_id
        did = power_coord._device_id  # noqa: SLF001
        model = power_coord._model  # noqa: SLF001

        all_days = {}
        for day_offset in range(-1, -days, -1):  # skip today (offset=0)
            day_date = today + timedelta(days=day_offset)
            # Stop if we'd overlap with live sensor data
            if cutoff_date and day_date >= cutoff_date:
                _LOGGER.debug(
                    "Backfill: skipping %s (live sensor has data)", day_date
                )
                continue
            try:
                result = client.get_solar(hid, did, model, offset=day_offset)
                data = result.get("data", [])
                start_time = result.get("start_time", 0)
                tz_name = result.get("timezone", "Europe/Helsinki")
                if data:
                    all_days[day_offset] = {
                        "data": data,
                        "start_time": start_time,
                        "timezone": tz_name,
                    }
                    total_w = sum(sum(e[1:]) for e in data)
                    kwh = total_w * 5 / 60 / 1000
                    _LOGGER.info(
                        "Backfill solar: %s (offset=%d), %d pts, %.1f kWh",
                        day_date, day_offset, len(data), kwh,
                    )
            except Exception:
                _LOGGER.exception("Backfill: failed to fetch offset=%d", day_offset)
        return all_days

    _LOGGER.info("Backfill solar: fetching up to %d days of history...", days)
    all_days = await hass.async_add_executor_job(_fetch_history)

    if not all_days:
        _LOGGER.warning("Backfill solar: no data to import (all days overlap with live sensor)")
        return

    # Aggregate to hourly statistics
    import zoneinfo

    hourly_stats: list[StatisticData] = []
    cumulative_kwh = 0.0

    for day_offset in sorted(all_days.keys()):
        day_data = all_days[day_offset]
        day_entries = day_data["data"]
        start_time = day_data["start_time"]
        tz_name = day_data["timezone"]

        # Group by hour
        hourly_wh: dict[int, float] = {}
        for entry in day_entries:
            minute_offset = entry[0]
            total_w = sum(entry[1:])
            wh = total_w * 5 / 60  # 5-min interval → Wh
            hour = minute_offset // 60
            hourly_wh[hour] = hourly_wh.get(hour, 0) + wh

        day_start = datetime.fromtimestamp(start_time, tz=timezone.utc)
        for hour in sorted(hourly_wh.keys()):
            if hour > 23:
                continue
            kwh = hourly_wh[hour] / 1000
            cumulative_kwh += kwh
            hour_start = day_start.replace(
                hour=hour, minute=0, second=0, microsecond=0
            )
            hourly_stats.append(
                StatisticData(
                    start=hour_start,
                    state=cumulative_kwh,
                    sum=cumulative_kwh,
                )
            )

    if not hourly_stats:
        _LOGGER.warning("Backfill solar: no hourly stats to import")
        return

    metadata = StatisticMetaData(
        has_mean=False,
        has_sum=True,
        name="Solar energy (backfill)",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement="kWh",
    )

    first_date = (today + timedelta(days=min(all_days.keys()))).isoformat()
    last_date = (today + timedelta(days=max(all_days.keys()))).isoformat()
    _LOGGER.info(
        "Backfill solar: importing %d hourly stats, %s to %s (%.1f kWh)",
        len(hourly_stats), first_date, last_date, cumulative_kwh,
    )
    async_add_external_statistics(hass, metadata, hourly_stats)
    _LOGGER.info("Backfill solar: done")


async def async_handle_set_battery_range(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    """Handle the set_battery_range service call.

    Writes the AI Battery Range — the SoC band the AI must operate within.
    Mirrors the app's "Save Battery Range" save: clears all 96 per-15-min
    slot overrides to 0x80 and sets byte 2 = 1 ("override mode active") when
    ``enable`` is True.
    """
    smart = call.data["smart_pct"]
    emergency = call.data["emergency_pct"]
    enable = call.data.get("enable", True)
    if smart < emergency:
        raise vol.Invalid("smart_pct must be >= emergency_pct")

    def _do_write():
        for attempt in range(2):
            try:
                coord, client = _get_coordinator_and_client(hass)
                hid, did, model = coord.home_id, coord._device_id, coord._model
                ok = client.set_battery_range(
                    hid, did, model,
                    smart_pct=smart, emergency_pct=emergency, enable=enable,
                )
                if ok:
                    _LOGGER.info(
                        "Battery Range set: %d-%d%% (override=%s)",
                        emergency, smart, enable,
                    )
                return ok
            except EmaldoAuthError:
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    coord._client = None
                else:
                    raise

    await hass.async_add_executor_job(_do_write)

    entries = hass.data.get(DOMAIN, {})
    for entry_data in entries.values():
        coord = entry_data["schedule"]
        await coord.async_request_refresh()


def async_register_services(hass: HomeAssistant) -> None:
    """Register Emaldo services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_SLOT_RANGE):
        return  # Already registered

    async def handle_set_slot_range(call: ServiceCall) -> None:
        await async_handle_set_slot_range(hass, call)

    async def handle_apply_bulk_schedule(call: ServiceCall) -> None:
        await async_handle_apply_bulk_schedule(hass, call)

    async def handle_reset_to_internal(call: ServiceCall) -> None:
        await async_handle_reset_to_internal(hass, call)

    async def handle_refresh_schedule(call: ServiceCall) -> None:
        await async_handle_refresh_schedule(hass, call)

    async def handle_set_ev_schedule(call: ServiceCall) -> None:
        await async_handle_set_ev_schedule(hass, call)

    async def handle_backfill_solar(call: ServiceCall) -> None:
        await async_handle_backfill_solar(hass, call)

    async def handle_set_battery_range(call: ServiceCall) -> None:
        await async_handle_set_battery_range(hass, call)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_SLOT_RANGE,
        handle_set_slot_range,
        schema=SCHEMA_SET_SLOT_RANGE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_APPLY_BULK_SCHEDULE,
        handle_apply_bulk_schedule,
        schema=SCHEMA_APPLY_BULK_SCHEDULE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_TO_INTERNAL,
        handle_reset_to_internal,
        schema=SCHEMA_RESET_TO_INTERNAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_SCHEDULE,
        handle_refresh_schedule,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EV_SCHEDULE,
        handle_set_ev_schedule,
        schema=SCHEMA_SET_EV_SCHEDULE,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_BACKFILL_SOLAR,
        handle_backfill_solar,
        schema=SCHEMA_BACKFILL_SOLAR,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_BATTERY_RANGE,
        handle_set_battery_range,
        schema=SCHEMA_SET_BATTERY_RANGE,
    )


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister Emaldo services if no entries remain."""
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SET_SLOT_RANGE)
        hass.services.async_remove(DOMAIN, SERVICE_APPLY_BULK_SCHEDULE)
        hass.services.async_remove(DOMAIN, SERVICE_RESET_TO_INTERNAL)
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH_SCHEDULE)
        hass.services.async_remove(DOMAIN, SERVICE_SET_EV_SCHEDULE)
        hass.services.async_remove(DOMAIN, SERVICE_BACKFILL_SOLAR)
        hass.services.async_remove(DOMAIN, SERVICE_SET_BATTERY_RANGE)
