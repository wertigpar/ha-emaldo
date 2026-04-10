"""Service handlers for Emaldo override commands."""

from __future__ import annotations

import logging
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


def async_unregister_services(hass: HomeAssistant) -> None:
    """Unregister Emaldo services if no entries remain."""
    if not hass.data.get(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SET_SLOT_RANGE)
        hass.services.async_remove(DOMAIN, SERVICE_APPLY_BULK_SCHEDULE)
        hass.services.async_remove(DOMAIN, SERVICE_RESET_TO_INTERNAL)
        hass.services.async_remove(DOMAIN, SERVICE_REFRESH_SCHEDULE)
