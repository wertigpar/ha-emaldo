"""Emaldo Battery — datetime entities for emergency charge scheduling."""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmaldoCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo datetime entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EmaldoCoordinator = data["power_coordinator"]

    async_add_entities([
        EmaldoEmergencyChargeStart(coordinator),
        EmaldoEmergencyChargeEnd(coordinator),
    ])


class _EmaldoEmergencyChargeDateTimeBase(
    CoordinatorEntity[EmaldoCoordinator], DateTimeEntity
):
    """Shared base for emergency charge start / end datetime entities."""

    _attr_has_entity_name = True
    _coordinator_key: str
    _coordinator_attr: str

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)

    @property
    def device_info(self) -> DeviceInfo:
        c = self.coordinator
        return DeviceInfo(
            identifiers={(DOMAIN, c.device_id or c.home_id)},
            name=c.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=c.device_model,
        )

    @property
    def native_value(self) -> datetime | None:
        """Return the currently configured datetime."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._coordinator_key)  # type: ignore[return-value]

    async def async_set_value(self, value: datetime) -> None:
        """Store the new datetime and update coordinator state optimistically."""
        setattr(self.coordinator, self._coordinator_attr, value)
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated[self._coordinator_key] = value
            self.coordinator.async_set_updated_data(updated)


class EmaldoEmergencyChargeStart(_EmaldoEmergencyChargeDateTimeBase):
    """Emergency charge window — start time.

    Set this to the desired charge start time before activating the
    *Emergency charge* switch.  If left unset (unknown) when the switch is
    turned on the integration will use the current time as the start.
    """

    _attr_name = "Emergency charge start"
    _attr_icon = "mdi:battery-clock"
    _coordinator_key = "emergency_charge_start_dt"
    _coordinator_attr = "_emergency_charge_start_dt"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_emergency_charge_start"


class EmaldoEmergencyChargeEnd(_EmaldoEmergencyChargeDateTimeBase):
    """Emergency charge window — end time.

    Set this to the desired charge end time before activating the
    *Emergency charge* switch.  If left unset (unknown) when the switch is
    turned on the integration uses start + 1 hour as the end.
    """

    _attr_name = "Emergency charge end"
    _attr_icon = "mdi:battery-clock-outline"
    _coordinator_key = "emergency_charge_end_dt"
    _coordinator_attr = "_emergency_charge_end_dt"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_emergency_charge_end"
