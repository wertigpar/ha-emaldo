# Changes

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
