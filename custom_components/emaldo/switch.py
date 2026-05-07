"""Switch platform for Emaldo integration.

Exposes the AI Battery Range "override active" toggle as a ``switch``
entity. When ON, the AI is constrained to operate inside
``[emergency_pct, smart_pct]`` (the values surfaced by the
``EmaldoBatteryRangeMarker`` ``number`` entities). When OFF, AI picks
its own band.

Writes go via :meth:`EmaldoClient.set_battery_range`, which sends opcode
0x1AA0 with all 96 per-slot overrides cleared to 0x80 — same wire write as
the app's "Save Battery Range" button.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
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
    schedule_coordinator: EmaldoScheduleCoordinator = data["schedule"]
    async_add_entities([EmaldoBatteryRangeOverrideSwitch(schedule_coordinator)])


class EmaldoBatteryRangeOverrideSwitch(
    CoordinatorEntity[EmaldoScheduleCoordinator], SwitchEntity
):
    """ON = AI must operate inside [emergency, smart]; OFF = AI picks the band."""

    _attr_has_entity_name = True
    _attr_name = "AI Battery Range override"
    _attr_icon = "mdi:battery-lock"

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        """Initialize the override switch."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_battery_range_override"

    @property
    def device_info(self) -> DeviceInfo:
        """Link to the main Emaldo device."""
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def is_on(self) -> bool | None:
        """Return whether override mode is active per the last read."""
        ov = (self.coordinator.data or {}).get("overrides") or {}
        val = ov.get("battery_range_override")
        return bool(val) if val is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate override mode using the currently-stored markers."""
        await self._write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Revert to AI-chosen Battery Range; markers are persisted."""
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
                        self.coordinator._model,      # noqa: SLF001
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
