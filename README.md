# Emaldo Battery – Home Assistant Integration

Custom [HACS](https://hacs.xyz/) integration for Emaldo home battery systems.

## Features

- Battery state of charge, capacity, charge/discharge power and daily energy
- Grid and load power monitoring
- AI schedule & override visualization (chart sensor)
- Plan source and active mode sensors
- Override services: set slot ranges, apply bulk schedules, reset to internal
- Real-time grid frequency regulation (balancing) state via E2E (FCR-N, FCR-D, mFRR)
- EV charge mode select and fixed charge amount control (Power Core models)
- Sell Back to Grid toggle and Sell Limit switch with configurable daily kWh threshold
- Manual selling switch and target kWh number for direct E2E grid-export (opcode 0x80/0x81)
- Fires `emaldo_next_day_schedule_ready` event when tomorrow's plan appears
- Configurable schedule polling (default: startup + 14:00 + every 2h)
- Automatic device discovery within a home
- Reconfigure credentials and app parameters without removing the integration

## Installation

### HACS (recommended)

1. Open HACS → Integrations → ⋮ → Custom repositories
2. Add this repository URL, category **Integration**
3. Install **Emaldo Battery**
4. Restart Home Assistant

### Manual

Copy `custom_components/emaldo/` into your HA `config/custom_components/` directory and restart.

## Configuration

> **Important:** Emaldo only allows one active session per account. If you use the same account as the official Emaldo app, this integration will sign it out every time it connects — and vice versa. **Create a dedicated Emaldo account for Home Assistant.**

Go to **Settings → Devices & Services → Add Integration → Emaldo Battery** and enter:

| Field | Description |
|-------|-------------|
| Email | Emaldo account email |
| Password | Account password |
| App ID | From `.emaldo_params.json` (see client README) |
| App Secret | From `.emaldo_params.json` |
| App Version | From `.emaldo_params.json` |
| Home ID | Optional – auto-detected if left blank |

You can get App ID, App Secret and App Version by running `python -m emaldo.extract_keys <apk>` from the emaldo client package.

### Reconfiguring credentials

To update your email, password, app version, or encryption keys without removing the integration:

**Settings → Devices & Services → Emaldo → ⋮ (three-dot menu) → Reconfigure**

All current values are pre-filled. The integration reloads automatically after saving.

### Options (Settings → Integrations → Emaldo → Configure)

| Option | Default | Description |
|--------|---------|-------------|
| Schedule start hour | 14 | Hour (0-23) for the first schedule check |
| Schedule start minute | 0 | Minute (0-59) for the first schedule check |
| Schedule interval | 7200 | Repeat interval in seconds (default 2h) |

## Entities

### Sensors

| Sensor | Unit | Description |
|--------|------|-------------|
| Battery SoC | % | Current state of charge |
| Battery capacity | kWh | Total battery capacity |
| Battery charged today | kWh | Energy charged today |
| Battery discharged today | kWh | Energy discharged today |
| Battery power | W | Net power (positive = charging) |
| Grid power | W | Net grid (positive = importing) |
| Load power | W | Home consumption |
| Plan source | — | "Internal" or "Override" for current slot |
| Active mode | — | Effective mode: Charge, Discharge, Idle, etc. |
| Schedule chart | — | Numeric mode + schedule data in attributes |
| Balancing state | — | Grid frequency regulation state: `idle`, `pre_balancing`, `fcr_n`, `fcr_d_up`, `fcr_d_down`, `fcr_d_up_down`, `mfrr_up`, `mfrr_down`, `balancing_failed` |
| Realtime connection | — | Diagnostic: E2E connection health and statistics |
| Battery Module N SoC | % | Per-module state of charge (one sensor per physical battery module) |
| Battery Module N Temperature | °C | Per-module BMS temperature |
| Battery Module N Voltage | V | Per-module pack voltage (diagnostic) |
| Battery Module N Health | % | Per-module state of health / capacity retention (diagnostic) |

> **Per-module sensors** are discovered automatically on the first successful battery module poll (~10 minutes after startup). Sensors are registered dynamically as modules are found — no configuration required.

### Controls

#### Grid export (all models)

| Entity | Type | Description |
|--------|------|-------------|
| Sell Back to Grid | Switch | Enables/disables selling surplus energy back to the grid |
| Sell Limit | Switch | Activates the daily grid-export cap protection |
| Sell Limit threshold | Number | Daily export cap in kWh/day (1–300). Only effective when Sell Limit is ON |
| Manual selling | Switch | Starts/stops direct manual grid-export via E2E (0x80); turn OFF to stop |
| Manual selling target | Number | Total kWh to sell before auto-stopping (1–100 kWh). Set before enabling |

#### EV charge controls (Power Core models only)

The following entities are only created for **Power Core** models (PC\*). They are hidden for models without an integrated EV charger (e.g. PS1-BAK10-HS10).

| Entity | Type | Description |
|--------|------|-------------|
| EV charge mode | Select | `lowest_price`, `solar_only`, `scheduled`, `instant_full`, `instant_fixed` |
| EV fixed charge amount | Number | Target kWh for Instant Fixed mode (1–100 kWh) |

**Battery schedule** — shows the AI-planned charge/discharge schedule as merged events. Override slots are prefixed "Override:" in the event summary. Events include average market price and solar forecast.

## Services

| Service | Description |
|---------|-------------|
| `emaldo.set_slot_range` | Override a time range with a specific action |
| `emaldo.apply_bulk_schedule` | Push a full 96-slot override array |
| `emaldo.reset_to_internal` | Clear overrides for a time range or all slots |
| `emaldo.refresh_schedule` | Manually trigger an immediate schedule + override refresh |

### `emaldo.set_slot_range`

Override a time range with a charge/discharge/idle action.

```yaml
service: emaldo.set_slot_range
data:
  start_time: "01:00"
  end_time: "05:00"
  action: charge-low
  # Optional: high_marker, low_marker (default: 72, 20)
```

**Actions:** `charge-low`, `charge-high`, `charge-100`, `idle`, `discharge-low`, `discharge-high`, `clear`

### `emaldo.apply_bulk_schedule`

Push a complete 96-slot override array (for EMHASS/AIO integration).

```yaml
service: emaldo.apply_bulk_schedule
data:
  slots: [128, 128, 128, 128, 20, 20, 20, 20, ...]  # 96 values
  # Optional: high_marker, low_marker
```

Slot values: `128` = no override (follow AI), `0` = idle, `1-100` = charge at N%, `184` = discharge-high, `236` = discharge-low.

### `emaldo.reset_to_internal`

Clear overrides, returning to the AI schedule.

```yaml
# Reset a time range
service: emaldo.reset_to_internal
data:
  start_time: "01:00"
  end_time: "05:00"

# Reset all slots
service: emaldo.reset_to_internal
data:
  all: true
```

### `emaldo.refresh_schedule`

Manually trigger an immediate schedule and override data refresh.

```yaml
service: emaldo.refresh_schedule
```

## Events

| Event | Description |
|-------|-------------|
| `emaldo_next_day_schedule_ready` | Fired when tomorrow's schedule first appears (~14:00). Use as automation trigger for EMHASS/AIO optimization. |

## Schedule Chart Visualization

The **Schedule chart** sensor exposes the full schedule as attributes, perfect for [ApexCharts Card](https://github.com/RomRider/apx-charts-card-card):

```yaml
type: custom:apexcharts-card
header:
  title: Battery Schedule
graph_span: 48h
span:
  start: day
series:
  - entity: sensor.power_store_schedule_chart
    data_generator: |
      const data = entity.attributes.schedule;
      return data.map(s => [new Date(s.t).getTime(), s.mode]);
    name: Mode
    type: area
    color: green
  - entity: sensor.power_store_schedule_chart
    data_generator: |
      const data = entity.attributes.schedule;
      return data.map(s => [new Date(s.t).getTime(), s.price]);
    name: Price (c/kWh)
    type: line
    color: orange
    group_by:
      func: raw
  - entity: sensor.power_store_schedule_chart
    data_generator: |
      const data = entity.attributes.schedule;
      return data.map(s => [new Date(s.t).getTime(), s.solar]);
    name: Solar forecast (Wh)
    type: line
    color: yellow
    group_by:
      func: raw
```

Mode values: `1` = charge, `-1` = discharge, `0` = idle.

Data is polled every 60 seconds.
