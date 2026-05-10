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
from .coordinator import EmaldoRealtimeCoordinator
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

    async_add_entities(
        [
            EmaldoThirdPartyPVSwitch(realtime_coordinator),
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