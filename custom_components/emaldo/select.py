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

from .const import DOMAIN
from .coordinator import EmaldoCoordinator
from .emaldo_lib.e2e import (
    EV_MODE_LOWEST_PRICE,
    EV_MODE_SCHEDULED,
    EV_MODE_INSTANT_FULL,
    EV_MODE_INSTANT_FIXED,
    set_ev_charging_mode_instant,
    set_ev_charging_mode_smart,
)

_LOGGER = logging.getLogger(__name__)


# EV select options, in the order they appear in the Emaldo app.
# ``Solar Only`` (mode 2) is defined in the APK enum but is not exposed
# in the current Android app UI on PC1-BAK15-HS10, so we omit it here
# as well.
EV_OPTION_LOWEST_PRICE = "Lowest Price"
EV_OPTION_SCHEDULED = "Scheduled"
EV_OPTION_INSTANT_FULL = "Until Fully Charged"
EV_OPTION_INSTANT_FIXED = "Fixed Amount"

EV_OPTIONS = [
    EV_OPTION_LOWEST_PRICE,
    EV_OPTION_SCHEDULED,
    EV_OPTION_INSTANT_FULL,
    EV_OPTION_INSTANT_FIXED,
]

# Map mode integer (as returned by ``read_ev_charging_mode``) back to
# the HA select option label. Mode 2 (Solar Only) maps to None so the
# select reports "unknown" if the device happens to be in that state.
_MODE_TO_OPTION: dict[int, str | None] = {
    EV_MODE_LOWEST_PRICE: EV_OPTION_LOWEST_PRICE,
    EV_MODE_SCHEDULED: EV_OPTION_SCHEDULED,
    EV_MODE_INSTANT_FULL: EV_OPTION_INSTANT_FULL,
    EV_MODE_INSTANT_FIXED: EV_OPTION_INSTANT_FIXED,
    2: None,  # Solar Only — not exposed in UI
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo select entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    power_coordinator: EmaldoCoordinator = data["power"]

    entities: list[SelectEntity] = []

    # Only add the EV mode select if the device actually reports EV
    # state (i.e. hardware with an EV charger attached).
    if (power_coordinator.data or {}).get("ev") is not None:
        entities.append(EmaldoEvChargeModeSelect(power_coordinator))
    else:
        _LOGGER.debug("No EV state reported; skipping EV mode select")

    async_add_entities(entities)


# ``EmaldoControlPrioritySelect`` — disabled.
#
# Inherited from wertigpar's upstream, this was a stub "virtual" select
# entity exposing "internal" / "override" priority options. The
# ``async_select_option`` handler only stored the chosen value in memory
# and didn't propagate anywhere — no service, coordinator, or other file
# in the integration ever read it. Keeping it visible in the UI was
# confusing because it looked functional, so we disable it until/unless
# someone wires up real behavior behind it.
#
# class EmaldoControlPrioritySelect(SelectEntity):
#     _attr_has_entity_name = True
#     _attr_name = "Control priority"
#     _attr_options = [CONTROL_PRIORITY_INTERNAL, CONTROL_PRIORITY_OVERRIDE]
#     _attr_current_option = CONTROL_PRIORITY_INTERNAL
#
#     def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
#         self._coordinator = coordinator
#         self._attr_unique_id = f"{coordinator.home_id}_control_priority"
#
#     @property
#     def device_info(self) -> DeviceInfo:
#         c = self._coordinator
#         return DeviceInfo(
#             identifiers={(DOMAIN, c.device_id or c.home_id)},
#             name=c.device_name or "Emaldo Battery",
#             manufacturer="Emaldo",
#             model=c.device_model,
#         )
#
#     async def async_select_option(self, option: str) -> None:
#         self._attr_current_option = option
#         self.async_write_ha_state()


class EmaldoEvChargeModeSelect(CoordinatorEntity[EmaldoCoordinator], SelectEntity):
    """Dropdown for EV charging mode.

    Read: current mode is taken from the slow coordinator's ``ev`` dict
    (wire byte 0x20 response), mapped through :data:`_MODE_TO_OPTION`.

    Write: each option sends the matching setter over E2E:
      - "Lowest Price"        → ``set_ev_charging_mode_smart(1)``
      - "Scheduled"           → ``set_ev_charging_mode_smart(3, ...)``
        (Reuses the stored weekday/weekend bitmaps from the slow
        coordinator so switching to Scheduled doesn't wipe the existing
        schedule. Edit the schedule via the ``emaldo.set_ev_schedule``
        service.)
      - "Until Fully Charged" → ``set_ev_charging_mode_instant(4)``
      - "Fixed Amount"        → ``set_ev_charging_mode_instant(5, fixed_kwh=...)``
        (Uses the current ``fixed_kwh`` value from the device; to change
        the kWh amount, use the ``number`` slider entity.)
    """

    _attr_has_entity_name = True
    _attr_name = "EV charge mode"
    _attr_icon = "mdi:ev-station"
    _attr_options = EV_OPTIONS

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        """Initialize the EV mode select."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_ev_charge_mode"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info linking to the main Emaldo device."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def current_option(self) -> str | None:
        """Return the current mode from the coordinator state."""
        ev = (self.coordinator.data or {}).get("ev") or {}
        return _MODE_TO_OPTION.get(ev.get("mode"))

    async def async_select_option(self, option: str) -> None:
        """Write the new mode to the device."""
        _LOGGER.debug("EV select: async_select_option called with %r", option)
        ev = (self.coordinator.data or {}).get("ev") or {}

        def _write() -> bool:
            client = self.coordinator._ensure_client()  # noqa: SLF001
            creds = client.e2e_login(
                self.coordinator.home_id,
                self.coordinator._device_id,  # noqa: SLF001
                self.coordinator._model,      # noqa: SLF001
            )
            if option == EV_OPTION_LOWEST_PRICE:
                return set_ev_charging_mode_smart(creds, EV_MODE_LOWEST_PRICE)
            if option == EV_OPTION_SCHEDULED:
                # Reuse stored hour bitmaps so switching modes doesn't
                # wipe the schedule.
                return set_ev_charging_mode_smart(
                    creds, EV_MODE_SCHEDULED,
                    weekdays=ev.get("weekdays"),
                    weekend=ev.get("weekend"),
                    sync=bool(ev.get("sync")),
                )
            if option == EV_OPTION_INSTANT_FULL:
                return set_ev_charging_mode_instant(creds, EV_MODE_INSTANT_FULL)
            if option == EV_OPTION_INSTANT_FIXED:
                # Reuse the currently stored fixed value; users adjust it
                # via the EmaldoEvFixedChargeNumber slider.
                return set_ev_charging_mode_instant(
                    creds, EV_MODE_INSTANT_FIXED,
                    fixed_kwh=int(ev.get("fixed_kwh") or 0),
                )
            _LOGGER.warning("Unknown EV option: %s", option)
            return False

        ok = await self.hass.async_add_executor_job(_write)
        if not ok:
            _LOGGER.warning("EV mode write (%s) was not acknowledged", option)
        await self.coordinator.async_request_refresh()
