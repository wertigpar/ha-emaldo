[![GitHub Release](https://img.shields.io/github/release/wertigpar/ha-emaldo.svg)](https://github.com/wertigpar/ha-emaldo/releases)
[![License](https://img.shields.io/github/license/wertigpar/ha-emaldo.svg)](https://github.com/wertigpar/ha-emaldo/blob/main/LICENSE)
[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Validate](https://github.com/wertigpar/ha-emaldo/actions/workflows/validate.yml/badge.svg)](https://github.com/wertigpar/ha-emaldo/actions/workflows/validate.yml)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=wertigpar&repository=ha-emaldo&category=integration)

# Emaldo Battery — Home Assistant Custom Integration

A Home Assistant custom integration for [Emaldo](https://emaldo.com/) battery systems. Provides real-time power monitoring, battery state tracking, schedule visualization, and full override control via services.

## Features

- **Real-time sensors** — Battery SoC, battery power, grid power, load power, daily charge/discharge energy
- **Schedule visualization** — Exposes the Emaldo AI schedule and override data as chart-ready attributes
- **Override services** — Set time-range overrides, push full 96-slot schedules, or reset to the internal AI plan
- **E2E communication** — Reads and writes override slots via Emaldo's end-to-end encrypted channel
- **EV charge control** — Select EV charging mode and set fixed charge amount (Power Core models only)
- **Resilient polling** — On API failures, sensors keep their last-known values while exponential-backoff retries recover automatically (60 s → 120 s → 4 min → … capped at 30 min)
- **Next-day schedule event** — Fires `emaldo_next_day_schedule_ready` when tomorrow's schedule appears
- **Reconfigure without removing** — Update credentials or app version via the Reconfigure menu

## Prerequisites

| Requirement | Details |
|---|---|
| **Home Assistant** | 2024.1+ |
| **Emaldo account** | Email + password for the Emaldo app |
| **App credentials** | App ID, App Secret, App Version (from the Emaldo APK) |
| **Network** | Internet access to `api.emaldo.com` and UDP access to the E2E server |

## Installation

1. Copy the `emaldo` folder into your Home Assistant `custom_components/` directory.

   See the [Architecture](#architecture) section for the full file list.

2. Restart Home Assistant.

3. Go to **Settings → Devices & Services → Add Integration → Emaldo Battery**.

## Configuration

### Config Flow

| Field | Description |
|---|---|
| **Email** | Your Emaldo account email |
| **Password** | Your Emaldo account password |
| **App ID** | Application ID from the Emaldo APK |
| **App Secret** | Application secret from the Emaldo APK |
| **App Version** | Application version string (e.g. `2.8.4`) |
| **Home ID** | *(optional)* Leave empty to auto-detect |

### Reconfiguring credentials

To update your email, password, app version, or encryption keys without removing the integration:

**Settings → Devices & Services → Emaldo → ⋮ (three-dot menu) → Reconfigure**

All current values are pre-filled. After saving, the integration reloads automatically with the new credentials.

### Options (Schedule Polling)

After setup, configure schedule polling via **Configure**:

| Option | Description | Default |
|---|---|---|
| **Start hour** | Hour of day to begin schedule polling (0–23) | `14` |
| **Start minute** | Minute to begin schedule polling (0–59) | `0` |
| **Repeat interval** | Polling interval in seconds (600–86400) | `7200` (2 hours) |

## Sensors

### Power & Battery Sensors

Polled every 60 seconds from the Emaldo cloud API.

| Sensor | Unit | Description |
|---|---|---|
| **Battery SoC** | % | Current state of charge |
| **Battery capacity** | kWh | Total battery capacity |
| **Battery charged today** | kWh | Energy charged today (cumulative) |
| **Battery discharged today** | kWh | Energy discharged today (cumulative) |
| **Battery power** | W | Net battery power (positive = charging, negative = discharging) |
| **Grid power** | W | Net grid power (positive = importing, negative = exporting) |
| **Load power** | W | Household load power |

### Schedule Sensors

Updated on the configured polling schedule.

| Sensor | Description |
|---|---|
| **Plan source** | Whether the current slot is `Internal` (AI schedule) or `Override` |
| **Active mode** | The effective action of the current slot (e.g. `Charge`, `Discharge`, `Idle`, `charge-high (72%)`) |
| **Schedule chart** | Numeric mode of current slot (1/0/−1) with full schedule data in attributes |

### Real-time Balancing Sensor

Read via E2E on every coordinator poll (best-effort — unavailable when E2E is unreachable).

| Sensor | Description |
|---|---|
| **Balancing state** | Current grid frequency regulation state (`idle`, `pre_balancing`, `balancing`, `balancing_failed`) |

### EV Charge Controls (Power Core models only)

These entities are created only for **Power Core** models (e.g. `PC1-BAK15-HS10`, `PC3-*`). They are hidden for models without an integrated EV charger such as `PS1-BAK10-HS10`.

| Entity | Type | Description |
|---|---|---|
| **EV charge mode** | Select | Sets the EV charging strategy (see modes below) |
| **EV fixed charge amount** | Number | Target kWh for *Instant Fixed* mode (1–100 kWh) |

**EV charge mode options:**

| Option | Description |
|---|---|
| `lowest_price` | Smart — charge during the cheapest grid hours |
| `solar_only` | Smart — charge only from surplus solar PV |
| `scheduled` | Smart — charge on a configured weekday/weekend hour schedule |
| `instant_full` | Instant — charge at full power until the car is full |
| `instant_fixed` | Instant — charge exactly the configured kWh amount then stop |

The **EV fixed charge amount** number is only effective when mode is `instant_fixed`.

#### Balancing State Values

| Value | Meaning |
|---|---|
| `idle` | Battery is not participating in any grid balancing service |
| `pre_balancing` | Battery is on hold, balancing is imminent |
| `fcr_n` | Actively providing FCR-N (Normal Frequency Containment Reserve) |
| `fcr_d_up` | Actively providing FCR-D Up (Disturbance reserve, upward regulation) |
| `fcr_d_down` | Actively providing FCR-D Down (Disturbance reserve, downward regulation) |
| `fcr_d_up_down` | Actively providing FCR-D Up+Down (bidirectional disturbance reserve) |
| `mfrr_up` | Providing mFRR Up (manual Frequency Restoration Reserve, upward) |
| `mfrr_down` | Providing mFRR Down (manual Frequency Restoration Reserve, downward) |
| `balancing_failed` | The balancing session ended with an error reported by the Emaldo server |

The sensor uses `device_class: enum`. It is best-effort — if the E2E connection fails it returns `unknown` until the next successful poll.

### Schedule Chart Attributes

The **Schedule chart** sensor exposes the full schedule as extra state attributes for dashboard visualization:

```json
{
  "start_date": "2026-03-19",
  "schedule": [
    {
      "t": "2026-03-19T00:00:00+02:00",
      "mode": 0,
      "state": "Idle",
      "price": 1.23,
      "solar": 0,
      "source": "internal"
    },
    {
      "t": "2026-03-19T01:00:00+02:00",
      "mode": 1,
      "state": "Charge",
      "price": 2.45,
      "solar": 0,
      "source": "override"
    },
    ...
  ],
  "slot_count": 192,
  "gap_minutes": 15
}
```

- `schedule[].t`: ISO 8601 timestamp with timezone
- `schedule[].mode`: `1` = charge, `−1` = discharge, `0` = idle
- `schedule[].state`: `"Charge"`, `"Discharge"`, or `"Idle"` — human-readable label for timeline charts
- `schedule[].price`: Market price in cents/kWh
- `schedule[].solar`: Solar forecast value (W), `0` when no solar
- `schedule[].source`: `"override"` = set by user/optimizer, `"internal"` = battery AI schedule

## Services

### `emaldo.set_slot_range`

Override a time range with a specific charge/discharge action.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `start_time` | time | yes | Start time (HH:MM) |
| `end_time` | time | yes | End time (HH:MM) |
| `action` | select | yes | One of: `charge-low`, `charge-high`, `charge-100`, `idle`, `discharge-low`, `discharge-high`, `clear` |
| `high_marker` | int | no | High battery threshold % (default: 72) |
| `low_marker` | int | no | Low battery threshold % (default: 20) |

```yaml
service: emaldo.set_slot_range
data:
  start_time: "01:00"
  end_time: "05:00"
  action: charge-low
```

### `emaldo.apply_bulk_schedule`

Push a full 96-slot override array. Used by the [Battery Optimizer](../battery_optimizer/) integration or external optimizers.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `slots` | list[int] | yes | 96 integers (0–255) |
| `high_marker` | int | no | High battery threshold % (default: 72) |
| `low_marker` | int | no | Low battery threshold % (default: 20) |

```yaml
service: emaldo.apply_bulk_schedule
data:
  slots: [128, 128, 128, 128, 20, 20, 20, 20, 72, 72, 72, 72, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 184, 184, 184, 184, 184, 184, 184, 184, 128, 128, 128, 128, 0, 0, 0, 0, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128, 128]
```

### `emaldo.reset_to_internal`

Clear overrides, returning to the Emaldo AI schedule.

| Parameter | Type | Required | Description |
|---|---|---|---|
| `start_time` | time | no | Start time (HH:MM). Omit for full reset. |
| `end_time` | time | no | End time (HH:MM). Omit for full reset. |
| `all` | boolean | no | If true, reset all 96 slots (default: false) |
| `high_marker` | int | no | High battery threshold % (default: 72) |
| `low_marker` | int | no | Low battery threshold % (default: 20) |

```yaml
# Reset all overrides
service: emaldo.reset_to_internal
data:
  all: true

# Reset a time range
service: emaldo.reset_to_internal
data:
  start_time: "01:00"
  end_time: "05:00"
```

### `emaldo.refresh_schedule`

Manually trigger an immediate schedule and override data refresh. Useful after API hiccups or when you want up-to-date data without waiting for the next polling cycle.

```yaml
service: emaldo.refresh_schedule
```

## Slot Encoding

The Emaldo battery uses single-byte override values per 15-minute slot:

| Value | Meaning |
|---|---|
| `0` | Idle — battery does nothing |
| `1–100` | Charge until battery reaches N% SoC |
| `128` (0x80) | No override — follow internal AI schedule |
| `129–255` | Discharge down to (256 − value)% SoC |

**Named actions** map to these byte values via `high_marker` and `low_marker`:

| Action | Byte Value |
|---|---|
| `charge-low` | `low_marker` (default: 20) |
| `charge-high` | `high_marker` (default: 72) |
| `charge-100` | `100` |
| `idle` | `0` |
| `discharge-low` | `256 − low_marker` (default: 236) |
| `discharge-high` | `256 − high_marker` (default: 184) |
| `clear` / `none` | `128` |

## Events

| Event | Payload | Description |
|---|---|---|
| `emaldo_next_day_schedule_ready` | `{"entry_id": "..."}` | Fired when tomorrow's schedule first appears (typically after 14:00) |

Use this to trigger automations when the next-day schedule becomes available:

```yaml
automation:
  - alias: "Run optimizer when next-day schedule arrives"
    trigger:
      - platform: event
        event_type: emaldo_next_day_schedule_ready
    action:
      - service: battery_optimizer.run_optimizer
        data:
          reason: "next_day_schedule"
          force: true
```

## Dashboard Examples

### ApexCharts — Battery Schedule Timeline

Requires [apexcharts-card](https://github.com/RomRider/apexcharts-card) from HACS.

Two stacked charts simulating a grouped timeline — **Battery State** shows Charge/Discharge/Idle, **Source** shows Internal vs User Override:

```yaml
type: vertical-stack
cards:
  - type: custom:apexcharts-card
    header:
      title: Battery State
      show: true
      show_states: false
    graph_span: 48h
    span:
      start: day
    now:
      show: true
      label: Now
      color: red
    apex_config:
      chart:
        height: 150px
        stacked: true
      plotOptions:
        bar:
          columnWidth: 100%
      legend:
        show: true
      yaxis:
        - show: false
          min: 0
          max: 1.1
    series:
      - entity: sensor.power_store_schedule_chart
        name: Charge
        type: column
        color: '#2ecc71'
        opacity: 0.9
        show:
          in_header: false
          legend_value: false
        data_generator: |
          const schedule = entity.attributes.schedule || [];
          return schedule.map(s => [
            new Date(s.t).getTime(),
            s.state === 'Charge' ? 1 : null
          ]);
      - entity: sensor.power_store_schedule_chart
        name: Discharge
        type: column
        color: '#e74c3c'
        opacity: 0.9
        show:
          in_header: false
          legend_value: false
        data_generator: |
          const schedule = entity.attributes.schedule || [];
          return schedule.map(s => [
            new Date(s.t).getTime(),
            s.state === 'Discharge' ? 1 : null
          ]);
      - entity: sensor.power_store_schedule_chart
        name: Idle
        type: column
        color: '#95a5a6'
        opacity: 0.5
        show:
          in_header: false
          legend_value: false
        data_generator: |
          const schedule = entity.attributes.schedule || [];
          return schedule.map(s => [
            new Date(s.t).getTime(),
            s.state === 'Idle' ? 1 : null
          ]);
  - type: custom:apexcharts-card
    header:
      title: Source
      show: true
      show_states: false
    graph_span: 48h
    span:
      start: day
    now:
      show: true
      label: Now
      color: red
    apex_config:
      chart:
        height: 150px
        stacked: true
      plotOptions:
        bar:
          columnWidth: 100%
      legend:
        show: true
      yaxis:
        - show: false
          min: 0
          max: 1.1
    series:
      - entity: sensor.power_store_schedule_chart
        name: Internal
        type: column
        color: '#95a5a6'
        opacity: 0.5
        show:
          in_header: false
          legend_value: false
        data_generator: |
          const schedule = entity.attributes.schedule || [];
          return schedule.map(s => [
            new Date(s.t).getTime(),
            s.source === 'internal' ? 1 : null
          ]);
      - entity: sensor.power_store_schedule_chart
        name: User Override
        type: column
        color: '#3498db'
        opacity: 0.9
        show:
          in_header: false
          legend_value: false
        data_generator: |
          const schedule = entity.attributes.schedule || [];
          return schedule.map(s => [
            new Date(s.t).getTime(),
            s.source === 'override' ? 1 : null
          ]);
```

### ApexCharts — Solar Forecast + Price

Solar forecast and electricity price on a single chart:

```yaml
type: custom:apexcharts-card
header:
  title: Emaldo Solar & Price Tables
  show: true
  show_states: false
graph_span: 48h
span:
  start: day
now:
  show: true
  label: Now
  color: red
apex_config:
  chart:
    height: 300px
  legend:
    show: true
  yaxis:
    - id: solar
      decimalsInFloat: 0
      title:
        text: Solar (W)
    - id: price
      opposite: true
      decimalsInFloat: 1
      title:
        text: c/kWh
series:
  - entity: sensor.power_store_schedule_chart
    name: Solar Forecast
    type: area
    yaxis_id: solar
    stroke_width: 1
    opacity: 0.3
    color: orange
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      return schedule.map(s => [new Date(s.t).getTime(), s.solar * 10]);
  - entity: sensor.power_store_schedule_chart
    name: Price
    type: line
    yaxis_id: price
    stroke_width: 2
    color: '#3498db'
    show:
      in_header: false
      legend_value: false
    data_generator: |
      const schedule = entity.attributes.schedule || [];
      return schedule.map(s => [new Date(s.t).getTime(), s.price]);
```

### Mini Graph — Real-time Power

```yaml
type: horizontal-stack
cards:
  - type: sensor
    entity: sensor.emaldo_battery_battery_soc
    name: Battery
    icon: mdi:battery
  - type: sensor
    entity: sensor.emaldo_battery_battery_power
    name: Battery Power
    icon: mdi:battery-charging
  - type: sensor
    entity: sensor.emaldo_battery_grid_power
    name: Grid
    icon: mdi:transmission-tower
  - type: sensor
    entity: sensor.emaldo_battery_load_power
    name: Load
    icon: mdi:home-lightning-bolt
```

## Troubleshooting

### "Authentication failed"

- Verify email and password work in the Emaldo app.
- Check that App ID, App Secret, and App Version match the installed APK.

### "No battery devices found"

- The Home ID may be wrong. Leave it empty to auto-detect.
- Log in to the Emaldo app and verify your battery appears.

### "Failed to read E2E overrides"

- E2E communication uses UDP — ensure no firewall blocks outbound UDP to the Emaldo E2E server (port 1050).
- The integration retries up to 3 times with increasing delay (60s, 120s, 180s).
- Override reading failures don't block schedule updates.

### Schedule not updating

- Check the schedule polling options: default is every 2 hours starting at 14:00.
- Force a refresh: call `emaldo.refresh_schedule` from Developer Tools → Services.
- Check logs for `emaldo` entries.

## Architecture

```
emaldo/
├── __init__.py              # Entry setup, platform forwarding, options listener
├── calendar.py              # Battery schedule calendar entity
├── config_flow.py           # Config + options + reconfigure flow
├── const.py                 # Integration constants and defaults
├── coordinator.py           # Power/battery data coordinator (60s polling)
├── number.py                # EV fixed charge amount number entity
├── schedule_coordinator.py  # Schedule + override coordinator (custom time triggers, E2E retry)
├── select.py                # Control priority + EV charge mode select entities
├── sensor.py                # 7 power sensors + 3 schedule sensors + 1 balancing state sensor
├── services.py              # set_slot_range, apply_bulk_schedule, reset_to_internal, refresh_schedule
├── services.yaml            # Service UI descriptions
├── strings.json             # Translation strings
└── emaldo_lib/              # Bundled Emaldo client library
    ├── __init__.py           # Re-exports EmaldoClient + exceptions
    ├── client.py             # REST API client (login, get_battery, get_power, etc.)
    ├── const.py              # App params, API endpoints, slot encoding/decoding
    ├── crypto.py             # Encryption utilities
    ├── e2e.py                # E2E encrypted UDP communication
    └── exceptions.py         # EmaldoError, EmaldoAuthError, etc.
```
