# Changes

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
