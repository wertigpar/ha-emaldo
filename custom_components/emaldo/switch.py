"""Switch platform for Emaldo integration.

Exposes Third-party PV toggle and AI Battery Range override toggle.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmaldoCoordinator, EmaldoRealtimeCoordinator
from .schedule_coordinator import EmaldoScheduleCoordinator
from .emaldo_lib.exceptions import EmaldoAuthError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo switch entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    realtime_coordinator: EmaldoRealtimeCoordinator = data["realtime"]
    schedule_coordinator: EmaldoScheduleCoordinator = data["schedule"]
    power_coordinator: EmaldoCoordinator = data["power"]

    async_add_entities(
        [
            EmaldoThirdPartyPVSwitch(realtime_coordinator),
            EmaldoSellBackToGridSwitch(realtime_coordinator),
            EmaldoSellLimitSwitch(realtime_coordinator),
            EmaldoEmergencyChargeSwitch(power_coordinator),
            EmaldoBatteryRangeOverrideSwitch(schedule_coordinator),
        ]
    )


class EmaldoThirdPartyPVSwitch(
    CoordinatorEntity[EmaldoRealtimeCoordinator], SwitchEntity
):
    """Switch entity for enabling/disabling Third-Party PV (3rd-party solar)."""

    _attr_has_entity_name = True
    _attr_name = "Third-party PV"
    _attr_icon = "mdi:solar-panel"

    def __init__(self, coordinator: EmaldoRealtimeCoordinator) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_thirdparty_pv_on"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when third-party PV is enabled."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("thirdparty_pv_on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable third-party PV."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_thirdparty_pv, True  # noqa: SLF001
        )
        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable third-party PV."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_thirdparty_pv, False  # noqa: SLF001
        )
        await asyncio.sleep(1.5)
        await self.coordinator.async_request_refresh()


class EmaldoSellBackToGridSwitch(
    CoordinatorEntity[EmaldoRealtimeCoordinator], SwitchEntity
):
    """Switch entity for enabling/disabling grid export (sell back to grid).

    ON  = sell-back allowed   (selling protection OFF, type 0x5E payload on=0x00)
    OFF = sell-back blocked   (selling protection ON,  type 0x5E payload on=0x01)

    State is read from the device via type 0x5F (get_sellingprotection) and
    stored in coordinator.data["sell_back_to_grid_on"].
    """

    _attr_has_entity_name = True
    _attr_name = "Sell Back to Grid"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(self, coordinator: EmaldoRealtimeCoordinator) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_sell_back_to_grid"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when selling back to the grid is allowed."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("sell_back_to_grid_on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow grid export (enable sell-back to grid)."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_sell_back_to_grid, True  # noqa: SLF001
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["sell_back_to_grid_on"] = True
            self.coordinator.async_set_updated_data(updated)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Block grid export (disable sell-back to grid)."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_sell_back_to_grid, False  # noqa: SLF001
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["sell_back_to_grid_on"] = False
            self.coordinator.async_set_updated_data(updated)


class EmaldoSellLimitSwitch(
    CoordinatorEntity[EmaldoRealtimeCoordinator], SwitchEntity
):
    """Switch entity for enabling/disabling the daily sell limit (selling protection).

    ON  = sell limit active   (set_sellingprotection on=1, type 0x5E)
    OFF = no daily sell limit (set_sellingprotection on=0, type 0x5E)

    The limit value is controlled by :class:`EmaldoSellLimitThreshold` (number
    entity).  Both entities preserve each other's last-known value when written.
    """

    _attr_has_entity_name = True
    _attr_name = "Sell Limit"
    _attr_icon = "mdi:transmission-tower-off"

    def __init__(self, coordinator: EmaldoRealtimeCoordinator) -> None:
        """Initialise the switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_sell_limit"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True when the daily sell limit is active."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("sell_limit_on")

    def _current_threshold(self) -> int:
        """Return the last-known threshold so writes preserve it."""
        if self.coordinator.data is None:
            return 0
        return int(self.coordinator.data.get("sell_limit_threshold") or 0)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate sell limit with the last-known threshold."""
        threshold = self._current_threshold()
        await self.hass.async_add_executor_job(
            self.coordinator._write_sell_limit, True, threshold  # noqa: SLF001
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["sell_limit_on"] = True
            self.coordinator.async_set_updated_data(updated)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Deactivate sell limit (keep threshold for future re-activation)."""
        threshold = self._current_threshold()
        await self.hass.async_add_executor_job(
            self.coordinator._write_sell_limit, False, threshold  # noqa: SLF001
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["sell_limit_on"] = False
            self.coordinator.async_set_updated_data(updated)


class EmaldoBatteryRangeOverrideSwitch(
    CoordinatorEntity[EmaldoScheduleCoordinator], SwitchEntity
):
    """ON means AI must stay inside [emergency, smart] marker percentages."""

    _attr_has_entity_name = True
    _attr_name = "AI Battery Range override"
    _attr_icon = "mdi:battery-lock"

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        """Initialize the override switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_battery_range_override"

    @property
    def device_info(self) -> DeviceInfo:
        """Link entity to the main Emaldo device."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return current override-active state from coordinator snapshot."""
        ov = (self.coordinator.data or {}).get("overrides") or {}
        val = ov.get("battery_range_override")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable AI battery range override mode."""
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable AI battery range override mode."""
        await self._write(False)

    async def _write(self, enable: bool) -> None:
        ov = (self.coordinator.data or {}).get("overrides") or {}
        smart = ov.get("high_marker", 50)
        emergency = ov.get("low_marker", 10)

        def _do_write() -> bool:
            for attempt in range(2):
                try:
                    client = self.coordinator._ensure_client()  # noqa: SLF001
                    return client.set_battery_range(
                        self.coordinator.home_id,
                        self.coordinator._device_id,  # noqa: SLF001
                        self.coordinator._model,  # noqa: SLF001
                        smart_pct=smart,
                        emergency_pct=emergency,
                        enable=enable,
                    )
                except EmaldoAuthError:
                    if attempt == 0:
                        self.coordinator._client = None  # noqa: SLF001
                    else:
                        raise
            return False

        ok = await self.hass.async_add_executor_job(_do_write)
        if not ok:
            _LOGGER.warning(
                "Battery Range override toggle was not acknowledged (target=%s)",
                enable,
            )
        await self.coordinator.async_request_refresh()


class EmaldoEmergencyChargeSwitch(CoordinatorEntity[EmaldoCoordinator], SwitchEntity):
    """Switch entity for starting and cancelling emergency charge.

    Turning ON starts a charge session using the duration configured in the
    companion :class:`~emaldo.number.EmaldoEmergencyChargeHours` entity.
    Turning OFF cancels any active session immediately.

    State is tracked optimistically — there is no dedicated device read-back
    for this command (it shares E2E type 0x01 with the manual-sell command).
    """

    _attr_has_entity_name = True
    _attr_name = "Emergency charge"
    _attr_icon = "mdi:battery-charging-high"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        """Initialize the emergency charge switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_emergency_charge"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return True while an emergency charge session is active."""
        if self.coordinator.data is None:
            return None
        return bool(self.coordinator.data.get("emergency_charge_active", False))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Start emergency charge using the configured start/end window."""
        import time as _time
        now = int(_time.time())
        data = self.coordinator.data or {}
        start_dt = data.get("emergency_charge_start_dt")
        end_dt = data.get("emergency_charge_end_dt")
        start_unix = int(start_dt.timestamp()) if start_dt is not None else now
        end_unix = int(end_dt.timestamp()) if end_dt is not None else start_unix + 3600
        await self.hass.async_add_executor_job(
            self.coordinator._write_emergency_charge_on,  # noqa: SLF001
            start_unix, end_unix,
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["emergency_charge_active"] = True
            self.coordinator.async_set_updated_data(updated)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Cancel the active emergency charge session."""
        await self.hass.async_add_executor_job(
            self.coordinator._write_emergency_charge_off  # noqa: SLF001
        )
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated["emergency_charge_active"] = False
            self.coordinator.async_set_updated_data(updated)