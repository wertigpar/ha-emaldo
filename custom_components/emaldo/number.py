"""Number platform for Emaldo integration.

Exposes EV fixed charge amount and AI Battery Range marker controls.
"""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, EV_UNSUPPORTED_MODELS
from .coordinator import EmaldoCoordinator
from .schedule_coordinator import EmaldoScheduleCoordinator
from .emaldo_lib.e2e import EV_MODE_INSTANT_FIXED
from .emaldo_lib.exceptions import EmaldoAuthError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo number entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    power_coordinator: EmaldoCoordinator = data["power"]
    schedule_coordinator: EmaldoScheduleCoordinator = data["schedule"]

    entities: list[NumberEntity] = []

    model = power_coordinator.device_model or ""
    if model not in EV_UNSUPPORTED_MODELS:
        entities.append(EmaldoEVFixedChargeAmount(power_coordinator))

    # AI Battery Range markers — always present; override write is destructive
    # (clears all per-15-min slot overrides), so the slider always sets the
    # battery_range_override flag to whatever the switch entity reads.
    entities.append(EmaldoBatteryRangeMarker(schedule_coordinator, "smart"))
    entities.append(EmaldoBatteryRangeMarker(schedule_coordinator, "emergency"))

    async_add_entities(entities)


class EmaldoEVFixedChargeAmount(CoordinatorEntity[EmaldoCoordinator], NumberEntity):
    """Number entity for EV fixed charge amount (Instant Fixed mode)."""

    _attr_has_entity_name = True
    _attr_name = "EV fixed charge amount"
    _attr_icon = "mdi:battery-charging-outline"
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_mode = NumberMode.BOX
    _attr_native_min_value = 1
    _attr_native_step = 1

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        """Initialize the EV fixed-charge number entity."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_ev_fixed_charge_amount"

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
    def native_value(self) -> float | None:
        """Return currently configured fixed charge value."""
        if self.coordinator.data is None:
            return None
        ev = self.coordinator.data.get("ev")
        if not isinstance(ev, dict):
            return None

        kwh = ev.get("fixed_kwh")
        full = ev.get("fixed_full_kwh")
        if full and full > self._attr_native_max_value:
            self._attr_native_max_value = float(full)
        return float(kwh) if kwh is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write fixed charge amount and switch charger to Instant Fixed mode."""
        fixed_kwh = int(value)
        await self.hass.async_add_executor_job(
            self.coordinator._write_ev_mode, EV_MODE_INSTANT_FIXED, fixed_kwh
        )
        await self.coordinator.async_request_refresh()


class EmaldoBatteryRangeMarker(
    CoordinatorEntity[EmaldoScheduleCoordinator], NumberEntity
):
    """Slider for one of the AI Battery Range markers (smart or emergency).

    Reads from the schedule coordinator's last `get_overrides` snapshot.
    Writes via :meth:`EmaldoClient.set_battery_range`, which sends opcode
    0x1AA0 with all 96 per-slot overrides cleared to 0x80. The override-active
    flag (byte 2) is preserved from the last read state — only the
    `EmaldoBatteryRangeOverrideSwitch` entity changes that flag.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:battery-charging-medium"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "%"

    def __init__(
        self,
        coordinator: EmaldoScheduleCoordinator,
        kind: str,
    ) -> None:
        """Initialize. ``kind`` is 'smart' or 'emergency'."""
        super().__init__(coordinator)
        if kind not in ("smart", "emergency"):
            raise ValueError(f"kind must be 'smart' or 'emergency' (got {kind!r})")
        self._kind = kind
        nice = "Smart reserve" if kind == "smart" else "Emergency reserve"
        self._attr_name = f"AI {nice}"
        self._attr_unique_id = f"{coordinator.home_id}_battery_range_{kind}"

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
    def native_value(self) -> float | None:
        """Return the current marker % from the last override read."""
        ov = (self.coordinator.data or {}).get("overrides") or {}
        key = "high_marker" if self._kind == "smart" else "low_marker"
        val = ov.get(key)
        return float(val) if val is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write a new marker. Preserves the other marker + override flag."""
        new_pct = int(round(value))
        ov = (self.coordinator.data or {}).get("overrides") or {}
        smart = ov.get("high_marker", 50)
        emergency = ov.get("low_marker", 10)
        enable = bool(ov.get("battery_range_override", False))
        if self._kind == "smart":
            smart = new_pct
        else:
            emergency = new_pct
        if smart < emergency:
            _LOGGER.warning(
                "Refusing battery-range write: smart (%d) < emergency (%d)",
                smart, emergency,
            )
            return

        def _write() -> bool:
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

        ok = await self.hass.async_add_executor_job(_write)
        if not ok:
            _LOGGER.warning("Battery Range write was not acknowledged")
        await self.coordinator.async_request_refresh()
