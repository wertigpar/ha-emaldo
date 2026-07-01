# Changes

## v1.0.0-beta13g

### Fixed
- **Realtime stream could wedge permanently after a cloud-API outage and only
  recover on an HA restart:** in stream mode the background receiver's in-place
  reconnect was the *only* recovery path, and it refreshes credentials through
  the shared REST client. When `api.emaldo.com` had a transient outage (users
  report these around 01:00–02:00), the shared REST token could be left dead;
  every subsequent in-thread re-handshake then reused stale credentials and the
  stream wedged indefinitely — a `long_stall` reconnect storm with frames
  frozen and `success_rate_recent_pct` stuck at 0 until Home Assistant was
  restarted. The coordinator's heavier "big hammer" recovery (`_reset_client()`
  → clean REST re-login → full session rebuild with genuinely fresh
  credentials) was never reachable in stream mode because the empty-read path
  short-circuited unconditionally. The coordinator now escalates to that full
  reset after no fresh frame for `STREAM_STALL_FULL_RESET_SECONDS` (180 s,
  well above the 45 s long-stall watchdog so a healthy in-place self-heal never
  triggers it). A new `stream_stall_reset` reconnect reason records each
  escalation in the diagnostic sensor.

## v1.0.0-beta13f

### Fixed
- **Battery range markers can no longer expose values >100 (#45):** the override
  read returned the raw marker bytes (`payload[0]`/`payload[1]`) directly, so a
  corrupt byte — seen historically during 21204 reconnect recovery — surfaced as
  a battery-range number entity above 100 % and made `apply_bulk_schedule` fail
  with `value must be at most 100 for ... 'low_marker'` / `'high_marker'`.
  `parse_overrides` now rejects a frame whose markers fall outside 0–100,
  treating it as corrupt so the coordinator keeps the last good values instead
  of publishing an invalid one. (The beta13e stream fixes already made the
  triggering reconnect storm rare; this adds the missing defensive validation.)

## 1.0.0-beta13e

### Added
- **Rolling "recent" success rate (`success_rate_recent_pct`):** the existing
  `success_rate_pct` is cumulative over the lifetime of the integration run —
  it only ever falls and is reset only by a restart, so a brief early outage
  permanently drags it down and a restart masks an ongoing problem. The
  realtime connection diagnostic now also reports `success_rate_recent_pct`
  computed over the most recent `REALTIME_SUCCESS_WINDOW` polls (240 ≈ a
  20-minute window at the 5 s cadence), plus `success_rate_window` (the number
  of samples currently in the window). This reflects *current* health and
  recovers once an outage clears.

### Fixed
- **Reconnect backoff no longer stalls reads (non-blocking backoff):** the
  stream receiver previously ran `time.sleep(backoff)` while holding the
  session lock, so an escalating backoff (up to the 30 s ceiling) blocked every
  concurrent power-flow read for the full wait, accumulating empty reads. The
  backoff is now served by the receiver loop's 0.1 s poll-sleep via a monotonic
  deadline (`_stream_reconnect_not_before`); the lock is released between
  iterations, so reads stay responsive throughout the wait.
- **Reconnect backoff streak resets on a successful handshake:** the escalation
  streak previously reset only when a frame arrived, so a relay that accepted
  the handshake but expired the session before the first frame could ratchet
  the backoff all the way to 30 s. The streak now also resets on a successful
  handshake, so escalation only grows across genuinely failed handshakes and a
  single competing read can no longer push the stream into 30 s stalls.

## 1.0.0-beta13d

### Changed
- **Realtime power flow switched to a subscribe-and-stream model
  (`REALTIME_STREAM_MODE`):** packet captures of the official app show that
  power flow (0x30) is a device *push* stream — the app subscribes once, the
  device pushes a burst of frames, and the app re-subscribes ~every 15 s. The
  previous poll model (send 0x30, read one frame, sleep 5 s) raced the relay's
  power-flow session TTL and produced the recurring 21204 "session expired"
  storm that capped realtime success around 81%. The persistent E2E session now
  runs a background receiver thread that subscribes once, continuously drains
  the pushed stream (caching the freshest frame), and re-subscribes +
  keepalives on the app's cadence. The coordinator poll just reads the cached
  frame. Set `REALTIME_STREAM_MODE = False` in `const.py` to revert to the
  legacy poll model.

### Fixed
- **Realtime success rate raised from ~81–87 % to ~99 % by stopping a
  `chat_secret` rotation collision:** the device `chat_secret` is rotated
  server-side on every `e2e_login`. The periodic REST/EV reads called
  `e2e_login` directly, rotating the secret out from under the live realtime
  UDP stream, which then expired (21204) and re-handshaked with the now-stale
  secret — a self-perpetuating 21204 storm. All E2E consumers now share one
  cached credential generation via `EmaldoClient.get_e2e_credentials()`
  (10-minute TTL), and the streaming session takes a credential-refresh
  callback so an in-place reconnect picks up a rotated secret instead of
  re-handshaking with a dead one. The remaining single startup 21204 now
  recovers cleanly with no storm.
- **Diagnostic sensor now surfaces the last read exception
  (`last_read_error`):** deterministic read failures used to be visible only in
  the Home Assistant log; the exact exception type/message is now exposed as a
  realtime connection attribute for faster diagnosis.

## 1.0.0-beta13c

### Fixed
- **Battery module sensors assigned to wrong slots intermittently (#44):** when a
  module responded slowly (past `probe_timeout`), its UDP packet could sit in the
  socket buffer and be received during the *next* slot's probe window. The next
  slot then got the previous module's data with an incremented `scan_index`,
  producing a cascading +1 shift (module 5 shows module 4 values, module 6 shows
  module 5 values, …) and a phantom extra module at the end. Fixed by draining
  any pending UDP datagrams from the socket immediately before each slot probe,
  so late arrivals from earlier probes are discarded rather than misassigned.
- **Power Store device badge showed Battery Module 1 SoC instead of whole-system
  Battery SoC:** per-module `soc` sensors had `device_class=SensorDeviceClass.BATTERY`,
  which caused Home Assistant to pick the alphabetically-first module sensor
  (`battery_module_1_soc`) as the device badge instead of the system-wide
  `battery_soc` sensor. Removed `device_class=BATTERY` from per-module SoC
  sensors; the system-wide `battery_soc` is now the sole battery-class entity
  on the device and displays correctly in the top-right of the Power Store page.

## 1.0.0-beta13b

### Fixed
- **Manual selling target no longer overwritten by polling (#42):** setting the
  manual selling target now stages the user's intended value instead of writing
  it to the firmware immediately. Previously, setting a target before enabling
  manual selling was reset by the firmware and then overwritten by the next
  coordinator poll, so the session could start with the wrong amount (e.g. 1 kWh
  instead of 2 kWh). A separate `manual_selling_intended_target` is now tracked,
  preferred by the target number and the switch's start command, and never
  clobbered by polling until a selling session is actually active.
- **Battery module entity history preserved across the slot-ID change (#43):**
  the slot-based per-module `unique_id` introduced in beta12a now migrates
  existing serial-based entities in the registry instead of orphaning them.
  History, statistics, dashboards and automations referencing those sensors are
  retained. The migration matches by module serial and runs once a battery scan
  reveals the serial-to-slot mapping.

## 1.0.0-beta13

### Changed

Default App Version 2.8.6

## 1.0.0-beta12f

### Changed
- **Non-blocking startup:** the realtime keepalive loop and the battery-module
  scan are now started as Home Assistant config-entry background tasks instead
  of bootstrap tasks. Home Assistant no longer waits for the E2E handshake or
  the cabinet scan during the "Wrapping up" phase, so "Home Assistant has
  started" appears promptly after a restart.
- **Faster battery-module scan:**
  - Per-probe timeout reduced to 1.5 s (the initial handshake still uses the
    full 5 s), so probing empty cabinet slots no longer stalls for the full
    read timeout.
  - After the first full discovery the installed slots are cached and
    re-scanned directly (no tier walk); a full rediscovery runs periodically
    (every 6th scan) to pick up added or removed modules.
  - The tier abort now stops only the current tier instead of the whole scan,
    so a second cabinet whose modules start at a higher physical slot index is
    still discovered even when the lower slots are empty.

### Fixed
- **Realtime/E2E sensors no longer flash "unavailable" on restart:** the
  realtime power-flow, balancing and per-module battery sensors now restore
  their last value (`RestoreSensor`) and serve it through the cold-start E2E
  handshake window instead of dropping out. The previous write-suppression
  (which left the entities stateless until the first read) has been removed.
  - Note: a restored value is only available once a run with this build has
    persisted state at least once, so the very first restart after updating may
    still show no value until the first successful E2E read completes.
- **Power-flow drain now handles non-timeout socket errors gracefully:** a
  connection reset or concurrently closed socket during the power-flow drain
  loop no longer propagates a raw socket traceback. The drain raises a typed
  `EmaldoE2EError` so the coordinator tears the session down and reconnects on
  the next poll, and the `settimeout` restore is guarded against a broken
  socket. Added a `drain_socket_error` diagnostic counter.

## 1.0.0-beta12e

### Fixed
- **Realtime power-flow success rate (poll cadence vs. relay session window):**
  the realtime power-flow poll interval was reduced (10 s → 8 s → 5 s) so reads
  land inside the relay's live power-flow session window.
  - Diagnostics showed failures occur at `age_since_handshake ≈ poll_interval`
    (10 s poll → ~9.6 s, 8 s poll → ~7.6 s, 5 s poll → ~4.6 s) while
    `age_since_keepalive` is uncorrelated — i.e. keepalive packets do NOT
    refresh the power-flow data session; only 0x30 reads (or a handshake)
    re-arm it. Polling below the window chains reads inside the live session.
  - Success rate improved 60% → 68% → ~75% across 10 s → 8 s → 5 s.
  - **Remaining failures are relay/device-side, not client-fixable:** logs show
    failures arrive in ~30 s bursts every ~1-3 min where the relay returns
    21204 even to an immediate read on a brand-new handshake (`age ≈ 44 ms`),
    while healthy sessions otherwise live 60-240 s. This is the device rotating
    its relay session; faster polling shortens the recovery storms (52 s → 30 s)
    but cannot read data the relay is not serving. ~75% is the practical ceiling.
  - During these bursts the realtime sensors retain their last successful values
    and stay available (the coordinator returns the last reading rather than
    raising `UpdateFailed`), so the dashboard does not flap to unavailable.

### Added
- **Realtime E2E diagnostics expanded for root-cause analysis:** the realtime
  connection diagnostic sensor now exposes reconnect and keepalive failure
  cause breakdown plus UDP round-trip-time (RTT) telemetry.
  - New reconnect diagnostics include `last_reconnect_reason` and
    `reconnect_reasons` counters (for example `empty_reads`, `read_error`,
    `auth_expired`, keepalive-driven reconnect causes).
  - New keepalive diagnostics split failures into
    `keepalive_failures_session_expired`, `keepalive_failures_closed`,
    `keepalive_failures_exception`, and `keepalive_failures_other`.
  - New RTT diagnostics include `e2e_rtt_last_ms`, `e2e_rtt_avg_ms`,
    `e2e_rtt_min_ms`, `e2e_rtt_max_ms`, and `e2e_rtt_samples`.

- **E2E power-flow read packet diagnostics:** added granular counters to
  distinguish whether empty reads are caused by initial timeouts, session
  expiry on the first response, non-matching first packet, drain packet loss,
  or drain exhaustion. These counters appear in the realtime connection
  diagnostic sensor as ``powerflow_initial_*`` and ``powerflow_drain_*``
  attributes.

- **21204 timing diagnostics:** added debug timing context for session-expired
  reads and reconnect attempts, including age since last handshake, age since
  last keepalive, 21204 stage (initial/drain), and reconnect completion time.
  This makes startup and steady-state relay expiry patterns directly visible in
  Home Assistant debug logs.

- **21204 reconnect behavior refined:** after a 21204, the session is
  re-handshaked and power-flow read is deferred to the next scheduled poll
  instead of issuing an immediate same-poll retry.
  - This removes an ineffective retry path that repeatedly hit 21204 within
    ~40 ms after reconnect and increased relay/session churn.
- **Session close made lock-safe:** `PersistentE2ESession.close()` now acquires
  the session lock before closing the socket, reducing keepalive race windows
  that produced `Bad file descriptor` / `NoneType` keepalive errors.

- **Empty-read reconnect deferral (Phase 2a):** when the keepalive task is
  healthy and no read errors have occurred, defer reconnect attempts for up to
  3 consecutive empty reads. This reduces unnecessary reconnect churn during
  transient relay issues.
- **Reduced unnecessary reconnects during healthy-session empty-read bursts:**
  realtime polling now defers a limited number of reconnects when the empty
  read threshold is reached but keepalive has been recently healthy.
  - This mitigates reconnect churn caused by short transient read gaps while
    preserving self-healing behaviour for real session failures.
  - Added diagnostic counter
    `empty_reconnect_deferrals_healthy_keepalive` to show how often this guard
    path is used.

## 1.0.0-beta12d

### Fixed
- **Reduced reconnect churn during short E2E relay blips:** when realtime
  polling reaches 3 consecutive empty reads, the coordinator now performs one
  final immediate probe before closing the session.
  - If that probe succeeds, the session is kept alive and reconnect is
    avoided.
  - New diagnostic counters were added to the realtime connection sensor:
    `reconnect_probes` and `reconnects_avoided`.
- **Reduced noisy SSL EOF retry warnings from underlying HTTP stack:** the
  REST client now disables urllib3 transport-level "other" retries (with
  backward-compatible fallback for older urllib3 builds).
  - Transient connection handling remains explicit in the coordinator
    (reset+single retry, then cached-data fallback), but avoids duplicate
    lower-level retry noise.
- **Midnight/day-rollover REST gaps no longer emit `unknown` for daily energy sensors:**
  when Emaldo day-series endpoints temporarily return an empty `data` list,
  daily totals now resolve to `0.0` kWh instead of `None`.
  - This prevents downstream Utility Meter warnings like "received invalid new
    state ... unknown" during rollover windows.
  - Empty MPPT/string series are now treated consistently the same way.
- **Short partial REST payloads no longer blank non-total sensors:** for
  non-total REST sensors (for example SoC), the entity keeps its last valid
  value when the coordinator update succeeds but that specific field is
  temporarily missing.
- **Battery module background scan is now more resilient to transient cloud login drops:**
  the standalone module scan retries one time after resetting the shared
  client when E2E login fails with a connection error.
  - Expected transient connection failures in this background path are now
    logged as concise debug lines without full traceback spam, while cached
    module data is retained.

## 1.0.0-beta12c

### Fixed
- **Transient cloud network drops no longer force Emaldo sensors unavailable:**
  intermittent HTTPS disconnects (for example `Remote end closed connection
  without response` / `Max retries exceeded`) in the slow REST poll path now
  use a more resilient recovery flow.
  - On `EmaldoConnectionError`, the coordinator now retries once with a
    freshly reset shared client/session.
  - If the retry also fails and a prior successful payload exists, the
    coordinator keeps the previous data instead of raising `UpdateFailed` for
    that cycle.
  - This prevents brief upstream/network blips from propagating as temporary
    entity unavailability and downstream "SoC could not be read" errors in
    dependent automations/integrations.
  - Long-running outages are still surfaced via throttled warning/error logs,
    and normal updates resume automatically when connectivity recovers.
- **Transient E2E override read failures no longer raise a misleading warning:**
  when the schedule coordinator's override-only retry path hits short-lived
  E2E issues (for example relay session expiry / status `21204`), it now uses
  the same targeted handling as the main override fetch.
  - Session-expired retries now invalidate cached E2E credentials before the
    next retry so the following attempt can perform a clean E2E re-login.
  - Timeout/protocol/generic E2E retry failures are treated as benign
    transient misses and do not escalate the whole coordinator state.
  - If schedule data is otherwise healthy, exhausted override retries are now
    logged at `INFO` instead of `WARNING`, because the integration continues
    serving the last known override/schedule state until the next successful
    poll.

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
