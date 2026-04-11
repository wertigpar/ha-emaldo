"""Number platform for Emaldo integration.

Exposes the EV "Fixed charge amount" slider as a ``number`` entity. Changes
are written via the 0x31 ``SET_EVCHARGINGMODE_INSTANT`` command with mode
set to ``instantChargeFixed`` (mode 5), which mirrors what the Android app
does when the user drags the slider in the EV panel.

The current value and max bound are both read from the slow coordinator
(wire byte 0x20 response).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmaldoCoordinator
from .emaldo_lib.e2e import (
    EV_MODE_INSTANT_FIXED,
    set_ev_charging_mode_instant,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo number entities from a config entry."""
    power_coordinator: EmaldoCoordinator = hass.data[DOMAIN][entry.entry_id]["power"]

    # Only add the EV slider if the device actually reports EV state.
    ev = (power_coordinator.data or {}).get("ev")
    if ev is None:
        _LOGGER.debug("No EV state reported; skipping EV fixed charge number")
        return

    async_add_entities([EmaldoEvFixedChargeNumber(power_coordinator)])


class EmaldoEvFixedChargeNumber(CoordinatorEntity[EmaldoCoordinator], NumberEntity):
    """Slider entity for EV "Fixed charge amount" (kWh)."""

    _attr_has_entity_name = True
    _attr_name = "EV fixed charge amount"
    _attr_icon = "mdi:ev-station"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        """Initialize the slider."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_ev_fixed_charge_kwh"

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
    def native_max_value(self) -> float:
        """Return the max slider value from the device's ``fixedFull``."""
        ev = (self.coordinator.data or {}).get("ev") or {}
        # Fall back to 100 if the device hasn't reported yet; matches
        # the default slider max observed on PC1-BAK15-HS10.
        return float(ev.get("fixed_full_kwh") or 100)

    @property
    def native_value(self) -> float | None:
        """Return the currently stored fixed charge value."""
        ev = (self.coordinator.data or {}).get("ev") or {}
        val = ev.get("fixed_kwh")
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write a new fixed charge amount to the device.

        Always sends mode=5 (instantChargeFixed) because setting a fixed
        kWh value while the device is in any other mode would silently
        drop the write. If the user is currently in a Smart mode, this
        will also switch the mode — mirroring the behaviour of the
        official Android app when the slider is dragged.
        """
        kwh = int(round(value))

        def _write() -> bool:
            client = self.coordinator._ensure_client()  # noqa: SLF001
            creds = client.e2e_login(
                self.coordinator.home_id,
                self.coordinator._device_id,  # noqa: SLF001
                self.coordinator._model,      # noqa: SLF001
            )
            return set_ev_charging_mode_instant(
                creds, EV_MODE_INSTANT_FIXED, fixed_kwh=kwh,
            )

        ok = await self.hass.async_add_executor_job(_write)
        if not ok:
            _LOGGER.warning("EV fixed charge write (%d kWh) was not acknowledged", kwh)
        # Refresh so the stored state is re-read from the device
        await self.coordinator.async_request_refresh()
