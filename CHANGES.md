# Changes

## 1.0.0-beta12b

### Fixed
- **Realtime sensors flash to `unknown` on HA restart, then recover after 30–60 s:**
  two root causes were identified via instrumented debug logs:
  1. **Initial state write during `async_add_entities`:** When HA loads the
     sensor platform it calls `async_write_ha_state()` on every entity
     (including realtime sensors whose coordinator has no data yet —
     `data is None`). This initial write is **not** routed through
     `_handle_coordinator_update`, so a guard there alone is insufficient.
     The entity's `native_value` returns `None`, which HA displays as
     "unavailable", overwriting the value that was just restored from the
     previous session's state.
     - All realtime coordinator entity classes (`EmaldoSensor`,
       `EmaldoBalancingStateSensor`, `EmaldoBatteryTotalEnergySensor`,
       `EmaldoBatteryModuleSensor`) now override `async_write_ha_state()`
       to suppress the write until `EmaldoRealtimeCoordinator` records its
       first successful E2E read (`_successful_first_refresh` flag). This
       guards **both** the initial setup write and coordinator-driven
       updates through a single choke point.
  2. **Battery module scan blocks the first power-flow delivery for ~54 s:**
     The 0x06 battery-module scan (probing up to 13 cabinet slots with 5 s
     timeouts each) ran synchronously inside `_async_update_data`. On cold
     start the poll counter is set to trigger the scan on the first
     successful read, so even though valid power-flow data was received
     within ~4 s, the coordinator could not notify listeners until the
     scan finished ~54 s later.
     - The battery module scan now runs in a **background task**
       (`_async_scan_battery_modules`), so `_async_update_data` returns
       power-flow data immediately. The scan updates
       `_battery_modules` / `_battery_module_slots` and calls
       `async_update_listeners()` when done.
  - Steady-state behaviour (transient failures keeping last known values) is
    unchanged.
  - Recovery time after HA restart is now dominated by the E2E handshake
    alone (~4–10 s), instead of handshake + battery scan (~58 s).

## 1.0.0-beta12a

### Fixed
- **Battery module sensors map to physical slots instead of serials (#23):**
  HP5000 systems return per-module battery info unreliably — not all modules
  answer every poll, and the responding modules come back in varying order.
  The previous serial-based sensor keys caused sensors to flip to
  `unavailable` and made "Module N" labels drift across polls.
  - Battery module sensors are now keyed by the **scan slot index** (physical
    cabinet position) rather than the module serial number.
  - Each cabinet slot gets one stable sensor per metric; the sensor value
    reflects whichever module currently occupies that slot.
  - The module serial number is exposed as a state attribute instead of being
    part of the unique ID.
  - `parse_battery_data` / `read_battery_info` now carry the scan slot index
    through to the coordinator, and the coordinator stores modules in a
    `battery_module_slots` dict keyed by that index.
  - Fixed the standalone `read_battery_info` helper (used by the coordinator's
    dedicated battery scan) so it also tags each module with `scan_index`.
    Previously only the persistent-session method added the field, leaving the
    slot-based sensor lookup empty.

## 1.0.0-beta11j

### Fixed
- **Battery module sensors missing on HP5000 systems (#23):** the battery-info
  scan (E2E type 0x06) used a hardcoded 250-byte minimum response size to skip
  empty cabinet slots.  HP5000 firmware returns valid battery responses at
  ~243 bytes, which fell below the threshold and were silently discarded as
  "empty slots".  The magic-size filter has been replaced with a protocol-aware
  approach: only responses shorter than 50 bytes (below the AES framing
  overhead) are skipped without decryption; everything else is passed to the
  existing decrypt → `HEADER_BATTERY` check → `parse_battery_data` pipeline so
  the protocol header decides validity, not a device-model-dependent size.
- **Bootstrap setup timeout waiting for Emaldo:** `async_setup_entry` no longer
  awaits the realtime or schedule coordinator first refreshes on the startup
  path. Both coordinators are started as config-entry background tasks, so the
  E2E UDP handshake and override reads cannot block HA bootstrap for tens of
  seconds. The primary REST power coordinator is still awaited so core sensors
  are ready before platforms are forwarded.
- **Spurious "Keepalive failed twice" warning:** closing the E2E session after
  two consecutive keepalive failures is the designed self-healing path (the
  next `read_power_flow` re-handshakes in place). This message is now logged at
  INFO instead of WARNING so it is not reported as an integration problem.

## 1.0.0-beta11i

### Fixed
- **Persistent E2E connection failures (#37):** the realtime power-flow layer
  no longer tears the whole UDP session down every few polls when the relay
  reports the session as expired (status 21204). The persistent session now
  re-handshakes in place — with a short backoff — and retries the read, so a
  short or aggressive relay TTL no longer escalates into the
  "persistent connection failure after 3 reconnect cycles" warning that
  accumulated thousands of drops.
  - `PersistentE2ESession.keepalive` returns `False` immediately when the
    relay replies with status 21204 (session expired) — keeping the keepalive
    task fast and non-blocking so it never stalls the HA bootstrap watchdog.
    The existing `fail_count >= 2 → close session` path in the keepalive loop
    tears down the dead socket, and the next `read_power_flow` call rebuilds
    the session in place with a short backoff.
  - `PersistentE2ESession.read_power_flow` reconnects on 21204 and retries the
    0x30 request once, instead of returning `None` and waiting for the
    coordinator to fully recreate the session (3 REST calls + handshake) on
    the next cycle.
- **Battery-info scan no longer starves the realtime keepalive:** the periodic
  per-module battery scan now runs on a dedicated one-shot E2E session instead
  of the persistent realtime socket. The 0x06 scan probes up to 13 cabinet
  slots and previously held the realtime session lock for tens of seconds,
  starving the 7 s keepalive task and letting the relay drop the realtime
  session — a direct contributor to the #37 reconnect storm.
- **Recovery logging stuck high:** `_consecutive_reconnects` is now reset as
  soon as a clean read lands, so the recovery INFO message fires reliably
  (previously it was gated on a threshold higher than the reset condition and
  could stay suppressed after connectivity returned).
- **Misleading "total drops" counter:** warning/recovery messages now report a
  per-episode drop tally (`episode drops`) that resets when a clean read
  succeeds. The monotonic lifetime `stats_empty_reads` is preserved unchanged
  for the diagnostics sensor. Sanity-filtered reads no longer count toward the
  drop/reconnect tallies.

### Changed
- **Keepalive cadence/TTL documentation reconciled:** the `PersistentE2ESession`
  docstrings and `DEFAULT_KEEPALIVE_INTERVAL` no longer contradict each other
  and the coordinator constant (`KEEPALIVE_INTERVAL = 7`, relay TTL ~10 s).
  All references now consistently document a ~10 s relay TTL with a 7 s
  keepalive cadence, which must stay below the read interval.

### Notes
- The trigger for #37 was a relay/firmware-side change that started dropping
  E2E sessions faster around 2026-06-16/17; this release makes the
  integration resilient to a short relay TTL instead of depending on it.

## 1.0.0-beta11h

### Fixed
- **HP5000 battery module discovery:** battery-info scans no longer stop when
  the first cabinet index tier returns only short empty-slot replies. The scan
  now continues across all known module indices (0-12) and still aborts only on
  repeated true timeouts, allowing systems whose valid modules start at later
  indices to create their per-module sensors (#23).

## 1.0.0-beta11g

### Added
- **Battery module discovery debug logging:** added detailed debug-level
  tracing around E2E battery-info scans and coordinator module polling to help
  diagnose missing HP5000/Power Core module sensors. Logs now show probed
  indices, response lengths, timeouts, parse results, duplicate serial skips,
  returned module counts and cached-module retention decisions (#23, #37).

### Fixed
- **Debug logging capture:** declared the integration logger namespace in the
  manifest and added setup-time debug marker lines so Home Assistant's
  "Enable debug logging" action has immediate `custom_components.emaldo`
  output to capture before the slower battery module discovery poll runs.

## 1.0.0-beta11f

### Fixed
- **Realtime sanity check no longer blocks valid power-flow updates on
  single-inverter systems:** coordinator-side payload rejection now validates
  only realtime channels that are actually consumed by Home Assistant entities
  (`battery_w`, `solar_w`, `grid_w`, `dual_power_w`, `ev_w`). Unused/raw
  channels (`ip2_w`, `op2_w`, `addition_load_w`, `other_load_w`) remain
  available for diagnostics but no longer freeze all realtime sensors when
  they contain outlier values (#38).

## 1.0.0-beta11e

### Fixed
- **Solar energy today now combines all solar sources:** `solar_energy_today`
  now sums internal MPPT string channels (1-3) plus the third-party PV channel
  when available. This makes totals correct for internal-only, external-only,
  and mixed installations (#35).
- **Backfill solar aggregation aligned with live sensor semantics:**
  `backfill_solar` now uses explicit solar columns (string1+string2+string3+
  third-party) instead of summing all row columns, avoiding accidental
  double-counting or inclusion of non-power columns.

### Added
- **Third-party solar energy today sensor:** new
  `thirdparty_solar_energy_today` daily kWh sensor exposing only external PV
  contribution from the mppt-v2 series.

## 1.0.0-beta11d

### Fixed
- **Daily energy sensors reverted to `total` state class with midnight reset:**
  `battery_charged_today`, `battery_discharged_today`, `solar_energy_today`
  (and per-string), `grid_import_today`, `grid_export_today` and
  `load_energy_today` switched back from `total_increasing` to `total` with
  `last_reset` set to local midnight. The Emaldo API recomputes today's energy
  totals from scratch on every poll (summing 5-minute power rows), so minor
  rounding-boundary fluctuations (e.g. 7.80 → 7.79 kWh) caused HA recorder
  warnings about the `total_increasing` contract being violated. Using `total`
  with a `last_reset` attribute is the semantically correct class for
  daily-reset derived accumulators and suppresses these warnings (#32).
  - Note: Home Assistant may show a one-time "state class changed" repair
    notice for these entities after upgrading; this is expected.

## 1.0.0-beta11c

### Changed
- **Daily energy sensors now use `total_increasing` state class:**
  `battery_charged_today`, `battery_discharged_today`, `solar_energy_today`
  (and per-string), `grid_import_today`, `grid_export_today` and
  `load_energy_today` switched from `total` (with `last_reset` at local
  midnight) to `total_increasing`. This is the recommended Home Assistant
  contract for meters that climb during the day and reset to zero, and it lets
  downstream cost integrations (e.g. Dynamic Energy Cost) natively ignore the
  midnight reset instead of registering a large negative delta (#31).
  - For a non-resetting lifetime total, use Home Assistant's built-in
    **Utility Meter** helper pointed at the relevant daily sensor with the
    reset cycle set to "No reset".
  - Note: Home Assistant may show a one-time "state class changed" repair
    notice for these entities after upgrading; this is expected.

## 1.0.0-beta11b

### Fixed
- **Solar energy today on non-Power-Core models:** the `Solar energy today`
  sensor now reads the device's pre-summed `pv_total_W` column (falling back
  to the legacy single-channel column) instead of summing the per-string MPPT
  columns. Models without internal MPPT leave the per-string columns at zero,
  which previously made the sensor report `0.00 kWh`; it now matches the CLI
  `solar` command's "Total" value (#29).

## 1.0.0-beta11

### Added
- **Total Energy sensor:** new realtime sensor summing the stored energy of
  all battery modules (mirrors the CLI `battery-detail` "Total Energy" line),
  with combined maximum capacity exposed as an attribute.
- **Expanded battery cell diagnostics:** per-module sensors now also cover
  Model, Current, Cycles, Stored Energy, Maximum Capacity, Cell A/Cell B
  temperatures and Nominal Capacity, in addition to the existing SoC, SoH,
  Temperature, Voltage and Serial.

### Fixed
- **Battery energy scaling:** module stored energy and maximum capacity are now
  decoded from 0.5 Wh ticks, correcting previously halved Wh readings.

### Changed
- Translations updated (en, da, fi, nb, sv) for the new Total Energy and battery
  module diagnostic sensors.

## 1.0.0-beta10b

### Fixed
- **Dual config-entry setups (two batteries, same account):** the primary
  coordinator no longer unconditionally forces legacy `home_id`-based unique
  IDs. Legacy mode is now detected per config entry by checking whether
  `{home_id}_battery_soc` already exists in that entry's entity registry, so
  two entries sharing the same `home_id` no longer generate identical unique
  IDs that leave one battery's entities unavailable (#26).

## 1.0.0-beta10

### Fixed
- **Shared cloud login across matching config entries:** REST authentication is
  now shared per account/app tuple so legacy multi-entry setups do not churn
  the Emaldo cloud token and immediately expire each other (#24).
- **Battery range auth retry:** battery-range writes now invalidate the actual
  shared REST client before retrying instead of clearing an unused placeholder.
- **Realtime power sensor spikes:** implausible `0x30` payload values are now
  rejected before decoding so corrupted E2E packets do not publish multi-MW
  battery/grid/solar/EV readings (#22).
- **Realtime second-stage guard:** coordinator-side sanity filtering now drops
  out-of-range/non-numeric realtime power channels and keeps last-known values
  instead of publishing bad states.
- **API credential scoping:** HTTPS app id/secret handling is now client-local
  instead of global mutable state, reducing cross-account race risk.
- **E2E app-id compatibility:** shared runtime client now keeps E2E app-id
  parameters synchronized for packet builders that still depend on
  `emaldo_lib.const` global app-id.
- **Auth-retry cleanup:** removed remaining legacy internal `_client` shim
  mutation patterns in coordinators in favor of explicit reset helper flows.
- **Multi-device groundwork (phase 1):** config flow now supports optional
  explicit `device_id` binding and the coordinator persists resolved
  `device_id/model/name` in entry data for stable per-device anchoring.
- **E2E scoping decision:** persistent realtime E2E sessions are now explicitly
  device-scoped and recreated if the bound `(home_id, device_id, model)`
  changes.
- **Multi-device fan-out (phase 2):** one config entry can now create
  per-device power/realtime/schedule coordinator sets for all discovered
  batteries (unless a specific `device_id` is pinned), and entity unique IDs
  are device-scoped to avoid collisions.
- **UID compatibility restore:** primary-device entities keep legacy
  home-based unique IDs to prevent single-device entity_id churn; additional
  fan-out devices remain device-scoped.
- **Service device targeting:** override/schedule/EV/backfill/battery-range
  services now accept optional `device_id` to route operations to a selected
  device in multi-device entries; omitting it keeps legacy primary-device
  behavior.
- **Unit test coverage:** added pure unit tests for service dispatch helper
  selection and routing behavior for `set_slot_range`, `set_ev_schedule`,
  `set_battery_range`, and `refresh_schedule` (default-primary,
  explicit-`device_id`, and unknown-device validation).

## 1.0.0-beta9

### Added
- **Per-string solar energy sensors** (integrated-MPPT models, e.g. Power Core):
  `Solar string 1/2/3 energy today` — daily kWh totals broken out per MPPT
  string, alongside the existing combined `Solar energy today` sensor (#20).
- Restored the **Battery Module N Serial** diagnostic sensors.

### Fixed
- **`Solar energy today` double-counting:** the daily total now sums only the
  three per-string columns of the `mppt-v2` series instead of also adding the
  pre-summed total and state columns.

### Notes
- The new per-string sensors are created only on models with integrated MPPT;
  they do not appear on Power Store (external solar).
