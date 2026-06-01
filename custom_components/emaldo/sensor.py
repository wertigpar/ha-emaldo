"""Sensor platform for Emaldo integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .emaldo_lib.const import (
    SLOT_NO_OVERRIDE,
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    decode_slot_action,
)

from .const import DOMAIN, EV_UNSUPPORTED_MODELS, PV_UNSUPPORTED_MODELS
from .coordinator import EmaldoCoordinator, EmaldoRealtimeCoordinator
from .schedule_coordinator import EmaldoScheduleCoordinator


def _latest_nonzero(d: dict | None) -> list | None:
    """Return the latest time-series entry with nonzero values."""
    if not isinstance(d, dict):
        return None
    entries = d.get("data", [])
    for e in reversed(entries):
        if len(e) >= 2 and any(v != 0 for v in e[1:]):
            return e
    return entries[-1] if entries else None


def _latest(d: dict | None) -> list | None:
    """Return the last entry from time-series data."""
    if not isinstance(d, dict):
        return None
    entries = d.get("data", [])
    return entries[-1] if entries else None


# -- Value extraction functions --


def _battery_soc(data: dict[str, Any]) -> float | None:
    level_data = data.get("battery", {}).get("power_level", {})
    if not isinstance(level_data, dict):
        return None
    entries = level_data.get("data", [])
    for e in reversed(entries):
        if len(e) >= 2:
            return float(e[1])
    return None


def _battery_charged_today(data: dict[str, Any]) -> float | None:
    """Total battery charge energy today in kWh.

    The ``/bmt/stats/battery-v2/day/`` response has at least 6 columns:
    [minute_offset, discharge_W, charge_main_W, charge_aux_W, charge_ac_W, state].

    For **Power Core** (internal MPPT solar):
      col 2 = solar MPPT → battery DC charge
      col 3 = grid → battery AC charge
      col 4 = 0 (unused, no separate AC-bus channel)

    For **Power Store** (external/third-party solar inverter on the AC bus):
      col 2 = 0 (no internal MPPT)
      col 3 = grid-direct battery charge only
      col 4 = solar-sourced AC-bus battery charge (this is the missing energy)

    Summing cols 2 + 3 + 4 is safe for both models: Power Core sees col 4 = 0,
    while Power Store gets the full AC-bus charge included.
    """
    bat_data = data.get("battery", {}).get("battery", {})
    if not isinstance(bat_data, dict):
        return None
    entries = bat_data.get("data", [])
    if not entries:
        return None
    total = sum(
        e[2] + e[3] + (e[4] if len(e) > 4 else 0)
        for e in entries
        if len(e) >= 4
    )
    return round(total * 5 / 60 / 1000, 2)


def _battery_discharged_today(data: dict[str, Any]) -> float | None:
    bat_data = data.get("battery", {}).get("battery", {})
    if not isinstance(bat_data, dict):
        return None
    entries = bat_data.get("data", [])
    if not entries:
        return None
    total = sum(e[1] for e in entries if len(e) >= 2)
    return round(total * 5 / 60 / 1000, 2)


def _sum_series(series: dict | None, column: int, interval_min: int = 5) -> float | None:
    """Sum a column from a 5-minute-interval power series and return kWh."""
    if not isinstance(series, dict):
        return None
    entries = series.get("data", [])
    if not entries:
        return None
    total = sum(e[column] for e in entries if len(e) > column)
    return round(total * interval_min / 60 / 1000, 3)


# Per the ``/bmt/stats/mppt-v2/day/`` layout (confirmed against the v1
# ``/bmt/stats/mppt/day/`` documentation and the official-app MPPT A/B/C
# breakdown), each 5-minute row is:
#   [minute_offset, string_1_W, string_2_W, string_3_W, pv_total_W, state]
# Only models with integrated MPPT (Power Core) populate these columns;
# on other models the series is all zeros.
_MPPT_STRING_COLUMNS = (1, 2, 3)


def _solar_series_entries(data: dict[str, Any]) -> list | None:
    """Return the raw mppt-v2 data rows, or None if unavailable."""
    solar_resp = data.get("solar")
    if not isinstance(solar_resp, dict):
        return None
    series = solar_resp.get("mppt") if "mppt" in solar_resp else solar_resp
    if not isinstance(series, dict):
        return None
    return series.get("data") or None


def _solar_string_energy_today(data: dict[str, Any], column: int) -> float | None:
    """Energy produced today by a single MPPT string (kWh)."""
    entries = _solar_series_entries(data)
    if not entries:
        return None
    total = sum(e[column] for e in entries if len(e) > column)
    return round(total * 5 / 60 / 1000, 3)


def _solar_energy_today(data: dict[str, Any]) -> float | None:
    """Total solar energy produced today (sum of the MPPT string columns).

    The mppt-v2 series carries the three per-string columns *and* a
    pre-summed total column plus a state column. Summing only the per-string
    columns (1-3) yields the true production without double-counting the
    total/state columns.
    """
    entries = _solar_series_entries(data)
    if not entries:
        return None
    total = sum(
        sum(e[c] for c in _MPPT_STRING_COLUMNS if len(e) > c)
        for e in entries
    )
    return round(total * 5 / 60 / 1000, 3)


def _grid_import_today(data: dict[str, Any]) -> float | None:
    """Total grid import energy today.

    The grid stats endpoint with ``get_real=True`` returns 13 columns per row:
    ``[time_offset, import_W, ?, export_W, ?, phantom_W, 0, ...]``.
    """
    grid_resp = data.get("power", {}).get("grid")
    return _sum_series(grid_resp, column=1)


def _grid_export_today(data: dict[str, Any]) -> float | None:
    """Total grid export energy today (col[3] of grid stats with get_real)."""
    grid_resp = data.get("power", {}).get("grid")
    return _sum_series(grid_resp, column=3)


def _load_energy_today(data: dict[str, Any]) -> float | None:
    """Total property load energy today (col[2] of usage stats)."""
    usage_resp = data.get("power", {}).get("usage")
    return _sum_series(usage_resp, column=2)


def _battery_power(data: dict[str, Any]) -> float | None:
    """Battery power in W — HA Energy Dashboard "Standard" convention.

    HA is house-centric: positive = flowing *into* the house, so positive
    means the battery is *discharging* (feeding home) and negative means
    *charging*. The Emaldo wire value already matches this, so we pass it
    through unchanged. Users can select "Standard" in the Energy Dashboard
    battery setup without needing the "Inverted" option.
    """
    if isinstance(data, dict):
        return data.get("battery_w")
    return None


def _grid_power(data: dict[str, Any]) -> float | None:
    """Grid power in W — HA convention: positive = importing, negative = exporting.

    The Emaldo wire value already matches this convention.
    """
    if isinstance(data, dict):
        return data.get("grid_w")
    return None


def _dual_power(data: dict[str, Any]) -> float | None:
    """Home consumption in W — HA convention: positive = consuming.

    The Emaldo wire value reports consumption as negative (a sink from the
    home node's POV). We flip it so the sensor reads as a positive load.
    """
    if isinstance(data, dict):
        w = data.get("dual_power_w")
        return -w if w is not None else None
    return None


def _solar_power(data: dict[str, Any]) -> float | None:
    """Solar PV power in W (Power Core only)."""
    if isinstance(data, dict):
        return data.get("solar_w")
    return None


def _car_charge_power(data: dict[str, Any]) -> float | None:
    """EV charger power in W — positive = charging the car (Power Core only).

    The Emaldo wire value reports EV load as negative (a sink from the home
    node's POV). We flip it so the sensor reads as a positive load, matching
    the Consumption sensor convention and user expectations for "car charge
    power" (0 = idle, positive = drawing power).
    """
    if isinstance(data, dict):
        w = data.get("ev_w")
        return -w if w is not None else None
    return None


def _balancing_display(data: dict[str, Any]) -> str | None:
    """Return the display string for the balancing state sensor.

    Returns None when the E2E call failed so the sensor shows as unknown
    rather than silently reporting "idle" on a transient network failure.
    """
    rf = data.get("regulate_frequency")
    if not isinstance(rf, dict):
        return None
    return rf.get("display", None)


# -- Sensor descriptions --


@dataclass(frozen=True, kw_only=True)
class EmaldoSensorEntityDescription(SensorEntityDescription):
    """Describe an Emaldo sensor."""

    value_fn: Callable[[dict[str, Any]], float | None]


# Sensors that read from the slow REST coordinator (battery + energy totals)
REST_SENSOR_DESCRIPTIONS: tuple[EmaldoSensorEntityDescription, ...] = (
    EmaldoSensorEntityDescription(
        key="battery_soc",
        translation_key="battery_soc",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_battery_soc,
    ),
    EmaldoSensorEntityDescription(
        key="battery_charged_today",
        translation_key="battery_charged_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_battery_charged_today,
    ),
    EmaldoSensorEntityDescription(
        key="battery_discharged_today",
        translation_key="battery_discharged_today",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_battery_discharged_today,
    ),
    EmaldoSensorEntityDescription(
        key="solar_energy_today",
        translation_key="solar_energy_today",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_solar_energy_today,
    ),
    EmaldoSensorEntityDescription(
        key="grid_import_today",
        translation_key="grid_import_today",
        icon="mdi:transmission-tower-import",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_grid_import_today,
    ),
    EmaldoSensorEntityDescription(
        key="grid_export_today",
        translation_key="grid_export_today",
        icon="mdi:transmission-tower-export",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_grid_export_today,
    ),
    EmaldoSensorEntityDescription(
        key="load_energy_today",
        translation_key="load_energy_today",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=_load_energy_today,
    ),
)

# Per-string solar energy sensors — only meaningful on models with integrated
# MPPT (Power Core). Each reads one string column from the mppt-v2 series.
PV_STRING_ENERGY_DESCRIPTIONS: tuple[EmaldoSensorEntityDescription, ...] = tuple(
    EmaldoSensorEntityDescription(
        key=f"solar_string_{n}_energy_today",
        translation_key=f"solar_string_{n}_energy_today",
        icon="mdi:solar-power-variant",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL,
        value_fn=(lambda d, col=col: _solar_string_energy_today(d, col)),
    )
    for n, col in enumerate(_MPPT_STRING_COLUMNS, start=1)
)

# Sensors that read from the fast E2E realtime coordinator (power flow)
REALTIME_SENSOR_DESCRIPTIONS: tuple[EmaldoSensorEntityDescription, ...] = (
    EmaldoSensorEntityDescription(
        key="battery_power",
        translation_key="battery_power",
        icon="mdi:battery-charging",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_battery_power,
    ),
    EmaldoSensorEntityDescription(
        key="grid_power",
        translation_key="grid_power",
        icon="mdi:transmission-tower",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_grid_power,
    ),
    EmaldoSensorEntityDescription(
        key="dual_power",
        translation_key="dual_power",
        icon="mdi:home-lightning-bolt",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_dual_power,
    ),
)

# Solar PV sensor — available on models with integrated MPPT (e.g. PC1, PC3)
PV_REALTIME_DESCRIPTIONS: tuple[EmaldoSensorEntityDescription, ...] = (
    EmaldoSensorEntityDescription(
        key="solar_power",
        translation_key="solar_power",
        icon="mdi:solar-power",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_solar_power,
    ),
)

# EV charger sensor — available on models with integrated EV charger (e.g. PC1, PC3)
EV_REALTIME_DESCRIPTIONS: tuple[EmaldoSensorEntityDescription, ...] = (
    EmaldoSensorEntityDescription(
        key="car_charge_power",
        translation_key="car_charge_power",
        icon="mdi:car-electric",
        native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_car_charge_power,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Emaldo sensors from a config entry."""
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: EmaldoCoordinator = data["power"]
    realtime_coordinator = data["realtime"]
    schedule_coordinator: EmaldoScheduleCoordinator = data["schedule"]

    entities: list[SensorEntity] = [
        EmaldoSensor(coordinator, description)
        for description in REST_SENSOR_DESCRIPTIONS
    ]

    # Real-time power sensors come from the E2E coordinator
    entities.extend(
        EmaldoSensor(realtime_coordinator, description)
        for description in REALTIME_SENSOR_DESCRIPTIONS
    )

    # Solar PV and EV charger sensors — created only for models that support them
    model = coordinator.device_model or ""
    if model not in PV_UNSUPPORTED_MODELS:
        entities.extend(
            EmaldoSensor(realtime_coordinator, desc)
            for desc in PV_REALTIME_DESCRIPTIONS
        )
        # Per-string solar energy (from the slow REST mppt-v2 series)
        entities.extend(
            EmaldoSensor(coordinator, desc)
            for desc in PV_STRING_ENERGY_DESCRIPTIONS
        )
    if model not in EV_UNSUPPORTED_MODELS:
        entities.extend(
            EmaldoSensor(realtime_coordinator, desc)
            for desc in EV_REALTIME_DESCRIPTIONS
        )

    # Diagnostic: realtime connection status
    entities.append(EmaldoRealtimeStatusSensor(realtime_coordinator))
    entities.append(EmaldoBalancingStateSensor(realtime_coordinator))

    entities.append(EmaldoPlanSourceSensor(schedule_coordinator))
    entities.append(EmaldoActiveModeSensor(schedule_coordinator))
    entities.append(EmaldoScheduleChartSensor(schedule_coordinator))
    async_add_entities(entities)

    # Per-module battery sensors — discovered dynamically when realtime data arrives.
    _registered_module_serials: set[str] = set()

    def _maybe_add_battery_modules() -> None:
        modules = (realtime_coordinator.data or {}).get("battery_modules") or []
        new_entities: list[SensorEntity] = []
        for num, module in enumerate(modules, start=1):
            serial = module.get("serial") or ""
            if not serial or serial in _registered_module_serials:
                continue
            _registered_module_serials.add(serial)
            for metric in ("soc", "soh", "bms_temp_c", "voltage_v", "serial"):
                new_entities.append(
                    EmaldoBatteryModuleSensor(realtime_coordinator, serial, num, metric)
                )
        if new_entities:
            async_add_entities(new_entities)

    entry.async_on_unload(
        realtime_coordinator.async_add_listener(_maybe_add_battery_modules)
    )
    _maybe_add_battery_modules()  # Handle case where data is already available


class EmaldoSensor(CoordinatorEntity[EmaldoCoordinator], SensorEntity):
    """Representation of an Emaldo sensor."""

    entity_description: EmaldoSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EmaldoCoordinator,
        description: EmaldoSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.home_id}_{description.key}"

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, coordinator.home_id)}
            if not (coordinator := self.coordinator).device_id
            else {(DOMAIN, coordinator.device_id)},
            name=coordinator.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=coordinator.device_model,
        )

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def last_reset(self) -> datetime | None:
        """Return midnight (local) for daily-total sensors; None for others."""
        if self.entity_description.state_class == SensorStateClass.TOTAL:
            return dt_util.start_of_local_day()
        return None


# -- Helper to compute current slot index --


def _current_slot_index(schedule: dict[str, Any]) -> int | None:
    """Return the current 0-based slot index, or None if unavailable."""
    from zoneinfo import ZoneInfo

    start_time = schedule.get("start_time", 0)
    gap = schedule.get("gap", 15)
    tz_name = schedule.get("timezone", "UTC")
    if not start_time:
        return None
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    now = datetime.now(tz)
    day_start = datetime.fromtimestamp(start_time, tz)
    elapsed = (now - day_start).total_seconds()
    if elapsed < 0:
        return None
    return int(elapsed / (gap * 60))


# -- Schedule-based sensors --


class EmaldoPlanSourceSensor(
    CoordinatorEntity[EmaldoScheduleCoordinator], SensorEntity
):
    """Reports whether the current slot is 'Internal' or 'Override'."""

    _attr_has_entity_name = True
    _attr_translation_key = "plan_source"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["Internal", "Override"]

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_plan_source"

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
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        schedule = self.coordinator.data.get("schedule") or {}
        overrides_data = self.coordinator.data.get("overrides") or {}
        override_slots = overrides_data.get("slots", []) if overrides_data else []

        idx = _current_slot_index(schedule)
        if idx is None:
            return None

        # E2E rolling model: current slot is always in the "today" portion
        tod = idx % 96
        if tod < len(override_slots) and override_slots[tod] != SLOT_NO_OVERRIDE:
            return "Override"
        return "Internal"


class EmaldoActiveModeSensor(
    CoordinatorEntity[EmaldoScheduleCoordinator], SensorEntity
):
    """Reports the effective mode of the current time slot."""

    _attr_has_entity_name = True
    _attr_translation_key = "active_mode"

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_active_mode"

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
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        schedule = self.coordinator.data.get("schedule") or {}
        overrides_data = self.coordinator.data.get("overrides") or {}
        override_slots = overrides_data.get("slots", []) if overrides_data else []
        high = overrides_data.get("high_marker", DEFAULT_MARKER_HIGH) if overrides_data else DEFAULT_MARKER_HIGH
        low = overrides_data.get("low_marker", DEFAULT_MARKER_LOW) if overrides_data else DEFAULT_MARKER_LOW

        slots = schedule.get("hope_charge_discharges", [])
        idx = _current_slot_index(schedule)
        if idx is None:
            return None

        # E2E rolling model: use time-of-day for override lookup
        tod = idx % 96
        if tod < len(override_slots):
            ov = override_slots[tod]
            if ov != SLOT_NO_OVERRIDE:
                return decode_slot_action(ov, low, high)

        # Fall back to schedule
        if idx < len(slots):
            value = slots[idx]
            if value == 100:
                return "Charge"
            elif value < 0:
                return "Discharge"
            else:
                return "Idle"

        return None


def _schedule_slot_to_numeric(value: int) -> int:
    """Convert a schedule slot value to numeric: 1=charge, -1=discharge, 0=idle."""
    if value == 100:
        return 1
    elif value < 0:
        return -1
    return 0


def _override_slot_to_numeric(value: int) -> int:
    """Convert an override slot value to numeric: 1=charge, -1=discharge, 0=idle."""
    if value == SLOT_NO_OVERRIDE:
        return 0  # not overridden, handled separately
    if value == 0:
        return 0  # idle
    if 1 <= value <= 100:
        return 1  # charge
    if value > 128:
        return -1  # discharge
    return 0


class EmaldoScheduleChartSensor(
    CoordinatorEntity[EmaldoScheduleCoordinator], SensorEntity
):
    """Sensor that exposes the full schedule as chartable attributes.

    State: numeric mode of the current slot (1=charge, -1=discharge, 0=idle).
    Attributes:
      - schedule: list of {t, mode, price, solar} dicts for charting
      - overrides: list of {t, mode} dicts for today's overrides
    """

    _attr_has_entity_name = True
    _attr_translation_key = "schedule_chart"
    _attr_icon = "mdi:chart-timeline-variant"
    _unrecorded_attributes = frozenset({"schedule"})

    def __init__(self, coordinator: EmaldoScheduleCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_schedule_chart"

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
    def native_value(self) -> int | None:
        """Current slot's numeric mode."""
        if self.coordinator.data is None:
            return None
        schedule = self.coordinator.data.get("schedule") or {}
        overrides_data = self.coordinator.data.get("overrides") or {}
        override_slots = overrides_data.get("slots", []) if overrides_data else []
        slots = schedule.get("hope_charge_discharges", [])
        idx = _current_slot_index(schedule)
        if idx is None:
            return None

        # E2E rolling model: current slot is always in the "today" portion
        tod = idx % 96
        if tod < len(override_slots):
            ov = override_slots[tod]
            if ov != SLOT_NO_OVERRIDE:
                return _override_slot_to_numeric(ov)

        if idx < len(slots):
            return _schedule_slot_to_numeric(slots[idx])
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose schedule data as attributes for chart cards.

        Uses compact format (HH:MM times, short keys) to stay within
        the HA recorder 16 KB attribute limit.
        """
        if self.coordinator.data is None:
            return None

        from zoneinfo import ZoneInfo

        schedule = self.coordinator.data.get("schedule") or {}
        overrides_data = self.coordinator.data.get("overrides") or {}

        slots = schedule.get("hope_charge_discharges", [])
        prices = schedule.get("market_prices", [])
        solar = schedule.get("forecast_solars", [])
        start_time = schedule.get("start_time", 0)
        gap = schedule.get("gap", 15)
        tz_name = schedule.get("timezone", "UTC")

        if not slots or not start_time:
            return None

        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = ZoneInfo("UTC")

        day_start = datetime.fromtimestamp(start_time, tz)

        # Build override lookup
        override_slots_list = overrides_data.get("slots", []) if overrides_data else []

        # Current time-of-day slot for rolling model interpretation
        now_slot_idx = _current_slot_index(schedule)
        now_tod = (now_slot_idx % 96) if now_slot_idx is not None else 0

        # Build schedule time series with full keys for dashboard compatibility
        _MODE_LABELS = {1: "Charge", -1: "Discharge", 0: "Idle"}
        sched_data = []
        for i, value in enumerate(slots):
            slot_time = day_start + timedelta(minutes=i * gap)

            is_overridden = False
            ovr_mode = 0
            if override_slots_list:
                if i < 96:
                    if i >= now_tod and i < len(override_slots_list):
                        ov = override_slots_list[i]
                        if ov != SLOT_NO_OVERRIDE:
                            is_overridden = True
                            ovr_mode = _override_slot_to_numeric(ov)
                else:
                    tomorrow_tod = i - 96
                    if tomorrow_tod < now_tod and tomorrow_tod < len(override_slots_list):
                        ov = override_slots_list[tomorrow_tod]
                        if ov != SLOT_NO_OVERRIDE:
                            is_overridden = True
                            ovr_mode = _override_slot_to_numeric(ov)

            mode = ovr_mode if is_overridden else _schedule_slot_to_numeric(value)
            sched_data.append({
                "t": slot_time.isoformat(),
                "mode": mode,
                "state": _MODE_LABELS[mode],
                "price": round((prices[i] if i < len(prices) else 0) * 100, 2),
                "solar": solar[i] if solar and i < len(solar) and solar[i] else 0,
                "source": "override" if is_overridden else "internal",
            })

        return {
            "start_date": day_start.date().isoformat(),
            "schedule": sched_data,
            "slot_count": len(slots),
            "gap_minutes": gap,
        }


class EmaldoBalancingStateSensor(CoordinatorEntity[EmaldoRealtimeCoordinator], SensorEntity):
    """Reports the real-time grid frequency regulation (balancing) state."""

    _attr_has_entity_name = True
    _attr_translation_key = "balancing_state"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [
        "idle",
        "pre_balancing",
        "fcr_n",
        "fcr_d_up",
        "fcr_d_down",
        "fcr_d_up_down",
        "mfrr_up",
        "mfrr_down",
        "balancing_failed",
    ]
    _attr_icon = "mdi:sine-wave"

    def __init__(self, coordinator: EmaldoRealtimeCoordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.home_id}_balancing_state"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.device_id or self.coordinator.home_id)},
            name=self.coordinator.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=self.coordinator.device_model,
        )

    @property
    def native_value(self) -> str | None:
        rf = self.coordinator.regulate_frequency
        if not isinstance(rf, dict):
            return None
        return rf.get("display")


class EmaldoRealtimeStatusSensor(SensorEntity):
    """Diagnostic sensor showing E2E realtime connection health."""

    _attr_has_entity_name = True
    _attr_translation_key = "realtime_connection"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:lan-connect"

    def __init__(self, coordinator) -> None:
        """Initialize the diagnostic sensor."""
        self._coordinator = coordinator
        self._attr_unique_id = f"{coordinator.home_id}_realtime_status"

    async def async_added_to_hass(self) -> None:
        """Register for coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_add_listener(self._handle_coordinator_update)
        )

    def _handle_coordinator_update(self) -> None:
        """Update state when coordinator refreshes."""
        self.async_write_ha_state()

    @property
    def device_info(self) -> DeviceInfo:
        """Return device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._coordinator.home_id)}
            if not self._coordinator.device_id
            else {(DOMAIN, self._coordinator.device_id)},
            name=self._coordinator.device_name or "Emaldo Battery",
            manufacturer="Emaldo",
            model=self._coordinator.device_model,
        )

    @property
    def native_value(self) -> str:
        """Return current connection state."""
        if self._coordinator.last_update_success:
            return "connected"
        return "reconnecting"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return statistics about the realtime connection."""
        import datetime
        c = self._coordinator

        def _to_iso(ts: float | None) -> str | None:
            if ts is None:
                return None
            return datetime.datetime.fromtimestamp(ts).isoformat()

        success_rate = None
        if c.stats_total_polls > 0:
            success_rate = round(
                100.0 * c.stats_successful_polls / c.stats_total_polls, 1
            )

        return {
            "total_polls": c.stats_total_polls,
            "successful_polls": c.stats_successful_polls,
            "success_rate_pct": success_rate,
            "empty_reads": c.stats_empty_reads,
            "reconnects": c.stats_reconnects,
            "keepalive_failures": c.stats_keepalive_failures,
            "last_success": _to_iso(c.stats_last_success),
            "last_failure": _to_iso(c.stats_last_failure),
            "last_reconnect": _to_iso(c.stats_last_reconnect),
        }


class EmaldoBatteryModuleSensor(CoordinatorEntity[EmaldoRealtimeCoordinator], SensorEntity):
    """A sensor for one metric of one physical battery module."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EmaldoRealtimeCoordinator,
        serial: str,
        module_num: int,
        metric: str,
    ) -> None:
        """Initialize a per-module battery sensor."""
        super().__init__(coordinator)
        self._serial = serial
        self._metric = metric
        self._attr_unique_id = f"{coordinator.home_id}_module_{serial}_{metric}"

        if metric == "soc":
            self._attr_name = f"Battery Module {module_num} SoC"
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_icon = "mdi:battery"
        elif metric == "soh":
            self._attr_name = f"Battery Module {module_num} Health"
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_icon = "mdi:battery-heart"
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif metric == "bms_temp_c":
            self._attr_name = f"Battery Module {module_num} Temperature"
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif metric == "voltage_v":
            self._attr_name = f"Battery Module {module_num} Voltage"
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        elif metric == "serial":
            self._attr_name = f"Battery Module {module_num} Serial"
            self._attr_icon = "mdi:identifier"
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

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
    def native_value(self) -> float | str | None:
        """Return the metric value for this module."""
        modules = (self.coordinator.data or {}).get("battery_modules") or []
        for m in modules:
            if m.get("serial") == self._serial:
                if self._metric == "serial":
                    return self._serial or None
                val = m.get(self._metric)
                return float(val) if val is not None else None
        return None

    @property
    def extra_state_attributes(self) -> dict:
        """Return serial number and SoC for diagnostics."""
        modules = (self.coordinator.data or {}).get("battery_modules") or []
        for m in modules:
            if m.get("serial") == self._serial:
                attrs: dict = {"serial_number": self._serial}
                soc = m.get("soc")
                if soc is not None:
                    attrs["battery_soc"] = int(soc)
                return attrs
        return {"serial_number": self._serial}

