"""Emaldo Battery — time entities for emergency charge scheduling."""

from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EmaldoCoordinator


def _uid_base(coordinator: EmaldoCoordinator) -> str:
    """Return stable UID base (legacy for primary, device-scoped for fan-out)."""
    if getattr(coordinator, "_legacy_uid_mode", False):
        return coordinator.home_id
    return coordinator.device_id or coordinator.home_id


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo time entities from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    entities: list[TimeEntity] = []
    for item in data.get("devices") or [data]:
        coordinator: EmaldoCoordinator = item["power"]
        entities.extend(
            [
                EmaldoEmergencyChargeStart(coordinator),
                EmaldoEmergencyChargeEnd(coordinator),
            ]
        )

    async_add_entities(entities)


class _EmaldoEmergencyChargeTimeBase(
    CoordinatorEntity[EmaldoCoordinator], TimeEntity
):
    """Shared base for emergency charge start / end time entities."""

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
    def native_value(self) -> time | None:
        """Return the currently configured time."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self._coordinator_key)  # type: ignore[return-value]

    async def async_set_value(self, value: time) -> None:
        """Store the new time and update coordinator state optimistically."""
        setattr(self.coordinator, self._coordinator_attr, value)
        if self.coordinator.data is not None:
            updated = dict(self.coordinator.data)
            updated[self._coordinator_key] = value
            self.coordinator.async_set_updated_data(updated)


class EmaldoEmergencyChargeStart(_EmaldoEmergencyChargeTimeBase):
    """Emergency charge window — start time.

    Set this to the desired charge start time before activating the
    *Emergency charge* switch.  If left unset (unknown) when the switch is
    turned on, the integration uses the current time as the start.
    """

    _attr_translation_key = "charge_start"
    _attr_icon = "mdi:battery-clock"
    _coordinator_key = "emergency_charge_start_t"
    _coordinator_attr = "_emergency_charge_start_t"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{_uid_base(coordinator)}_emergency_charge_start"


class EmaldoEmergencyChargeEnd(_EmaldoEmergencyChargeTimeBase):
    """Emergency charge window — end time.

    Set this to the desired charge end time before activating the
    *Emergency charge* switch.  If left unset (unknown) when the switch is
    turned on, the integration uses start + 1 hour as the end.
    Overnight windows (e.g. start 23:00, end 04:00) are handled
    automatically — the end date is advanced to the next day when it
    falls before the start.
    """

    _attr_translation_key = "charge_stop"
    _attr_icon = "mdi:battery-clock-outline"
    _coordinator_key = "emergency_charge_end_t"
    _coordinator_attr = "_emergency_charge_end_t"

    def __init__(self, coordinator: EmaldoCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{_uid_base(coordinator)}_emergency_charge_end"
