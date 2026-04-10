"""Calendar platform for Emaldo integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.calendar import CalendarEntity, CalendarEvent
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .emaldo_lib.const import (
    SLOT_NO_OVERRIDE,
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    decode_slot_action,
)

from .const import DOMAIN
from .schedule_coordinator import EmaldoScheduleCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo calendar from a config entry."""
    schedule_coordinator: EmaldoScheduleCoordinator = hass.data[DOMAIN][
        entry.entry_id
    ]["schedule"]

    async_add_entities([EmaldoBatteryScheduleCalendar(schedule_coordinator)])


def _get_tz(data: dict[str, Any]) -> Any:
    """Get timezone from schedule data."""
    from zoneinfo import ZoneInfo

    tz_name = data.get("timezone", "UTC")
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def _build_events(data: dict[str, Any]) -> list[CalendarEvent]:
    """Build calendar events from schedule + override data."""
    schedule = data.get("schedule") or {}
    overrides_data = data.get("overrides") or {}

    slots = schedule.get("hope_charge_discharges", [])
    prices = schedule.get("market_prices", [])
    solar = schedule.get("forecast_solars", [])
    start_time = schedule.get("start_time", 0)
    gap = schedule.get("gap", 15)

    override_slots = overrides_data.get("slots", []) if overrides_data else []
    high = overrides_data.get("high_marker", DEFAULT_MARKER_HIGH) if overrides_data else DEFAULT_MARKER_HIGH
    low = overrides_data.get("low_marker", DEFAULT_MARKER_LOW) if overrides_data else DEFAULT_MARKER_LOW

    if not slots or not start_time:
        return []

    tz = _get_tz(schedule)
    day_start = datetime.fromtimestamp(start_time, tz)
    slot_duration = timedelta(minutes=gap)

    # Decode each slot, noting if it's overridden
    decoded: list[tuple[datetime, str, bool, float, int]] = []
    for i, value in enumerate(slots):
        slot_time = day_start + slot_duration * i
        is_override = False
        effective_value = value

        # Override only applies to today's 96 slots
        if i < len(override_slots) and i < 96:
            ov = override_slots[i]
            if ov != SLOT_NO_OVERRIDE:
                is_override = True
                effective_value = ov

        # For schedule slots: 100 = charge, negative = discharge, 0 = idle
        if not is_override:
            if value == 100:
                label = "Charge"
            elif value < 0:
                label = "Discharge"
            else:
                label = "Idle"
        else:
            label = decode_slot_action(effective_value, low, high)

        price = prices[i] if i < len(prices) else 0.0
        sol = solar[i] if i < len(solar) else 0

        decoded.append((slot_time, label, is_override, price, sol))

    # Merge consecutive slots with same label + override status
    events: list[CalendarEvent] = []
    if not decoded:
        return events

    merge_start, merge_label, merge_override, price_sum, solar_sum, count = (
        decoded[0][0],
        decoded[0][1],
        decoded[0][2],
        decoded[0][3],
        decoded[0][4],
        1,
    )

    def _flush(end_time: datetime) -> None:
        avg_price = price_sum / count if count else 0
        prefix = "Override: " if merge_override else ""
        summary = f"{prefix}{merge_label}"
        desc_parts = [f"Avg price: {avg_price * 100:.1f} c/kWh"]
        if solar_sum:
            desc_parts.append(f"Solar: {solar_sum} Wh")
        desc_parts.append(f"Slots: {count}")

        events.append(
            CalendarEvent(
                start=merge_start,
                end=end_time,
                summary=summary,
                description=", ".join(desc_parts),
            )
        )

    for i in range(1, len(decoded)):
        slot_time, label, is_override, price, sol = decoded[i]
        if label == merge_label and is_override == merge_override:
            price_sum += price
            solar_sum += sol
            count += 1
        else:
            _flush(slot_time)
            merge_start = slot_time
            merge_label = label
            merge_override = is_override
            price_sum = price
            solar_sum = sol
            count = 1

    # Flush last group
    last_end = decoded[-1][0] + slot_duration
    _flush(last_end)

    return events


class EmaldoBatteryScheduleCalendar(
    CoordinatorEntity[EmaldoScheduleCoordinator], CalendarEntity
):
    """Calendar entity that shows the battery schedule as events."""

    _attr_has_entity_name = True
    _attr_name = "Battery schedule"

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_battery_schedule"

    @property
    def device_info(self) -> DeviceInfo:
        coordinator = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id or coordinator.home_id)},
            name=coordinator.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=coordinator.device_model,
        )

    @property
    def event(self) -> CalendarEvent | None:
        """Return the current active event."""
        if self.coordinator.data is None:
            return None
        events = _build_events(self.coordinator.data)
        now = datetime.now().astimezone()
        for ev in events:
            if ev.start <= now < ev.end:
                return ev
        return None

    async def async_get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return events in the given time range."""
        if self.coordinator.data is None:
            return []
        events = _build_events(self.coordinator.data)
        return [ev for ev in events if ev.end > start_date and ev.start < end_date]
