# Changes

## v1.0.0-beta17

### Changed (schedule polling: faster, simpler, fixes active_mode/plan_source lag)

- Removed `Start hour` and `Start minute` schedule-polling config options.
  The 2-hour default was an over-correction: the integration already polls
  power flow every 5s (720 REST-ish calls/hr/device), so one extra schedule
  REST call every few minutes is negligible API load.
- `Repeat interval` (schedule refresh) is now clamped to **60-600s** with a
  default of **600s (10 min)**. This also catches external (app-side) override
  changes within 10 min instead of up to 2 hours.
- Added a **1-minute slot-boundary recompute ticker** to
  `EmaldoScheduleCoordinator`: it re-pushes the cached `self.data` via
  `async_set_updated_data` with **zero network cost**, so the time-derived
  `active_mode` (Swedish: aktivt läge) and `plan_source` (plan källa) sensors
  recompute on every 15-min slot crossing within ~1 min. Fixes the reported
  up-to-9-min (worst case 2-hour) staleness when idle/charge/discharge or
  internal/override flips.

## v1.0.0-beta16q

### Added (publish both power-flow load channels as realtime sensors)

- Both per-frame load channels are now exposed as realtime W sensors:
  - `addition_load_power` ("Additional load power") ← `addition_load_w`
  - `other_load_power` ("Other load power") ← `other_load_w`
- Wire values report load as a negative sink from the home POV; both
  sensors flip the sign so they read as positive loads (consistent with
  the existing `Consumption` and `Car charge power` sensors).
- `realtime_sanity.py`: moved both load keys from the non-blocking AUX
  set into `REALTIME_POWER_ENTITY_KEYS`, so a spike on one channel
  blocks only that channel (and not the other load channel or the whole
  payload).


### Fixed (PowerFlow reasonability-fail DEBUG flood)

- Root cause: `_stream_drain_locked` called `_try_parse_power_flow` on
  **every** datagram drained from the shared subscription socket, without
  first confirming the datagram was a 0x30 (`GET_GLOBAL_CURRENT_FLOW_INFO`)
  reply. Relay keepalive / notice / wake / heartbeat datagrams on that same
  socket were force-fed into the power-flow parser, producing the weekly
  "PowerFlow reasonability fail" DEBUG spam with bogus values
  (e.g. -16513, -18140, 15759, 6307, 20866, 25021).
- Fix: added `_is_0x30_reply(resp)` pre-filter on the raw outer METHOD TLV
  marker `82 f5 30`. Power-flow parsing now only runs on datagrams that
  actually carry that marker; all other datagrams fall through to the
  existing classify/drain path (correctly bucketed as keepalive/notice).
  No real power-flow data is lost — the dedicated 0x30 reads already
  contain the `82f530` marker.

## v1.0.0-beta16p

### Fixed (`reset_to_internal` crash — β16o regression, every call failed)

- **Symptom (user report, 2026-07-19, β16o):** "reset to AI is not working" —
  worse than β16n. HA log (home-assistant_emaldo_2026-07-19T17-14-00.999Z.log)
  showed `Error executing script ... Unexpected error for call_service`:
  `HomeAssistant.async_add_executor_job() got an unexpected keyword argument
  'device_id'` at `services.py:728` (and the loop at :755). Every
  `reset_to_internal` invocation threw `TypeError` before doing any work — the
  service was 100% broken, not merely flaky/re-firing.
- **Root cause:** β16o's `_reset_one_device` refactor called
  `hass.async_add_executor_job(_reset_one_device, hass, device_id=...,
  reset_all=..., ...)` — passing arguments as **keyword** args. HA's
  `async_add_executor_job(func, *args)` accepts only positional args; kwargs are
  rejected with `TypeError`. The rest of the codebase uses inline closures
  (`_do_override`, `_do_bulk`) passed bare to `async_add_executor_job` — β16o
  violated that convention.
- **Fix:** wrapped both call sites (single-device and the per-device loop) in
  local closures (`def _reset_job(): return _reset_one_device(...)`) and passed
  the bare closure to `async_add_executor_job`, matching `_do_bulk`. The
  β16o design (per-device result dict + read-back verify + persistent
  notification on failure) is otherwise retained.
- **Verification:** `python3 -m py_compile services.py` clean.
- **Files:** `services.py` (`async_handle_reset_to_internal` call sites),
  `manifest.json` (`1.0.0-beta16p`).

## v1.0.0-beta16o

### Fixed (`reset_to_internal` — silent fire-and-forget, blind re-fire under 21204)

- **Symptom (JanBaecklund β16n field log, 2026-07-19):** "reset to AI" command
  sometimes appeared to take no effect; eventually both PS1 (RYFOzJxpGrffGm1x)
  and PS2 (uVuOAKnVtrM5gUeN) reset. Log showed 8 top-level `reset_to_internal`
  calls in ~2m: two initial single-device calls 15s apart, then a ~17–20s
  re-fire cadence on each unit. Each fire hit `CONN_NOT_ESTABLISHED` because the
  E2E stream was mid-21204-reconnect, retried 1s later on a still-dead session,
  and landed only after the stream self-healed (β16n `long_stall` fix). The
  command *did* work — just serially, per-device, delayed — but the service
  returned no status, so the caller (automation / repeated button) kept firing.
- **Root cause:** `async_handle_reset_to_internal` ran the 3-attempt loop
  fire-and-forget and returned nothing. The caller could not distinguish
  "succeeded" from "delayed by stream churn", so any recurring trigger re-fired
  blindly. The override path also used `_reset_client()` (device-only refresh)
  which does NOT force home-secret rotation — correct for dual-unit safety
  (#47 / RC1–RC5), but it means an override send during a 21204 death cycle
  cannot recover until the stream escalates on its own.
- **Fix (atomic + visible, NOT forced home rotation):** refactored the per-device
  work into `_reset_one_device()` returning `{device_id, success, reason}`.
  - When called **without** `device_id`, it now iterates every device, awaits
    each result, and logs a summary. On any failure it raises a
    `persistent_notification` ("reset to AI partially failed" + per-device
    reason) so the caller stops re-firing and sees the outcome.
  - Added `_override_readback_ok()`: after a relay ACK, the override is read
    back via `get_overrides` and compared to the intended slots. A relay ACK
    that didn't clear state is caught and retried. Best-effort: a read failure
    is treated as success (never masks a real write).
  - Preserved the existing device-only refresh + legacy one-shot fallback
    (#47 Option C). **No change to the credential-escalation / ping-pong guard**
    — home-secret rotation is still only escalated by the stream ladder, so
    dual-unit homes stay safe.
- **Why not "force home-secret rotation on override retry" (rejected):** forcing
  escalation on a single override reject would rotate the shared home secret
  under the OTHER unit's live session, re-introducing the reciprocal 21204
  storm that RC1–RC5 eliminated — catastrophic for a 2-unit home with a re-firing
  trigger.
- **Files:** `services.py` (`_reset_one_device`, `_override_readback_ok`,
  `async_handle_reset_to_internal`), `manifest.json` (`1.0.0-beta16o`).

## v1.0.0-beta16n

### Fixed (`long_stall` wedge — in-place reconnect spun without recovering)

- **Symptom (β16m field log, ~11.7k polls):** `stream_reconnect_reasons` showed
  `long_stall: 61` (vs `session_expired_21204: 157`). System log carried
  `E2E stream wedged: no fresh frame for 24 consecutive polls (~120s) — in-place
  reconnect not recovering; resetting REST client and rebuilding session`.
  Wedge diag: `reconnects: 21` during one stall, `relay_status: {status_json:0,
  control_text:0}`, `keepalive_acks` frozen — i.e. the socket received ZERO
  inbound (a relay-side session-binding drop), not a decrypt failure. 21 in-place
  reconnects all re-handshaked "ok" but the relay never resumed pushing frames,
  until the coordinator's 120s wedge timer forced a full REST reset.
- **Root cause:** `_stream_flag_reconnect` (e2e.py) set
  `_stream_needs_creds_refresh = True` only for `21204`. A `long_stall` flagged
  a reconnect WITHOUT a creds refresh, so `_stream_reconnect_locked` re-handshaked
  with the SAME home secret. The relay had dropped the session binding, so the
  transport-level handshake "succeeded" but no frames flowed. The decrypt-gate
  watchdog (e2e.py, `_stream_watchdog_locked`) only escalates when frames ARRIVE
  but fail to decrypt — it cannot see a blackhole (zero inbound), so it never
  forced a rotation. Result: the in-place path spun uselessly until the
  coordinator's out-of-band 120s REST reset bailed it out.
- **Fix:** in `_stream_flag_reconnect`, when the reason is `long_stall` AND the
  session was previously healthy (`_stream_ever_decrypted`), also set
  `_stream_needs_creds_refresh = True`. The next in-place rebuild now rotates the
  home secret (same path as 21204) and re-establishes a fresh relay session, so
  the stall self-heals in one reconnect instead of waiting ~120s for the REST
  reset. Cold-start stalls (never decrypted) are left alone — a legitimately idle
  relay is not punished by a forced rotation. The `long_stall` creds-refresh is
  gated identically to the decrypt-gate's own `_stream_ever_decrypted` guard.
- **Files:** `emaldo_lib/e2e.py`, `manifest.json` (→ `1.0.0-beta16n`).

## v1.0.0-beta16m

### Cleanup (remove dead code — graph-assisted audit)

- **How found:** knowledge-graph pass over `ha-emaldo-repo` (graphify) flagged
  functions with zero call sites across the repo; each candidate was verified
  by grep + `value_fn=` registry check (sensors are dispatched via
  `EmaldoSensorEntityDescription.value_fn`, not by name) before removal.
- **Removed (5 functions, 0 references, safe to delete):**
  * `coordinator.py` — `_get_paired_realtime` (orphaned helper, never called)
  * `coordinator.py` — `_read_power_flow_secondary` (secondary-device power-flow
    read, never invoked; primary path uses `_read_power_flow`)
  * `sensor.py` — `_latest` (time-series helper, not in any `value_fn=`)
  * `sensor.py` — `_latest_nonzero` (time-series helper, not in any `value_fn=`)
  * `sensor.py` — `_balancing_display` (display-string helper for the balancing
    state sensor, superseded by `EmaldoBalancingStateSensor`'s own mapping)
- **Not removed (verified intentional, not dead):** `emaldo_lib/client.py` ↔
  `emaldo_lib/e2e.py` name pairs (`cancel_sell`, `get_manual_selling`,
  `set_virtualpowerplant`, …) are the two-layer REST vs E2E-stream API;
  `schedule_coordinator._ensure_client`/`_reset_client` delegate to the parent
  `EmaldoCoordinator` (composition, not duplication).
- **Files:** `coordinator.py`, `sensor.py`, `manifest.json` (→ `1.0.0-beta16m`).

### Fixed (spurious `AttributeError: NoneType` read_error on session invalidate)

- **Symptom (beta16l field log):** 2× `reconnect_reasons: read_error` with
  `last_read_error: AttributeError: 'NoneType' object has no attribute …` over
  ~11.8k polls. Benign (99.6% success, self-heals) but a needless reconnect.
- **Root cause:** `_ensure_session()` can return `None` when the session is
  invalidated (rotate/reset → `_invalidate_session_ref` sets `self._session =
  None`) during its own first-frame wait. Its guard (coordinator.py:1060) is
  documented to "return the (None) session so the caller's normal re-create
  path handles it" — but `_read_power_flow` did not null-check before calling
  `session.get_latest_power_flow()`, so the `None` was dereferenced and threw
  `AttributeError`, which the poll wrapper caught as a generic `read_error`
  and triggered a reconnect. The intended clean "not ready, recreate next
  poll" path was never taken.
- **Fix:** in `_read_power_flow`, immediately after `session =
  self._ensure_session()`, `if session is None: return None`. The `None` now
  propagates cleanly up to `_async_update_data` (the normal re-create path on
  the next poll handles it) instead of raising. No spurious `read_error`
  reconnect.
- **Files:** `coordinator.py`.

### Fixed (`1 nonce(s) tried` decrypt-noise — full drain + legacy-read coverage)

- **Reported by:** JanBaecklund (issue #47). beta16k silenced
  `_try_parse_power_flow` + the drain classifier; beta16l added the
  force-logout caller. After those, the `emaldo-e2e-stream` thread STILL
  flooded `decrypt_response: 1 nonce(s) tried but decryption/validation
  failed` + `no nonce markers` for every keepalive/alive/wake/heartbeat
  ACK (observed paired with a `SUCCESS` for the same nonce a few ms later —
  the frame IS decryptable, just not with the tried key).
- **Root cause:** the drain loop's **`_try_parse_regulate_frequency`**
  caller (e2e.py, inside `_stream_drain_locked`) called `decrypt_response`
  with the default `silent=False`. Every drain packet that was not a
  power-flow frame fell through to `_try_parse_regulate_frequency`, which
  emitted `1 nonce(s) tried … failed` on the benign relay frame. The
  same benign-relay-frame pattern also lived in the **module-level legacy
  `read_*` / `send_*` session functions** (`read_override_state`,
  `read_regulate_frequency_state`, `read_power_flow`,
  `read_selling_protection`, `read_virtualpowerplant`, `read_manual_selling`,
  `read_ev_charging_mode`, `read_battery_info` drains, the `_run_session`
  `_accept_any` peak-shaving read, and the `_try_decrypt_verbose` helper) —
  these fire `1 nonce(s) tried` during initial setup / creds-refresh reads.
  The earlier "3 drain callers" claim in this changelog was INCOMPLETE:
  the regulate-frequency parse path was the actual stream-thread flood
  source, and the legacy reads were never covered.
- **Fix:** pass `silent=True` to every benign-relay-frame `decrypt_response`
  call across both the `PersistentE2ESession` methods AND the module-level
  `read_*`/`send_*` functions:
  * `_try_parse_regulate_frequency` (the stream-thread flood — primary fix)
  * `_try_parse_battery` (class method + module-level legacy variant)
  * `_try_decrypt_verbose` (legacy read helper)
  * `_run_session` `_accept_any` peak-shaving read
  * `read_override_state`, `read_regulate_frequency_state`,
    `read_power_flow`, `read_selling_protection`, `read_virtualpowerplant`,
    `read_manual_selling`, `read_ev_charging_mode` drains + their
    `home_chat_secret` fallbacks
  Total `silent=True` decrypt calls went from 4 → 30. The stream thread now
  logs only `decrypt_response: SUCCESS` + `decrypted OK` + the coalesced
  `DECRYPTED non-data push xN (window=60s)` summary. **Left intentionally
  non-silent:** `load_ev_page_data` EV-panel discovery burst (tries many
  wire bytes on purpose; stores raw on failure — diagnostic by design).
- **Verification (field log 2026-07-18 18:23):** zero `1 nonce(s) tried`
  / `no nonce markers` in ~2 min of stream traffic; only `SUCCESS` +
  `decrypted OK` remain.
- **Files:** `emaldo_lib/e2e.py`.

### Fixed (benign `parse_battery_data: payload is None` flood)

- **Symptom:** every poll logged ~15× `parse_battery_data: payload is None` for
  empty battery module slots (idx 3-12 when modules not installed). Benign but
  noisy — not coalesced like the `_pf_rejected` pattern.
- **Fix:** coalesce to one line per 60s window (first event immediate, then
  `[xN in window]`), mirroring the `_pf_rejected`/`_decrypt_rejected` counters.
  Genuine single-event visibility preserved.
- **Files:** `emaldo_lib/e2e.py`.

### Fixed (regression: `UnboundLocalError` on `parse_battery_data` coalescing)

- **Symptom:** `Battery module info read failed: cannot access local variable
  '_battery_none_count' where it is not associated with a value`
  (e2e.py:705) on every empty battery slot → battery scan aborts.
- **Root cause:** the `parse_battery_data` coalescing block assigned to the
  module-level `_battery_none_count` without a `global` declaration, so Python
  bound it as a function-local (uninitialized) variable. The other two
  coalescing blocks (`_decrypt_rejected`, `_pf_rejected`) declare `global`;
  this one was missing it.
- **Fix:** add `global _battery_none_count, _battery_none_window_start` inside
  the block.
- **Files:** `emaldo_lib/e2e.py`.

### Fixed (21204 storm — RC5 5s grace blocked home-secret rotation escalation)

- **Symptom (beta16m field log):** 1128 `Stream saw 21204` events over ~2h,
  `stream_reconnects` climbing to 557+, `force_home_refresh` never emitted,
  every forced creds refresh immediately followed by `Home e2e cache Ns fresh
  — reusing`. Stream wedged in a ~18-min 21204 cadence (relay's own session
  TTL), self-healing only when the relay opened a fresh window.
- **Root cause:** the escalation path was correctly wired (RC4) — `_get_e2e_
  credentials` sets `_do_home_refresh=True` when `entry.generation >= 3` within
  60s and passes it to `e2e_login(force_home_refresh=...)` → `_get_home_e2e(
  force_refresh=True)`. But RC5's grace short-circuit (`client.py:789`, the
  `if force_refresh and age < 5: return cached`) fired on that forced call and
  replayed the STALE home secret instead of hitting the `/home/e2e-login/`
  rotation API. So the home secret never actually rotated and the stream could
  never rejoin — exactly the wedge RC4 was meant to break.
- **Fix:** remove the forced-refresh grace reuse. `_get_home_e2e` now only
  short-circuits on the normal home TTL when `not force_refresh`. The ONLY
  caller that passes `force_refresh=True` is an escalation, so routine (non-
  escalation) forces from the stream keep `force_refresh=False` and RC5's 5s
  dedupe still protects dual-unit ping-pong. On escalation the rotation API
  now fires, breaking the 21204 loop. Live sessions learn the new secret via
  the post-rotation home-secret callback (now fired on the real rotation path).
- **Files:** `emaldo_lib/client.py`.

## v1.0.0-beta16l

### Fixed (decrypt-noise flood NOT killed by beta16k — the `_try_parse_power_flow` path)

- **Reported by:** JanBaecklund (issue #47), still seeing `decrypt_response:
  1 nonce(s) tried but decryption/validation failed` after beta16k.
- **Root cause:** beta16k's `silent=True` only covered the drain-**classifier**
  pass (the SECOND decrypt of each unparsed frame). Every drain frame first
  goes through `_try_parse_power_flow` (the power-flow parse attempt), and
  that path still called `decrypt_response` with `silent=False`. Keepalive
  frames carry AES nonce markers (e.g. `90a3_pos=[71]`) but are not encrypted
  with our `chat_secret`, so the power-flow pass emitted
  `1 nonce(s) tried but ... failed` for every one of them. That was the bulk
  of the flood — beta16k reduced it by ~half at most, not to zero.
- **Fix:** pass `silent=True` to both `decrypt_response` calls inside
  `_try_parse_power_flow` (own-key + home-key fallback). Combined with the
  beta16k classifier fix, every drain-frame decrypt attempt is now silent on
  the per-call `no nonce markers` / `N nonce(s) tried but ... failed` lines.
  A keepalive frame now increments `stream_keepalive_acks` with zero decrypt
  noise. Nothing real is lost: `decrypt_response: SUCCESS` still logs for real
  frames, and genuine power-flow decrypt trouble is still surfaced by
  `_try_parse_power_flow`'s coalesced `_pf_rejected [xN in window]` and
  `decrypt_response=None [xN in window]` summaries (1 line / 60 s window).
- **File:** `emaldo_lib/e2e.py` (`silent=True` at both `_try_parse_power_flow`
  decrypt calls).

## v1.0.0-beta16k

### Fixed (diagnostic decrypt-noise — "still a lot of decryption/validation failed")

- **Reported by:** JanBaecklund (issue #47), comparing beta16j → beta16k. The
  DEBUG log showed a very high `decrypt_response: ... decryption/validation
  failed` count and he read it as a regression.
- **Root cause (NOT a real failure):** the E2E socket carries ~99 % benign relay
  control traffic — keepalive/notice/server frames (`alive`/`and_`/`notice`/
  `srv`/`SERVER`) and `cmd not allowed` ACKs. These are NOT encrypted with our
  `chat_secret` (or carry no AES nonce at all), so every one of them logs
  `no nonce markers` / `N nonce(s) tried but failed`. beta16k *added* to this:
  the new drain-classifier re-decrypts every unparsed packet to categorise it,
  so the same benign frames were logged a second time. Two independent logs
  confirm the ratio is benign — beta16j was 11.2:1 fail:success over ~82 s;
  beta16k was 17.3:1 over ~5.9 min (higher raw count but mostly the extra
  classifier pass + 4× longer run). The REAL #47 symptom — reconnect
  ping-pong / dead window — is fixed: beta16k froze stream reconnects at 2
  across the whole run with no `CONN_NOT_ESTABLISHED`, vs beta16j churning 19
  reconnect refs in 82 s.
- **Fix:** `decrypt_response` gains a `silent: bool = False` kwarg that
  suppresses the per-call `no nonce markers` / `N nonce(s) tried but ...
  failed` DEBUG lines while still returning the decrypted bytes (and still
  emitting `SUCCESS` + the rate-limited `DECRYPTED non-data push` summary).
  The drain-classifier categorisation pass uses `silent=True`.
- **File:** `emaldo_lib/e2e.py` (`decrypt_response` signature + two guarded
  DEBUG lines; `silent=True` at the drain-classifier call site).

### Changed (diagnostics — split drained datagrams into three scopes)

- **Why:** even after categorising, lumping keepalive/status/ACK traffic under
  `stream_drain_unparsed` was misleading — those frames are perfectly parseable
  benign relay chatter, not "unparsed". The counter that matters (frames we
  truly could not parse) was buried in a dict dominated by keepalives.
- **What:** drained non-power-flow datagrams are now routed into **three
  separate scopes** instead of one `unparsed` bucket with a nested breakdown:
  - `stream_keepalive_acks` — **top-level** counter for benign relay presence/
    keepalive/notice/server chatter (`alive`/`and_`/`notice`/`srv`/`SERVER`).
    The bulk of drain traffic; expected, ignore.
  - `stream_relay_status` — decrypted benign relay status/ACK pushes,
    `{status_json, control_text}`; expected, ignore.
  - `stream_drain_unparsed` (+ `stream_drain_unparsed_categories`) — now counts
    **only** frames we genuinely could not parse, `{binary, undecryptable}`.
    `undecryptable > 0` is the single value that indicates a real decrypt
    problem (stale secret / wrong key / corrupt frame).
- All three are cumulative across sessions (coordinator delta-merge, reset on
  `_invalidate_session_ref`). Verified: keepalive/notice/SERVER → keepalive
  counter, JSON/ACK → relay_status, non-text/no-key → drain_unparsed only.
- **Files:** `emaldo_lib/e2e.py` (session counters + routing in the drain loop +
  `stream_diagnostics`), `coordinator.py` (three cumulative stats + merge +
  reset), `sensor.py` (three attributes).

### Changed (diagnostics — categorize the drain "unparsed" bucket)

- **Why:** the diagnostic sensor showed a single `stream_drain_unparsed` count
  that was often ~87 % of `stream_drain_packets`, which *looks* like a massive
  failure to anyone without protocol background. In reality these are almost all
  NORMAL relay control/status datagrams multiplexed onto the same persistent E2E
  subscription socket as the 0x30 power-flow frames — the integration decrypts
  them fine and intentionally ignores them (they are not power-flow /
  regulate-frequency / force-logout). The high ratio only means most pushes on
  that socket are control, not data.
- **What:** the drain "unparsed" packets are now classified into categories via
  `_classify_drain_payload(resp, decrypted)`. Classification looks at the RAW
  response first (relay keepalive tags live *outside* the AES-CBC body), then
  the decrypted payload:
  - `keepalive_ack` — raw response carries the relay `alive`/`and_<session>`
    presence tags. This is the relay's keepalive/heartbeat ACK and is by far the
    most common drain datagram. **Verified against a real debug-log run** (see
    below): `alive` (`616c697665`) appears in 628 raw responses; these are the
    179 B replies to `Alive(home)`/`Alive(device)`. Not power-flow, not an error.
  - `status_json`  — decrypted, valid JSON status push (`{"__time":...}` etc.)
  - `control_text` — decrypted printable non-JSON (ACK, `cmd not allowed`)
  - `binary`       — decrypted (often only a partial/tail block, e.g. ending
    `...u_x_02"}`) but not valid printable text
  - `undecryptable`— neither a keepalive tag nor decryptable with any known key
    (**the only category that may indicate a real problem**: stale secret /
    wrong key / corrupt frame)
  Per-session counts live on the session; the coordinator accumulates them
  cross-session (same delta pattern as reconnect reasons) into
  `stats_stream_drain_unparsed_categories`, surfaced on the sensor as the
  `stream_drain_unparsed_categories` attribute (only non-zero categories shown).
- **Verification logging:** to confirm the taxonomy against real traffic, a
  rate-limited DEBUG sample is emitted at most once per category per 120 s
  (`drain unparsed sample: category=... payload=NB ... resp_ascii=... resp_head=... decrypted_hex=...`).
  This lets a debug-log test run reveal the actual payloads without flooding.
  `resp_ascii` (first 48 B, non-printable rendered `.`) was added after the
  first debug-log run revealed the keepalive tags live in the raw response, not
  the decrypted body — it makes the `keepalive_ack` classification auditable.
- **First debug-log run (2026-07-17):** confirmed the drain "unparsed" bucket is
  dominated by keepalive ACKs; earlier `binary`/`undecryptable` labels were
  reclassified to `keepalive_ack` once the raw response was inspected. No
  `status_json`/`control_text` seen in that window; realtime sensor healthy
  (85/85 polls, `stream_stale: false`, RTT min 92.1 ms).
- **Second debug-log run (2026-07-17, 38 min, 453 polls):** `keepalive_ack:
  1290` vs `binary: 2` / `undecryptable: 1` — 99.7 % of the 1293 drain "unparsed"
  packets are normal relay keepalive/presence ACKs. The single `undecryptable`
  decodes to the same `<id><token>` presence format (no literal `alive`/`and_`
  substring in the scanned window) — benign, not a secret/key problem. Realtime
  fully healthy: 453/453 polls, 0 failures, 5 `session_expired_21204` reconnects
  all recovered, `stream_stale: false`.
- **RTT min guard tightened (`< 1.0` ms reject):** the first run showed
  `e2e_rtt_min_ms: 0` because a sub-millisecond clock-granularity measurement
  slipped past the old `<= 0` guard and rounded to `0.0`. Real relay RTT is
  always 50-120 ms, so any sample `< 1 ms` is an artifact and is now dropped.
- **No protocol/behavior change** — the total `stream_drain_unparsed` counter is
  unchanged; this only adds the breakdown and the sample log. The `#53` decrypt
  gate, `#53` None-guard, and RTT guard from beta16j are unaffected.
- **Third debug-log run (2026-07-17, 15 min):** confirmed all drain-unparsed
  frames are benign relay traffic. The earlier `undecryptable`/`binary` samples
  decode to relay `notice`/`SERVER` presence frames and partial-offset JSON
  control tails (``..."logout"}``) — none indicate a stale/key problem. The
  `undecryptable` frames even carry valid AES nonces but use a *relay* key, not
  our `chat_secret`, so they are expected. Broadened the `keepalive_ack` marker
  set to `alive`/`and_`/`notice`/`srv`/`SERVER` so these land in the benign
  bucket; `undecryptable` is now reserved for genuinely unparseable frames. The
  only error in the run was a transient `OSError: Bad file descriptor` on a bulk
  override command during a reconnect window (fd mid-rebuild) — expected
  CONN_NOT_ESTABLISHED retry behavior, self-recovered, same family as #41 RC4.
  This is now handled cleanly (see below).

### Fixed (override socket teardown race — `OSError: Bad file descriptor`)

- **Root cause:** `_send_override_via_stream` runs in a HA `SyncWorker` executor
  thread. Between `_ensure_session()`'s `closed` check and the actual
  `send_command_for_creds`, the session's socket can be torn down concurrently
  by *another* thread: the stream reconnect thread (primary) or the
  `rekey_home` callback fired after a primary rotates the home secret (any
  device in a **multi-unit** home). Sending on that dead fd raised
  `OSError(EBADF, "Bad file descriptor")`, which surfaced as a full Traceback
  and was retried as a generic `exception` (3× → `CONN_NOT_ESTABLISHED`). It
  always self-recovered, but the Traceback looked alarming and the retry
  reason was miscategorised.
- **Fix A — classify EBADF as transient (no Traceback):** the `except` now
  catches `OSError` and, when `errno == EBADF`, logs a quiet DEBUG line and
  classifies the failure as `conn_not_established` (retryable) instead of a
  Traceback-logged `exception`. Any *other* `OSError` (real network fault) is
  still logged with a full Traceback.
- **Fix B — close the TOCTOU:** immediately before the send, re-check
  `session.closed`. If the session was invalidated under us, bail out cleanly
  (`conn_not_established`, invalidate + flag fresh creds) rather than sending on
  a dead socket. Deliberately **no re-acquire loop** — a single deterministic
  bail keeps this safe during a mass `rekey_home` across many units; the caller
  (`services.py`) retries on the freshly rebuilt session.
- **Multi-unit safety:** the fd race is strictly per-device (each device owns
  its own socket in `_device_sessions`, keyed per device — an override for one
  unit cannot close another unit's fd). `_ensure_client`/`_reset_client` are
  already lock-guarded in `client.py`. The one multi-unit-only trigger
  (primary→secondary `rekey_home` concurrent with a secondary override) is
  covered by the same "transient → retry" semantics; no new locking, no hot
  loop. Verified single-unit-testable; multi-unit path is behaviourally
  identical.
- **Verified:** a standalone script exercised the *real* extracted method over
  4 cases — closed-before-send, EBADF-during-send, other-OSError, happy-path —
  all pass (transient paths return `False` with `conn_not_established` and no
  raise; other `OSError` still surfaces; happy path unchanged).
- **File:** `coordinator.py` (`_send_override_via_stream` + `import errno`).
- **Counter scope fixed (subset > parent bug):** a debug-log review caught
  `stream_drain_unparsed` (per-session, from `stream_diagnostics`) being *smaller*
  than the cumulative `stream_drain_unparsed_categories` total after reconnects
  (run showed 671 vs 733 — the breakdown exceeded its own parent). Both are now
  **cumulative lifetime**: the coordinator folds the per-session delta into
  `stats_stream_drain_unparsed` (survives session recreation via the same
  delta-merge as the categories), and the sensor reads that. The per-session
  value remains available in `stream_diagnostics["drain_unparsed"]` if needed.
- **Files changed:** `emaldo_lib/e2e.py` (`_classify_drain_payload` +
  `_classify_decrypted_payload` wrapper; category counters, sample log, and
  `drain_unparsed_categories` in `stream_diagnostics`), `coordinator.py`
  (cumulative `stats_stream_drain_unparsed_categories` + merge/reset),
  `sensor.py` (`stream_drain_unparsed_categories` attribute), `manifest.json`.

## v1.0.0-beta16j

### Fixed (#53 — session wedged "handshake ok" but never decrypts)

- **Root cause (issue #53 Q1/Q3):** a handshake returning `"ok"` only proves
  the transport reconnected — it does **not** prove the current `chat_secret`
  can still decrypt the realtime push stream. The reconnect logic cleared
  `_stream_needs_reconnect` on handshake-ok alone, so a session that handshaked
  `ok` yet decrypted **no** frames was declared "recovered" and stopped
  escalating — the reporter saw a >6 h stall with repeating `Handshake complete
  (response=ok)` while `Stream long-stall` kept climbing.
- **Fix (decrypt-gated reconnect, Q3):** the session now records the monotonic
  time of the last successfully **decrypted** power-flow frame
  (`_stream_last_decrypted_frame_ts`, set in `_stream_drain_locked`). After a
  handshake-ok reconnect, if the session has ever decrypted before
  (`_stream_ever_decrypted`), a decrypt-gate deadline is armed
  (`_stream_decrypt_gate_window`, 30 s). A cleanly decrypted frame clears it; if
  the deadline passes with no fresh frame, `_stream_watchdog_locked` escalates
  to a **forced credential/home-secret refresh** (same path as a 21204).
- **Automatic recovery (Q1):** the escalation sets `_stream_needs_creds_refresh`
  and `_stream_reconnect_locked` now refreshes creds **before** the first
  re-handshake when that flag is set (otherwise the re-handshake reuses the
  stale secret, succeeds at the transport level, and the gate would loop). The
  forced refresh increments the generation counter → `force_home_refresh` →
  rotates the stale `home_end_secret`, so the stream self-heals instead of
  wedging until a manual restart.
- **No refresh spiral (Q2):** the gate fires **at most once per reconnect
  episode** (the deadline is cleared when it triggers) and `long_stall`/socket
  reconnects still do NOT force-refresh — only a decrypt-gated failure does.
  A never-yet-decrypted session (cold start, inverter offline) is not gated, so
  a legitimately idle relay is never punished.
- **Files changed:** `emaldo_lib/e2e.py` (`_stream_last_decrypted_frame_ts` /
  `_stream_ever_decrypted` / `_stream_decrypt_gate_deadline` /
  `_stream_decrypt_gate_window`; decrypt bookkeeping in `_stream_drain_locked`;
  decrypt-gate check in `_stream_watchdog_locked`; pre-reconnect creds refresh +
  gate arming in `_stream_reconnect_locked`).

### Fixed (spurious read_error reconnects — `NoneType.get_latest_power_flow`)

- **Problem:** `_ensure_session` cached `_session = self._session` then ran a
  first-frame-wait loop calling `_session.get_latest_power_flow(...)`. If the
  session was invalidated (`_invalidate_session_ref` sets `self._session = None`
  during a rotate/reset) between the stream-mode check and the loop, the call
  raised `AttributeError: 'NoneType' object has no attribute
  'get_latest_power_flow'`, surfacing as a `read_error` reconnect
  (`last_read_error` in the diagnostic sensor).
- **Fix:** guard `if _session is None: return self._session` before the loop so
  the caller's normal re-create path handles the missing session.
- **Files changed:** `coordinator.py` (`_ensure_session`).

### Fixed (implausible ~0 ms RTT samples)

- **Problem:** `stats_e2e_rtt_min_ms` could drop to ~0.1 ms — physically
  impossible for a UDP round-trip to the relay — when a cached/local path
  reported a non-positive `last_rtt_ms`.
- **Fix:** `_sample_session_rtt` now skips samples `<= 0`.
- **Files changed:** `coordinator.py` (`_sample_session_rtt`).

### Changed (logging — coalesce per-step decrypt flood)

- **Problem:** `_try_parse_power_flow` logged a DEBUG line at every step for
  each relay status/control push (non-power-flow packet): "trying
  home_chat_secret (device key failed)" + "decrypt_response=None". These fire
  per packet (~65/min in a 28-min beta16i capture → ~1,840 lines, the dominant
  log content), drowning real diagnostics. The `DECRYPTED non-data push`
  *summary* was already rate-limited, but these step logs were not.
- **Fix:** coalesced both step logs into a single line per 60 s window (first
  event flushed immediately, later events in the window counted and flushed
  once at window expiry) via module-level `_pf_rejected_*` state in `e2e.py`,
  mirroring the existing `_decrypt_rejected_*` pattern. The rare-but-useful
  lines (`decrypted OK`, `parse_power_flow FAILED`) stay at full verbosity.
  Pure diagnostic change — no protocol, timing, or data-keeping behavior change.
- **Regression + fix #1 (same version):** the first beta16j build wrote those
  module globals without a `global` declaration inside the method, raising
  `UnboundLocalError` in the `emaldo-e2e-stream` worker thread on every
  non-power-flow push (161 reconnects in a 26-min capture, classified as
  `loop_exception:UnboundLocalError`). Fixed by adding
  `global _pf_rejected_count, _pf_rejected_window_start, _pf_rejected_window_s,
  _pf_rejected_lock` at the top of `_try_parse_power_flow` (mirrors the existing
  `global` in `decrypt_response`).
- **Regression + fix #2 (same version):** the coalescing guard used
  `is_first = count == 0` then reset `count = 0` on flush. That made *every*
  fresh-window start flush immediately, so the coalescing never suppressed
  anything — both the new `_pf_rejected` step logs AND the pre-existing
  `DECRYPTED non-data push` summary were still emitted per-packet (~80/min,
  drowning the log). Corrected to a `window_start == 0.0` first-event check
  followed by a true `now - window_start >= WINDOW` flush, so each window
  yields exactly one summary line. The pre-existing `_decrypt_rejected` block
  had the identical flaw and is fixed the same way.
- **Files changed:** `emaldo_lib/e2e.py` (module-level `_pf_rejected_*` state +
  wrapped step logs + `global` decl + corrected coalescing in both
  `_try_parse_power_flow` and `decrypt_response`), `manifest.json` (version bump).

## v1.0.0-beta16i

### Changed (#49 — Realtime connection sensor should reflect stale data)

- **Root cause (issue #49):** a user reported that with the inverter off for
  15 min, Home Assistant kept showing the last power-flow values while the
  vendor app correctly went blank. The "Realtime connection" diagnostic sensor
  only had two states — `connected` (driven by `coordinator.last_update_success`)
  and `reconnecting` (on a transport `UpdateFailed`). The stream path
  *deliberately* keeps the last-known values visible when no fresh frame
  arrives within `STREAM_STALE_AFTER` (so a brief gap doesn't blank the
  dashboard), and that path does **not** raise `UpdateFailed` — so
  `last_update_success` stayed `True` and the sensor never left `connected`,
  even after 15 min of no fresh data. The stall was captured in diagnostics
  (`last_success` froze, `success_rate_recent` dropped, `stall_snapshot`
  captured) but was not surfaced as a connection state.
- **Fix:** added a third state `stale` to the "Realtime connection" sensor
  (`sensor.py` `native_value`). It is returned when `last_update_success` is
  `True` **but** `stats_last_success` is older than `STREAM_STALE_AFTER`
  (28 s) — i.e. the last poll "succeeded" yet no fresh frame has arrived
  recently. This is exactly the inverter-off / relay-stopped-pushing case the
  reporter expected to see. Transport failures still return `reconnecting`.
  Also added a `stream_stale` boolean attribute (`true`/`false`) for
  automations/templates. No protocol, timing, or data-keeping behavior change
  — the last-known values are still shown during a stall; only the *state
  label* now distinguishes stale from connected.
- **Files changed:** `sensor.py` (`native_value` three-state logic +
  `stream_stale` attribute, `STREAM_STALE_AFTER` import, `import time`),
  `manifest.json` (version bump).

## v1.0.0-beta16h-C4

### Fixed

- **Override (set_slot_range / apply_bulk_schedule / reset_to_internal)
  CONN_NOT_ESTABLISHED after periodic relay 21204 (#47 C4):** the override
  session path (`_send_override_via_stream`) silently returned False on
  timeout/CONN_NOT_ESTABLISHED without invalidating the dead session. The
  3-attempt retry loop called `coord._reset_client()` (REST client) but the
  realtime coordinator's cached session was never invalidated — every retry
  reused the same relay-dead socket. After 3 failures the legacy one-shot
  fallback succeeded (non-fatal), but the 3-attempt failure cascade added
  ~3s latency to every override that hit the ~2-3s relay-expiry window.
  **Fix:** on CONN_NOT_ESTABLISHED or timeout, `_send_override_via_stream`
  now calls `session.close()`, `_pop_device_session()`,
  `_invalidate_session_ref()`, and sets `_needs_fresh_creds = True` so the
  next retry builds a fresh session with fresh credentials. The retry loop's
  `coord._reset_client()` is then redundant for the E2E case (but harmless).
- **Files changed:** `coordinator.py` (override session invalidation).
- **`stream_reconnect_reasons` diagnostic mismatch vs `stream_reconnects`
  counter (#47 beta16h-C4 diagnostics):** two layered bugs made the sensor
  look contradictory. (1) The reasons dict incremented only inside
  `_stream_flag_reconnect` when `not self._stream_needs_reconnect`, but the
  reconnect counter increments on every actual rebuild in
  `_stream_reconnect_locked`. While a reconnect retries with the flag already
  set, the counter climbs but the reason is never re-recorded — so the sensor
  showed e.g. `stream_reconnects: 60` with `stream_reconnect_reasons:
  {session_expired_21204: 2}`, making a healthy stream look like a 2-reason
  storm. The 128 MB field log confirmed the real reason was 21204 on 7708 of
  9347 diagnostic lines. (2) The sensor compared two different scopes:
  `stream_reconnects` is a **coordinator-cumulative** total
  (`stats_stream_reconnects_total`, rolled up across session recreations in
  `_accumulate_stream_stats`), while `stream_reconnect_reasons` was read from
  the **per-session** `stream_diagnostics()` dict, which resets to `{}` on
  every session teardown — so even after fix (1) the live session showed
  `stream_reconnects: 29` with `stream_reconnect_reasons: {}`.
  **Fix:** (a) `_stream_flag_reconnect` now always records
  `_stream_last_reconnect_reason`; the per-rebuild reason count is incremented
  in `_stream_reconnect_locked` next to the reconnect counter, so the per-session
  dict tracks rebuilds 1:1. (b) The coordinator now accumulates the reason
  breakdown **cumulatively** (`_merge_stream_reasons` + flush at
  `_invalidate_session_ref` teardown), mirroring `_accumulate_stream_stats`, and
  the sensor reads the cumulative `stats_stream_reconnect_reasons` /
  `stats_stream_last_reconnect_reason`. The two now share scope and stay in
  lockstep. No reconnect/backoff/credential behavior change.
- **Files changed:** `emaldo_lib/e2e.py` (`_stream_flag_reconnect`,
  `_stream_reconnect_locked`), `coordinator.py` (`_merge_stream_reasons`,
  `_invalidate_session_ref`, `_accumulate_stream_stats`), `sensor.py`
  (cumulative stream reconnect-reason attributes).

- **`e2e_rtt_*` diagnostic stayed `null` / 0 in stream mode (#47 beta16h-C4
  RTT gap):** the session measures UDP RTT on every `_send_raw` exchange
  (handshake/keepalive/command) and stores it in `_last_rtt_ms`, but the
  coordinator's RTT-sampling block lived only in the **legacy** (non-stream)
  read path. In stream mode `_read_power_flow_primary` returns the cached
  frame *before* reaching that block, so `stats_e2e_rtt_samples` never
  incremented and the sensor showed `e2e_rtt_last_ms: null`,
  `e2e_rtt_samples: 0` — even though the log contained 52 `rtt=` lines proving
  RTT was being measured. The same early return also left the legacy
  `powerflow_drain_*` / `powerflow_initial_*` accumulators at 0 in stream mode
  (those are covered by `stream_diagnostics()` so no data is actually lost).
  **Fix:** extracted `_sample_session_rtt(session)` and call it in **both**
  paths (stream branch + legacy branch). Purely diagnostic — copies an
  already-computed value, sends no packets, no protocol/timing change.
- **Files changed:** `coordinator.py` (`_sample_session_rtt`, stream + legacy
  RTT sampling in `_read_power_flow_primary`).

### Changed

- **Decrypt diagnostic relabeled `DECRYPTED-BUT-REJECTED` → `DECRYPTED non-data push` (#41 logging clarity):** the rate-limited debug line fired whenever AES decrypted a packet but the payload was not a power-flow/battery frame. Most such packets are relay status/control JSON (e.g. `{"__time":...,"domain":"eu_x_02"}`, `cmd not allowed`) delivered on the same socket — they are *handled* (decrypted + identified) and ignored, not *rejected* as errors. The old label sounded like a failure to anyone without full context. The sample payload is now classified: printable ASCII/JSON → `text/status push: '<preview>'`; otherwise → `binary (not power-flow/battery)`. No behavior change — purely diagnostic wording.
- **Files changed:** `emaldo_lib/e2e.py` (`decrypt_response` diagnostic + `_classify_decrypted_payload` helper).

## v1.0.0-beta16h-C3

### Changed (#47 Phase 4 — secondary must never rotate home secret)

- **Root cause identified:** 21204 storm persists because BOTH devices
  independently escalate to ``force_home_refresh=True`` (per-device generation
  counters in ``client.py`` escalation). Each rotation desyncs the other →
  permanent ping-pong. The OptC coordination failed because (a) primary
  published only inside ``_creds_provider`` (reactive, ~90s late at startup),
  and (b) the secondary itself kept rotating via its own escalation, defeating
  the override.
- **Fix 1 — primary publishes proactively at session creation:**
  ``_ensure_session`` now publishes ``home_end_secret``/``home_chat_secret``
  to the shared ``_home_secrets`` dict immediately after fetching credentials
  (not only in ``_creds_provider``). The dict is populated before the
  secondary's ``_ensure_session`` runs (~164ms later), so the secondary has
  the correct value from the start.
- **Fix 2 — secondary never rotates the shared home secret:**
  Added ``allow_home_refresh`` parameter to ``get_e2e_credentials`` /
  ``_get_e2e_credentials``. Secondary passes ``allow_home_refresh=False``,
  forcing ``_do_home_refresh = _do_home_refresh AND allow_home_refresh``.
  Only the primary can escalate to ``force_home_refresh=True``; the secondary
  always reuses the primary's published value.
- **Files changed:** ``coordinator.py`` (proactive publish + pass-through
  ``allow_home_refresh``), ``client.py`` (new parameter + escalation guard).

### Test aim
In two-unit setup, expect: 21204 → ~0; primary publishes once at startup
+ on genuine TTL expiry only; no ``force_home_refresh`` ever fires from
secondary; override delivery same as single-unit. Monitor ``[OptC-diag]``
logs for ``primary: published home_secret at session creation`` (startup),
``_creds_provider secondary: overriding``, and absence of ``home_secrets
dict empty`` on secondary startup.

## v1.0.0-beta16h-C2

### Changed (#47 Option C — follow-up)
- **Override legacy one-shot fallback in all 4 service paths**
  (set_slot_range, apply_bulk_schedule, reset_to_internal reset-all +
  partial). After 3 failed stream attempts, falls back to
  `client.set_override(...)` on a fresh socket — mirrors the working
  emergency-charge pattern. Fixes override loss during 21204 windows
  (beta16h-C log: 5 "Failed to set/reset override after 3 attempts").
- **Added [OptC-diag] coordination diagnostics** (primary election,
  `_creds_provider` primary publish / secondary override, secondary
  home_secrets-empty) to investigate why home-secret coordination
  isn't stopping the dual-unit 21204 storm.

### Test aim
In two-unit setup (single-unit baseline confirmed clean), capture
`[OptC-diag]` log output to determine why home-secret coordination
doesn't engage during the 21204 storm. Key diagnostics:
- `[OptC-diag]_creds_provider primary: published home_secret` (primary)
- `[OptC-diag]_creds_provider secondary: overriding` or `no home_secrets` (secondary)
- `[OptC-diag] Secondary ... using primary-managed home secret` or `empty` (secondary `_ensure_session`)
- `[OptC-diag] primary_election` (both units, once per startup)

### Next steps depending on result
- **If primary publishes + secondary overrides but 21204 continues**:
  root cause is elsewhere (e.g. session short-circuit, stale device
  session). Extend diagnostics.
- **If primary publishes + secondary logs `no home_secrets`**:
  shared `_home_secrets` dict not populated before secondary session
  creation → race condition or `_creds_provider` not firing. Fix timing.
- **If primary never publishes**: `_creds_provider` not called on
  primary → stale `_participant_session` or `_creds_provider` closure
  captured wrong client. Fix call path.
- **If storm stops**: remove all `[OptC-diag]` logs, clean up, ship
  final.

## v1.0.0-beta16g

### Changed
- **Switch write verification + retry + warning (#51 via #52 by
  leifkristianssonl):** ``EmaldoThirdPartyPVSwitch``, ``EmaldoSellBackToGridSwitch``,
  ``EmaldoSellLimitSwitch``, and ``EmaldoManualSellSwitch`` previously performed
  one write attempt with no read-back verification. A stale session could return
  a "success" response while the relay actually rejected the command, leaving
  the switch state out of sync with the device.
  **Fix:** ``_write`` now retries once on ``EmaldoAuthError`` and logs a warning
  on failure. The coordinator's ``async_request_refresh()`` reads back the
  applied state so the UI reflects reality.
- **Cross-device E2E packet construction for shared home session (#47, Candidate
  A):** when a secondary device sends a command or power-flow read via the
  single shared ``PersistentE2ESession``, the relay binds the TCP socket to
  the **handshake device** (the one that established the session). Sending
  packets with the secondary device's ``sender_end_id``/``chat_secret`` causes
  the relay to reject with 21204 / ``CONN_NOT_ESTABLISHED``.
  **Fix:** ``send_command_for_creds`` and ``_read_power_flow_locked`` now merge
  the **session owner's** ``sender_end_id``/``sender_group_id``/``chat_secret``
  into the wire credentials when the target device differs from the owner. The
  target device is still addressed via ``recipient_end_id``/``recipient_group_id``.
  Decryption uses the owner's ``chat_secret`` first, then falls back to
  ``home_chat_secret`` (unchanged).

### Test aim
Confirm the 21204 storm stops and secondary override delivery reaches ~equal
success rate as the primary. Expected: 21204 rate → ~0; override delivery for
both devices → ~equal & high (>50% single-attempt).

### Next steps depending on result
- **If 21204 stops + override works for both → success.** Land ``beta16g``.
  If congestion remains → add ``_send_override_with_fallback`` (override
  verification + one-shot standalone retry, reusing PR #52's verify pattern).
  If emergency-charge ON/OFF still has asymmetry → evaluate secondary's
  ``_send_override_via_stream`` for emergency charge.
- **If 21204 continues (relay rejects owner-wire creds) → Candidate B:**
  encrypt with ``home_chat_secret`` instead of owner device ``chat_secret``
  (the relay may expect the home-level session key). Switch in both
  ``send_command_for_creds`` and ``_read_power_flow_locked``.

## v1.0.0-beta16h-C

### Changed (Option C — per-device E2E sessions + home-secret coordination)

- **Architecture: single shared E2E session → per-device sessions (#47).**
  Three previous command-path fixes all failed because the relay binds its TCP
  socket to ONE device after ``Alive(home)``+``Alive(device)`` handshake. A
  second device cannot send commands through that socket, even with wire-credential
  merging (beta16g) or a fresh one-shot socket (beta16f, beta16h-A).
  **Fix:** each device now creates its own ``PersistentE2ESession``, established
  as itself via its own ``Alive(home)``+``Alive(device)``+``Wake``+``Heartbeat``
  handshake sequence. The primary device publishes the current ``home_end_secret``
  to a shared ``hass.data[DATA]`` dict; secondary devices override their REST-fetched
  home secret with the primary's published value, preventing the 21204 ping-pong
  that occurred when two devices independently rotated the shared home secret.

- **Deterministic per-home primary election.** Previously ``is_primary = i == 0``
  assumed the first device in the config entry's device list was always primary.
  With multiple config entries sharing one home_id, each entry's first device became
  primary — producing two primaries for the same home.
  **Fix:** the first ``device_id`` registered per ``home_id`` (across all config entries)
  wins, using a persistent ``hass.data[DOMAIN]["home_primaries"]`` tracker.

- **Emergency charge: removed secondary-device restriction.** The legacy standalone
  fallback (standalone E2E socket) was blocked for secondary devices to avoid
  ``Alive(home)`` collision with the old shared session. With per-device sessions,
  each device owns its E2E socket — both stream path and legacy fallback are safe
  for any device.

- **Stream power-flow read simplified.** Primary and secondary no longer use
  different ``read_power_flow`` paths; both call ``session.read_power_flow()``
  on their own per-device session.

### Test aim
Confirm both devices can send overrides and emergency-charge commands through their
own E2E sessions without ``CONN_NOT_ESTABLISHED``. Expected: both devices achieve
high (>90%) single-attempt command delivery; no 21204 storm.

### Next steps depending on result
- **If both devices reliable → land beta16h-C.** Version ``1.0.0-beta16h-C``.
- **If 21204 returns → check home_secret coordination** (primary publish vs secondary
  override not working correctly in ``_creds_provider`` or ``_ensure_session``).

## v1.0.0-beta16f

### Fixed
- **Override (set_slot_range / apply_bulk_schedule / reset_to_internal) rejects
  with "Command causes failure" in dual-device setup (#47):** ``set_override``
  used the one-shot ``send_override`` function which opens a new UDP socket,
  sends ``Alive(home)``/``Wake``/``Heartbeat`` with a fresh nonce, and closes.
  The ``Alive(home)`` from the competing socket disrupted the persistent session's
  stream connection, causing the relay to reject commands with
  ``CONN_NOT_ESTABLISHED`` (observed in JanBaecklund log: `"command causes
  failure"` at 09:52:57). ``send_override``'s ``return resp is not None`` masked
  the rejection — any non-None response including ``cmd not allowed`` returned
  ``True``.
  **Fix:** added ``build_override_payload()`` to extract the raw (unencrypted)
  override payload from ``build_override_packet()``. Added
  ``_send_override_via_stream()`` to ``EmaldoRealtimeCoordinator`` — uses
  the persistent session's established socket and ``send_command_for_creds()``
  to send type-0x1A override, exactly mirroring the beta16e emergency-charge
  fix. All 3 service handlers (``set_slot_range``, ``apply_bulk_schedule``,
  ``reset_to_internal``) updated to call the session-routed path. Added
  ``stats_override_last_result`` diagnostic attribute surfaced in the realtime
  status sensor for debugging.

## v1.0.0-beta16e

### Fixed
- **ip2_w/op2_w sanity check rejecting whole PowerFlow frame for unpublished
  auxiliary fields:** the ``_has_reasonable_power_flow_values`` validator checked
  offsets 12/14 (ip2_w, op2_w) against the 2000-hectowatt threshold, despite
  these fields carrying raw watts (not hectowatts) and being unmapped to any HA
  sensor. A power-flow packet with op2_w > 2000 raw W was discarded entirely —
  battery_w, solar_w, grid_w were all lost. **Fix:** removed offsets 12/14 from
  the unsigned_offsets validation block. Non-entity auxiliary fields are no longer
  checked. Remaining validated fields now emit ``_log.debug`` on each failure
  with offset, value, and threshold for easier root-cause analysis. The ``* _scale``
  multiplier is also removed from ip2_w/op2_w parsing — they now store raw watts
  in the result dict, matching their actual wire encoding.

- **Shared session emergency charge routed to wrong device in dual-device setup
  (#47 JanBaecklund):** both devices on one account were ``is_primary=True``
  (separate config entries). The shared session (created by PS1) was used by both
  devices. ``send_command()`` encrypted every command with ``self._creds`` (PS1's
  ``chat_secret``), so PS2's emergency charge, third-party PV, sell-back, sell
  limit, and manual selling commands were all sent with PS1's identity — the
  relay applied them to PS1. Power-flow reads worked correctly because
  ``read_power_flow_for_creds()`` already accepted per-device credentials. The
  user observed: activating emergency charge on PS2 physically started charging
  PS1, with no visible change in HA.
  **Fix:** added ``send_command_for_creds()`` to ``PersistentE2ESession`` — same
  pattern as the existing ``read_power_flow_for_creds()``, but for write
  commands. All 5 call sites updated to fetch the calling device's own E2E
  credentials and pass them explicitly. ``_write_sell_back_to_grid`` also fixed
  to read ``user_id`` from the device's own creds instead of the session's
  (previous: always used primary device's ``user_id`` for VPP payload).

## v1.0.0-beta16d

### Fixed
- **120s home secret grace window blocked escalation recovery — 2-min death
  cycle (#47 JanBaecklund):** RC3 extended the home E2E cache grace window from
  5s → 120s to prevent dual-unit ping-pong. This was too aggressive: the
  escalation mechanism sets ``force_home_refresh=True`` after ≥3 generation
  increments within 60s, but ``_get_home_e2e(force_refresh=True)`` at line 789
  blocked the API call when cache age < 120s. The home secret was never rotated,
  so every in-place reconnect (interval ~2s) got 21204 again. The device was
  stuck in a 110–120s death cycle until the cache aged past 120s. During the
  dead window, emergency charge commands failed — the stream path returned
  CONN_NOT_ESTABLISHED and the legacy standalone path's session was invalidated
  by concurrent stream storm activity. **Fix:** reduce grace window back to 5s.
  With RC1 (re-handshake first) and RC4 (escalation + re-arm) in place, the 5s
  window is sufficient to prevent race conditions between concurrent device
  refreshes without blocking recovery. The escalation generation counter (≥3 in
  60s) provides the rate limiting that prevents reciprocal ping-pong.

## v1.0.0-beta16c

### Fixed
- **Stream reconnect silently marked successful after second handshake also
  failed with 21204 — single-device accounts never recovered (#41 InterceptorDK,
  #47 JanBaecklund):** ``_stream_reconnect_locked`` tried re-handshake with
  current creds, then (on failure) refreshed creds and tried again. The second
  ``_reconnect()`` result was never checked. ``_do_handshake`` does not raise
  on 21204 — it records the response. So code fell through to
  ``_stream_needs_reconnect = False`` even with a dead session. The stream then
  sat healthy-flagged while no frames arrived. The ``_stream_needs_creds_refresh``
  flag was consumed by ``_refresh_creds_locked`` on the first attempt, so every
  subsequent retry used ``force_refresh=False`` → cached stale creds → still
  21204. The generation counter could never reach the home-refresh escalation
  threshold (≥3 within 60s). **Fix:** after the second ``_reconnect()``, check
  ``_last_handshake_response``. If still not ``"ok"``, re-arm
  ``_stream_needs_creds_refresh`` and raise ``EmaldoE2ESessionExpired`` so the
  retry cycle continues with ``force_refresh=True``. The next iteration
  increments the generation counter; after 3+ iterations within 60s the
  escalation triggers ``force_home_refresh=True`` → rotates stale
  ``home_end_secret`` → fresh handshake succeeds. Clear
  ``_stream_needs_creds_refresh`` on successful reconnect to prevent stale flag
  from bleeding into non-21204 reconnect reasons. Based on beta13n's
  ``EmaldoE2ESessionExpired`` raising pattern (same file, line 400+).

## v1.0.0-beta16b

### Fixed
- **`_creds_provider` stale client reference caused REST auth cascade (#47
  JanBaecklund):** the `_creds_provider` closure captured the `EmaldoClient`
  reference at session creation time and never reassigned it. After a REST token
  expiry, `_reset_client()` replaced `self._parent._client` with a new instance,
  but `_creds_provider` still held the old one. Every subsequent forced-credential
  refresh then hit `EmaldoAuthError` on the stale client, triggering another
  `_reset_client()` + `_ensure_client()` — an infinite forced-login loop that
  produced 12+ logins per minute. The server rate-limited the account, returning
  -12 on all REST endpoints for ~60s, causing 21 authentication failures in a 2h
  window. **Fix:** replace the captured `client` with
  `self._parent._ensure_client()` on every call, so the latest client reference
  is always used.

- **Dual-unit 21204 home-secret rotation ping-pong still not fully broken
  (#47 JanBaecklund):** the per-home lock + 5s cache cooldown (beta16) serializes
  concurrent `/home/e2e-login/` calls, but sequential kills still occurred:
  Device A's `_stream_reconnect_locked` calls `_refresh_creds_locked` →
  `force_refresh=True` → `e2e_login(force_home_refresh=True)` when the
  generation counter ≥3 in under 60s. This rotates the account-wide
  `home_end_secret`, expiring Device B's session → B reconnects with
  force_refresh → rotates secret back → ping-pong. The stream was forced to
  fetch fresh cloud credentials on every 21204, regardless of whether the
  existing credentials were still valid. **RC1 fix:** in
  `_stream_reconnect_locked`, try a full re-handshake with the current
  credentials first (UDP-only, no cloud API call). Only if the handshake returns
  non-`"ok"` does the code fall back to `_refresh_creds_locked` + a second
  reconnect. In the common case (creds still valid) the entire reconnect stays
  UDP-local. **RC3 fix:** extend the ``_get_home_e2e`` cache grace window from
  5s to 120s so Device B's escalation reuses Device A's refreshed home secret
  instead of making its own API call that would rotate the secret back. JanBaecklund
  beta16b log confirmed: 50+ 21204 events in 3 min despite RC1 fix; the 5s
  window was far too short for the ~2 min ping-pong cycle.

## v1.0.0-beta16

### Fixed
- **Dual-device home-secret rotation ping-pong permanently broken (#47):**
  the shared ``/home/e2e-login/`` endpoint rotates the account-wide
  ``home_end_secret`` server-side on every call. When two devices shared one
  account, Device A's escalation (3+ urgent credential refreshes in under 60s)
  called ``e2e_login(force_home_refresh=True)``, rotating the home secret.
  Device B's session then received a ``force-logout`` JSON datagram from the
  relay (home-end-secret mismatch) → 21204 storm → Device B escalated in turn,
  rotating the secret back → reciprocal 21204 on Device A → infinite ping-pong.
  Three-part fix:
  1. **Per-home serialization lock** in ``_get_home_e2e``: concurrent
     ``/home/e2e-login/`` calls for the same ``home_id`` are serialized by a
     per-home ``threading.Lock``. A 5-second grace window prevents back-to-back
     rotations when two devices escalate simultaneously — the second device
     reuses the cache instead of calling the API again.
  2. **Home secret rotation callbacks:** ``_get_home_e2e`` fires registered
     callbacks on every rotation (and on cache-reuse for sessions that
     registered late). Each live ``PersistentE2ESession`` updates its in-memory
     home credentials via ``rekey_home()`` — the next keepalive automatically
     carries the new secret, so the relay never detects a mismatch and never
     sends ``force-logout``. The mutual ping-pong is eliminated at source.
  3. **Force-logout detection in stream drain loop:** ``_stream_drain_locked``
     now attempts decryption with ``home_end_secret`` when a datagram matches
     no power-flow or regulate-frequency pattern. Detects
     ``{"cmd":"force-logout"}`` and triggers a clean reconnect with a home-level
     credential refresh, providing a safety net if the callback re-key
     mechanism is not yet in place during a rolling upgrade.

- **``PersistentE2ESession.rekey_home()`` method added:** updates
  ``home_end_id``, ``home_group_id``, ``home_end_secret``, and
  ``home_chat_secret`` in the live session's ``_creds`` dict. No UDP
  re-handshake needed — the next keepalive picks up the new values and the
  relay-side ledger is already updated by the ``/home/e2e-login/`` call that
  triggered the re-key.

- **``EmaldoClient.register_home_secret_callback()`` added:** returns an
  unregister callable. The coordinator's ``_ensure_session`` registers one
  callback per session, capturing the session reference at registration time.
  ``_invalidate_session_ref`` unregisters the callback on session teardown.

- **``DECRYPTED-BUT-REJECTED`` debug logging rate-limited (#41):** the
  diagnostic log in ``decrypt_response`` previously emitted one line per
  (nonce, offset) candidate where AES decrypted cleanly but the payload
  validator rejected it — ~14 lines *per key* per non-power-flow packet
  (relay ACKs, status pushes), flooding the log on any debug-enabled install.
  Rejections are now coalesced into a single periodic line (60s window) that
  reports the event count plus one sample payload, so the #41 signal is
  preserved without the flood. The first rejection in a window is logged
   immediately so a genuine single event is never swallowed.

- **Stream thread self-deadlock on home secret callback (#47 emergency charge hang):**
  ``_refresh_creds_locked`` called ``_creds_provider`` while holding
  ``self._lock``. The provider chain (``_get_home_e2e`` → home secret callbacks
  → ``rekey_home()``) tried to re-acquire ``self._lock`` on the same thread —
  a classic self-deadlock with a non-reentrant ``threading.Lock``. Once
  deadlocked, every ``send_command()`` (emergency charge ON/OFF, REGULATE_FREQ)
  blocked forever trying to acquire the same lock. Fix: release ``self._lock``
  before calling ``_creds_provider``, re-acquire after, with ``self._closed``
  check on resume.

- **``UnboundLocalError`` in ``decrypt_response`` on module-level rate-limit
  globals:** the ``DECRYPTED-BUT-REJECTED`` rate-limiting counters
  (``_decrypt_rejected_count``, ``_decrypt_rejected_window_start``,
  ``_decrypt_rejected_sample``) were assigned inside the function body,
  making Python treat them as local variables. First access threw
  ``UnboundLocalError`` on any code path that entered the DEBUG rate-limit
  block (``_try_parse_battery``, ``_try_parse_power_flow``). This broke E2E
  sensors, battery module scanning, and schedule overrides. Fix: add
  ``global`` declaration for the three module-level variables.

## v1.0.0-beta15i-diagnostic2

### Added
- **Decrypted-but-rejected payload logging (#41):** the previous diagnostic
  build could not distinguish a *wrong-IV* decrypt failure from a *successful
  decrypt whose payload the validator rejected*. Since hardware limits cap
  real power at ~20 kW (far below the 200 kW ``_POWER_FLOW_MAX_RAW_HECTOWATTS``
  gate), the load-dependent stall is almost certainly a **validator/format
  rejection of a correctly-decrypted high-load payload** (e.g. an extended
  >24-byte 0x30 frame when batteries/cabinets are active) — not a crypto
  problem. ``decrypt_response`` now logs ``DECRYPTED-BUT-REJECTED`` with
  ``payload_len`` and ``payload_hex`` whenever AES decrypts cleanly but
  ``payload_validator``/accepted headers reject it. This is the evidence that
  breaks the tie between the two root-cause hypotheses.

## v1.0.0-beta15i-diagnostic

### Added
- **Diagnostic logging for decrypt failure analysis (#41 #47):**
  ``decrypt_response`` now logs success with nonce, offset, key length, and
  **full untruncated response hex** on successful decrypt. ``read_power_flow``
  and ``_try_parse_power_flow`` emit full response hex via ``_LOGGER.debug``
  on every decrypt failure. This enables analysis of the new relay response
  format (``e3`` header, ``90b7`` CT tag, extra ``a0`` byte) without
  truncation at byte 128.

## v1.0.0-beta15i

### Fixed
- **Schedule services no longer overwrite Battery Range settings on omitted
  fields (#50):** ``set_slot_range``, ``apply_bulk_schedule``, and
  ``reset_to_internal`` previously injected ``high_marker=72`` /
  ``low_marker=20`` (from ``vol.Optional(default=...)``) and
  ``battery_range_override=False`` into the E2E override packet whenever
  the caller omitted those fields — silently disabling Battery Range Override
  and resetting the user's reserve markers. The schema defaults are removed;
  omitted fields now fall back to the **current device state** (read via
  ``get_overrides()`` before writing), preserving whatever the user has
  configured. The ``battery_range_override`` field is also exposed as an
  optional schema field across all three services for callers that explicitly
  want to change it.

- **``set_override`` docstring corrected (#50 follow-up):** the docstring
  claimed ``False`` "leaves the AI Battery Range setting unchanged" — it
  actually writes byte 2 = 0x00, which actively disables Battery Range
  Override. Updated to state the real behaviour.

## v1.0.0-beta15h

### Fixed
- **Stream credential refresh no longer crashes on stale REST session
  (`EmaldoAuthError` in `_creds_provider`):** the stream thread's credential
  refresh captured a reference to `SharedEmaldoClient` at construction time. If
  the REST session expired (S390 error from `api.emaldo.com`), calling
  `get_e2e_credentials()` on the stale client raised `EmaldoAuthError` — which
  propagated through the refresh callback into the stream thread's catch-all,
  where it was swallowed as a generic `Stream creds refresh failed` every 2-3
  seconds. No fresh E2E credentials ever reached the stream. Now
  `_creds_provider` catches `EmaldoAuthError`, calls `_reset_client()` +
  `_ensure_client()` for a fresh REST login, and retries. Confirmed by log:
  zero "Stream creds refresh failed" after fix.

- **Dual-unit 21204 loop not broken by device-only credential refresh
  (#47 JanBaecklund):** when two Power-Stores share an Emaldo account, either
  device's `e2e_login(force_home_refresh=True)` rotates the home-level secret
  server-side. The other device derives its `chat_secret` from the now-stale
  cached home secret, producing a credential the relay rejects with 21204.
  Device-only refreshes (`force_home_refresh=False`) keep returning bad
  chat_secrets in this case. `_get_e2e_credentials` now escalates to
  `force_home_refresh=True` after 3+ consecutive force-refreshes in under 60
  seconds (detected by the cache entry's generation counter), so a persistently
  rejected device eventually re-joins the home. Recovery after a relay wedge
  drops from ~90s to ~14s.

- **Single-device 21204 / 100% decrypt failure in legacy fallback (#41
  InterceptorDK):** after the relay server-side change at solar-start, PowerFlow
  responses omitted the `\x10\xa3` response-IV marker — `decrypt_response` used
  only the request nonce (from `\x90\xa3`) as AES-CBC IV, which failed padding
  and returned `None`. Three-pronged fix:
  1. `decrypt_response` now accepts a `fallback_ivs` parameter — callers pass
     the session nonce (`session_nonce`) as an additional IV candidate, tried
     after marker-extracted nonces.
  2. Fallback to `home_chat_secret` when device `chat_secret` fails decryption
     (the relay may now encrypt responses with the home-level key instead of
     the per-device key).
  3. Diagnostics now log `key_len`, marker presence, and all tried nonce hex
     in the debug-level "decrypt_response" message.

## v1.0.0-beta15g

### Fixed
- **21204 storm on dual-device setups caused by lambda TypeError (#47
  JanBaecklund):** ``self._log(...)`` in ``e2e.py`` was called with printf-style
  format + 2 positional arguments (3 args total), but ``coordinator.py`` provides
  a ``lambda msg:`` that takes exactly 1 argument. The resulting ``TypeError``
  propagated out of ``_stream_drain_locked`` into ``_stream_loop``'s catch-all
  exception handler, which called ``_stream_flag_reconnect("loop_exception:
  TypeError")`` — triggering a full session reconnect on every alt-key decrypt.
  With two devices (PS1+PS2) pushing frames, this produced a ~2-5s reconnect
  cycle. Converted the printf-style call to f-string so the call site matches the
  1-arg lambda. The stream now stays alive across alt-key decryption.

### Removed
- **Misleading ``start_stream`` log-after-return:** the "Stream receiver started"
  message printed unconditionally after the early-return guard, implying a fresh
  startup on every call even when the stream was already running.
- **Dead race-check code in ``_ensure_session``:** the defensive race check
  (``coordinator.py:1010-1023``) assumed dual config entries sharing one session.
  With single-entry multi-device discovery this path is never exercised. Removed.

### Fixed
- **Service handlers crash on ``_home_sessions`` metadata in ``hass.data[DOMAIN]``:**
  ``_home_sessions`` and ``_home_session_owners`` (added by #47) are stored at the
  same level as config-entry data in ``hass.data[DOMAIN]``. All 7 service-handler
  loops that iterated ``.values()`` picked up these dicts, causing:
  - ``async_handle_set_ev_schedule``: crash (``KeyError`` on ``item["power"]``)
  - ``_get_target_set(device_id=None)``: crash (``KeyError`` on ``result["schedule"]``)
  - ``async_handle_apply_bulk_schedule``: false-positive warning
    (``item missing 'schedule' key!``, the user-visible symptom)
  Added ``_get_entry_data()`` helper that filters out ``_``-prefixed keys. All
  iteration sites now use it instead of raw ``hass.data.get(DOMAIN, {})``.

- **Legacy-mode high-load stall caused by stale push frames in socket
  buffer (#41 InterceptorDK):** The persistent session's 0x30 subscription
  (``payload=bytes([0x01])``, meaning "subscribe continuously") kept the relay
  pushing frames between polls. Under high solar+battery load the relay pushed
  more aggressively; leftover frames accumulated in the socket buffer and
  polluted the next ``read_power_flow()`` call's initial ``recvfrom``. The first
  response was a stale push (``initial_nonmatching=1``) and the 5-packet drain
  timed out before reaching a fresh power-flow frame (``drain_exhausted=1``).
  Beta9 didn't have this because each poll used a fresh socket.
  **Fix:** ``_read_power_flow_locked()`` now drains stale packets from the socket
  buffer (0.05s non-blocking drain) before sending the 0x30 subscription. The
  initial response is now always a fresh reply to the current poll — no more
  intermittent stalls under high load.

## v1.0.0-beta15f

### Fixed
- **Battery Optimizer `KeyError: 'schedule'` crash:** All five service handlers
  that iterate device sets for post-operation schedule refresh now use
  `item.get("schedule")` instead of `item["schedule"]`, preventing KeyError
  when a device-set dict is missing the "schedule" key. The refresh is
  safely skipped when the key is absent; the schedule coordinator catches up
  on its next periodic interval. Diagnostic ERROR-level logging added to
  capture the missing key and surrounding keys for root-cause analysis.

- **Stream credential-rotation death spiral (#47 JanBaecklund):** Every stream
  reconnect (long_stall, socket error) force-refreshed the device chat_secret,
  turning previously-queued relay push notifications undecryptable — causing
  more stalls → more reconnects → 89% unparsed packets. Fix: only force-refresh
  E2E credentials when the reconnect is triggered by an actual 21204 (session
  expired). `long_stall` and socket-error reconnects reuse existing credentials,
  breaking the self-inflicted decrypt failure loop.

- **Cross-device packet decryption (#47 multi-config-entry):** When two Power-
  Stores share the same E2E account, the relay pushes each device's encrypted
  notifications to all open sessions. The stream now tries EVERY registered
  device's `chat_secret` on each received datagram (try-all-keys). Packets from
  device B are no longer discarded as `unparsed` by device A's stream — they
  are decrypted and cached per device.

- **Stream frame cache key mismatch (single-unit data unavailable):** The per-
  device power-flow cache stored frames keyed by `sender_end_id` (E2E end_id)
  but `get_latest_power_flow(device_id=...)` looked up by API `device_id`
  — different strings → every read returned None. Fixed: `_stream_drain_locked`
  now reverse-lookup the API `device_id` from `_device_key_registry` using the
  own `chat_secret`; `_ensure_session` registers the device key **before**
  starting the stream so the registry is populated on the first drain.

- **`AttributeError: _device_id` on single-unit setup:** `_read_power_flow`
  referenced `self._device_id` (private attr of `EmaldoCoordinator`) but
  `EmaldoRealtimeCoordinator` extends `DataUpdateCoordinator` directly — the
  attr does not exist. Changed to `self.device_id` / `self.device_model`
  (public properties that delegate to parent).

### Added
- **Diagnostic debug logging for traceability:**
  `async_handle_apply_bulk_schedule` entry (call data keys),
  `_get_target_set` (entry_ids, returned item keys, schedule-key presence),
  `_get_coordinator_and_client` (target_set keys, type).

- **Per-device power flow cache (`PersistentE2ESession`):** `_latest_power_flow`
  changed from a single `dict | None` to a `dict[str, dict]` keyed by device_id.
  Each coordinator reads only its own device's cached frame via
  `get_latest_power_flow(device_id=...)`. Cross-device data no longer overwrites.

- **Device key registry (`PersistentE2ESession._device_key_registry`):**
  Class-level `{home_id: {device_id: chat_secret}}` dict populated by each
  coordinator during `_read_power_flow`. The stream drain loop references it
  for try-all-keys decryption.

- **TLV raw-hex logging on cross-device decrypt hit:** When a packet is
  successfully decrypted with an alternate device's key, the first 48 bytes
  of the raw packet are logged as hex — enabling future development of a
  cleartext TLV parser to skip the try-all-keys loop.

## v1.0.0-beta15e

### Added
- **Diagnostic debug logging for Power Core 2.0 0x30 parse failure (#41):** six
  targeted log sites now dump raw response hex, nonce-marker presence, decrypt
  stage (success/failure/exception), and parse stage — all at DEBUG level.
  No behavioral change; normal operation unaffected until debug logging is
  enabled.

## v1.0.0-beta15d

### Fixed
- **Two separate config entries for the same home created two E2E sessions
  (relay collision, same as #47):** the shared E2E session was scoped to a
  single config entry. Users with two config entries (one per device on the
  same Emaldo account) each got their own session — both sent ``Alive(home)``
  with the same ``home_end_id``, causing the relay to invalidate the first
  session when the second ``Alive(home)`` arrived. The session is now keyed
  by ``home_id`` across all config entries of the same home. The first entry
  to create the session is recorded as its owner; only the owner closes the
  session on shutdown, so the stream survives an entry reload of the other
  device.

## v1.0.0-beta15c

### Fixed
- **Dual-primary race could still create two E2E sessions (#47):** when two
  coordinators both think they are primary (`_is_primary=True`), both enter
  the session-creation path in `_ensure_session`. The first stores its
  session as shared, then the second overwrites it — leaving two live
  sessions (two stream threads, two `Alive(home)` sequences) and the same
  relay collision as the original #47. Now after storing the session, the
  coordinator re-reads the shared slot. If another session was stored
  concurrently, it closes its own and uses the existing one, so at most one
  session survives regardless of how many coordinators think they are
  primary.

### Added
- **Setup-time logging for multi-device detection:** each device's realtime
  coordinator now prints its `is_primary` flag at setup time so a debug log
  immediately shows whether `__init__.py` deployed correctly
  (`[Setup] device <id> (1/2): realtime coordinator is_primary=True` /
  `(2/2): false` for secondary).

## v1.0.0-beta15b

### Fixed
- **Secondary power-flow read always returned None (decryption used wrong
  key):** `_try_parse_power_flow` called `decrypt_response(resp,
  self._creds["chat_secret"])` — the primary's `chat_secret`. The device
  encrypts the 0x30 response with the sender's `chat_secret` (secondary's), so
  decryption with the primary's key produced garbage and was always rejected by
  the payload validator. Now accepts optional `chat_secret` param; both call
  sites in `_read_power_flow_locked` pass `actual_creds["chat_secret"]`.
- **Emergency charge legacy fallback collision with shared session (#47):** the
  second attempt in `_write_emergency_charge_on/off` calls
  `client.emergency_charge_window/off()` which opens a fresh UDP socket and
  sends `Alive(home)` — would collide with the primary's shared session for
  secondary devices. Now gated: detects paired realtime coordinator's
  `_is_primary` flag before attempt 1; secondary devices skip the legacy
  fallback and raise directly with a warning log.

### Added
- Runtime logging for shared E2E session lifecycle: acquire/rejected for
  secondary, secondary power-flow reads, shutdown no-op (secondary does not
  close shared session), emergency charge device context (`is_primary` flag).

## v1.0.0-beta15

### Fixed
- **Multi-device Deadly Embrace: PS2 stream works, PS1 never gets frames (#47
  RC6):** two devices on the same home share the same `home_end_id`. Each
  device's `Alive(home)` packet registered the home endpoint with the relay,
  and the second send invalidated the first device's session — a ping-pong where
  whichever device sent `Alive(home)` most recently kept its session alive and
  the other hung at 0 frames forever. The config entry now maintains a single
  shared `PersistentE2ESession` owned by the primary (first-discovered) device.
  Secondary devices fetch their own E2E credentials (`sender_end_id`,
  `chat_secret`) but send the 0x30 power-flow subscription through the shared
  session socket via the new `read_power_flow_for_creds()` method — no
  `Alive(home)` is ever sent for secondary devices, so the relay collision is
  eliminated. The primary handles keepalive and re-handshake; secondary
  coordinators read through the shared session and never send `Alive(home)`,
  never start their own keepalive loop, and never close the shared session.

## v1.0.0-beta14e

### Fixed
- **Battery module probe uses device-reported slot index instead of probe
  sequence index (#44 RC5):** `_probe_slot` stored `idx` (the sequential probe
  index) as `scan_index`, overriding the module's self-reported physical
  position. When a late UDP response from the previous slot arrived during the
  next probe's receive window, the module was assigned to the wrong slot,
  causing cascading off-by-one shifts in multi-cabinet setups. Now uses
  `info.get("index", idx)` — the module's own instance index — so even a stray
  response carries its correct slot position. Also fixes the class-method path
  (`PersistentE2ESession.battery_info`). Tested on single-unit setup.
- **Emergency charge toggle disrupts stream, sensors briefly lose values (#47
  follow-up):** the legacy read after toggle (beta14b) opened a fresh UDP socket
  to the E2E relay, invalidating the stream's persistent session (21204 flood).
  Stream reconnect cycle caused sensors to flash unavailable on every ON/OFF.
  Removed ``_async_force_realtime_refresh_after_charge`` entirely — the stream
  recovers naturally within 1-2 polls, and the legacy command fallback already
  covers stream failures.
- **``emergency_charge_off`` called ``cancel_sell`` instead of
  ``set_emergency_charge(on=False)`` (#47 follow-up):** both send identical wire
  payloads (9 zero bytes on type 0x01), so this was harmless in practice — the
  fix is for code consistency with the on-path and to remove the confusing
  dependency on the outdated sell-named helper.

## v1.0.0-beta14d

### Fixed
- **Emergency charge toggle crashes with TypeError on HA 2026.12+ (#47 RC4):**
  ``_async_force_realtime_refresh_after_charge()`` called
  ``await realtime.async_set_updated_data(data)``, but ``async_set_updated_data``
  is synchronous (not ``async def``) in HA 2026.12+ — calling it with ``await``
  executed the method body (which returned ``None``) and then tried to ``await
  None``, producing ``TypeError: 'NoneType' object can't be awaited``. Removed
  the ``await`` so the call matches the integration's existing 12 call sites.
- **``_ensure_session`` thread-safety crash on dual-inverter setups (#47 RC5):**
  multiple executor threads (SyncWorker_N) raced on ``self._session`` during
  emergency charge toggle. Thread A created a new session and entered the
  first-frame wait loop; thread B found ``_session_binding=None``, closed A's
  session, and set ``self._session=None``, so A crashed at
  ``AttributeError: 'NoneType' object has no attribute 'get_latest_power_flow'``.
  The session binding is now set before the frame-wait loop, and a local
  reference protects the loop from cross-thread ``self._session`` replacement.
- **Emergency charge command depends on broken stream in multi-device setups
  (#47 RC5):** ``_write_emergency_charge_on/off`` only used the stream path
  (``_send_emergency_charge_via_stream``), which returns ``CONN_NOT_ESTABLISHED``
  when the relay keeps rejecting handshakes with 21204 session expired. Added a
  legacy fallback: after the stream path fails, retry once via the standalone
  legacy E2E command (``client.emergency_charge_window / off``) with fresh
  credentials, bypassing the broken stream entirely.

## v1.0.0-beta14c

### Fixed
- **Battery module slot permanently locked by stray late packet (#44 RC2):**
  when a race or slow response caused a module's serial to be recorded at the
  wrong slot, the ``known_serial_slots`` check in ``_probe_slot`` permanently
  rejected the correct serial from its rightful slot. Removed the
  serial-slot rejection — the pre-probe drain already catches ~99% of stray
  packets, and accepting the rare false assignment beats a permanent slot lock.
- **Emergency charge toggle crashes on HA 2026.12+ with thread-safety
  RuntimeError (#47 RC3):** ``_force_realtime_refresh_after_charge()`` ran on
  the executor thread and used ``run_coroutine_threadsafe`` to schedule
  ``async_set_updated_data``. HA 2026.12+ hardened the thread-safety check in
  ``async_write_ha_state``, making cross-thread origin a hard RuntimeError even
  with ``run_coroutine_threadsafe``. The method is now async; the blocking
  legacy read runs via ``async_add_executor_job`` and
  ``async_set_updated_data`` is called with ``await`` from the event loop.
  The call is moved from the executor functions to ``switch.py``, which is
  already on the event loop after the executor job completes.

## v1.0.0-beta14b

### Fixed
- **Sensor entity duplication on reload after device binding (#41 RC1):** the
  legacy UID detection checked only for ``{home_id}_battery_soc`` in the entity
  registry. If that specific sensor was absent (e.g. certain Power Core models,
  or a partial migration), the check missed all legacy entities and the
  ``_uid_base`` switched from ``home_id`` to ``device_id``, creating orphan
  duplicates. Detection now matches **any** ``{home_id}_`` prefix in the
  entry's existing unique IDs, so legacy mode is activated correctly as long as
  at least one sensor from a previous version exists.
- **Power Core 2.0 high combined output rejected by sanity filter (#41 RC3):**
  the ``REALTIME_POWER_ABS_MAX_W`` threshold was set to 10 kW. Power Core
  2.0 systems can exceed this during simultaneous solar + battery + grid export.
  The threshold is raised to 50 k W, keeping safety for malformed payloads
  without blocking legitimate data from high-capacity installations.
- **Emergency charge toggle not reflected in battery_w sensor (#47):** after
  emergency charge ON/OFF, the paired realtime coordinator's stream session
  could hold stale power-flow data from another device in multi-device setups
  (cross-device frame pollution). The toggle now forces a device-specific
  one-shot legacy E2E read and pushes the result into the realtime coordinator
  data, so ``battery_w`` (and related power sensors) reflect the new charge
  state immediately instead of waiting for the next interleaved stream frame.

## v1.0.0-beta14

## v1.0.0-beta13r

### Fixed
- **Emergency charge ON/OFF silently fails when handshake timeouts or relay
  rejects the command:** The one-shot E2E functions (`set_emergency_charge`
  and `cancel_sell`) ignored handshake step failures (Alive/Wake/Heartbeat
  returning ``None``) and accepted *any* non-``None`` response as success.
  When the Heartbeat timed out (3 s), the code continued to send the command
  on a broken session, and the relay replied ``CONN_NOT_ESTABLISHED`` — which
  was treated as ``result=True`` because ``resp is not None`` evaluated to
  ``True``. The coordinator then also ignored the boolean return value,
  setting ``_emergency_charge_active`` regardless of the actual outcome.
  The device kept charging and the user had to toggle again to stop it.
  **Fix in ``cancel_sell`` / ``set_emergency_charge``:**
  - Abort early (return ``False``) if any handshake step returns ``None``.
  - Reject responses containing the ASCII error ``CONN_NOT_ESTABLISHED``.
  **Fix in ``_write_emergency_charge_on/off``:** check the boolean return
  value and raise ``EmaldoE2ESessionExpired`` on ``False``, which triggers
  the existing retry logic (invalidate session cache → fresh credentials →
  reattempt). After two failed attempts the exception propagates to HA,
  which logs the error and leaves the switch unchanged.

### Changed
- Moved ``_close_realtime_session()`` from *after* the one-shot command to
  *before* it (beta13q placed it after, but the 21204 kick happens during
  the handshake, too early for a post-command close). Also added
  ``future.result(timeout=5)`` so the executor thread waits for the async
  close to actually complete before opening the one-shot socket.

## v1.0.0-beta13q

## v1.0.0-beta13p

### Added
- **Emergency charge E2E diagnostic logging:** `set_emergency_charge()` in
  `e2e.py` now logs each handshake step (Alive/Wake/Heartbeat/Command) with
  sent/received byte counts, round-trip time, and response hex prefix — plus
    timeout detection — under the `[EmergencyCharge]` prefix. The OFF
    path (`cancel_sell` → `emergency_charge_off`) also logs the same handshake
    details when the label mentions "emergency"/"charge". Coordinator
    `_write_emergency_charge_on/off` already logged timestamps and device ID from
    beta13o. This lets a single debug-log capture show whether a toggle's E2E
    handshake completed, which step failed, and what the relay replied, per device.
- **Power-flow data logging:** `EmaldoRealtimeCoordinator._async_update_data` now
  logs parsed power-flow values (`battery_w`, `solar_w`, `grid_w`, `soc`) under
  the `[PowerFlow]` prefix on every successful read. This shows whether HA is
  actually receiving the correct realtime data from the E2E stream.

## v1.0.0-beta13o

### Fixed
- **Cross-device 21204 cascade on emergency charge / sell / EV commands (#47
  follow-up):** `send_sell()`, `cancel_sell()`, `emergency_charge_window()`
  called `e2e_login()` directly instead of the shared credential cache. That
  rotated the per-device `chat_secret` server-side, immediately expiring the
  active stream session (21204). On a multi-device account, device A's stream
  reconnect then force-refreshed the *home* secret (via
  `force_home_refresh=True` in `_get_e2e_credentials`), rotating it out from
  under device B's live session — a mutual ping-pong that neither device could
  escape. All one-shot E2E operations now use `get_e2e_credentials()` (the
  shared cache), and `_get_e2e_credentials(force_refresh=True)` no longer
  propagates `force_home_refresh=True`, so a device-only credential refresh
  never rotates the shared home secret.
- **Emergency charge toggle unreliability after E2E session expiry:** if the
  E2E session expired (21204) between the user toggling the switch and the
  command reaching the relay, the command silently failed and the coordinator's
  optimistic state (`_emergency_charge_active`) permanently diverged from the
  device. `_write_emergency_charge_on/off` now catch `EmaldoE2ESessionExpired`,
  invalidate the stale cache entry, and retry once with fresh credentials so
  the toggle succeeds even when the session was stale.

### Changed
- **Emergency charge write in coordinator.py:** `_write_emergency_charge_on/off`
  now invalidate cached E2E credentials on `EmaldoE2ESessionExpired` + retry
  once, matching the pattern from `_run_e2e_with_refresh_retry`.
- **Credential refresh cascade blocked in client.py:**
  `_get_e2e_credentials(force_refresh=True)` no longer passes
  `force_home_refresh=True` to `e2e_login`. Home secret rotation happens only
  on its own 30-minute TTL, so one device's transient 21204 recovery never
  punches the other device offline.

## v1.0.0-beta13n

### Fixed
- **Realtime session reported "healthy" while no data ever arrived (#47):** the
  realtime coordinator returned the last-known data on every empty read, which
  marks the Home Assistant update as *successful*. Before the first successful
  read there is no last-known data, so the coordinator kept reporting success
  with `None` data — the integration looked healthy while every entity stayed
  unavailable (a reporter saw 180+ consecutive "success" polls with no data).
  The coordinator now raises `UpdateFailed` when it has never produced a valid
  read and the failures pass the tolerance threshold, so HA surfaces the real
  state and keeps retrying. Established sessions still keep their last values on
  a transient gap (unchanged).
- **Every session-expiry (21204) wasted a full poll (#41):** on a 21204 the
  session re-handshaked in place and then *deferred* the power-flow read to the
  next poll. When the relay expired the session on each first read, this turned
  every poll into a guaranteed empty read and could chain indefinitely. The
  read is now retried immediately on the refreshed session; a second 21204 in
  the retry returns cleanly without looping.
- **Confirmed-failure credential refresh did not rotate the home secret
  (#41, #47):** a forced credential refresh (after a confirmed session expiry
  or decrypt failure) rotated only the per-device `chat_secret` and reused the
  cached account-level home login for up to its 30-minute TTL. The forced path
  now also refreshes the home secret (matching beta9, which both reporters
  confirm is stable), while the routine TTL-expiry path still reuses the cached
  home login so it does not disturb another device's live session (#47).
- **Misleading stall diagnosis:** the "switch to poll mode" advisory always
  blamed a restrictive NAT/firewall even when the relay *was* delivering
  packets that simply could not be decrypted. The advisory now classifies the
  stall from the stream diagnostics: it only cites NAT/firewall when no packets
  are arriving, and otherwise reports a credential/decryption failure (for
  which poll mode may not help) and asks for a log on the issue tracker.

### Added
- **Automatic legacy (beta9) compatibility fallback (#41, #47):** when the
  persistent/stream session is fully reset several times with no successful
  read in between, the coordinator latches into the beta9 read model — a fresh
  UDP socket + handshake + single power-flow read per poll, with a one-shot
  fresh re-login on failure. Both #41 and #47 reporters confirm beta9 is stable
  on networks where the persistent/stream session yields no usable frames. The
  fallback stays active until the integration is reloaded and is exposed as the
  `legacy_fallback_active` attribute on the realtime connection diagnostics
  sensor.

## v1.0.0-beta13m

### Fixed
- **Duplicate AI Battery Range entities ("double sensors") after upgrading to
  beta13l (#47):** beta13l's phantom-device fix seeded the schedule
  coordinator's `device_id` at construction, but that value also feeds
  `_uid_base()`, which builds the AI Battery Range entities' `unique_id` for
  non-legacy/fan-out devices. Seeding it changed those unique_ids, so Home
  Assistant created a second copy of each entity (smart/emergency markers and
  the override switch) alongside the originals — worst on multi-device setups,
  and enough extra entity churn to overload low-power hosts (a reporter's Pi
  choked). The seeding is reverted so unique_ids match pre-beta13l releases and
  no duplicates are created. Users who ran beta13l will have orphaned duplicate
  entities left in the registry; these show as unavailable and can be deleted
  from Settings → Devices & services. (The original phantom empty-device
  cosmetic issue that beta13l tried to fix returns for now and needs a
  unique_id-safe reimplementation — fix `device_info` without touching the uid
  base.)

## v1.0.0-beta13l

### Fixed
- **Two devices on one account stalled each other's realtime session — both
  stream and poll mode (#47):** every per-device `e2e_login` re-ran the
  account-level `/home/e2e-login/` endpoint, which rotates the shared home
  `end_secret` server-side. That secret is baked into the `home_alive`
  keepalive/handshake packets of *every* device session on the account, so one
  device logging in (its 10-minute credential refresh, or any 21204-triggered
  re-login, or an EV/emergency/override command) rotated the secret out from
  under the *other* device's live session — expiring it (21204), which forced
  *that* device to re-login and rotate the secret back, a mutual ping-pong that
  capped realtime success around ~39 % and froze sensors like battery power.
  Switching transport (stream ↔ poll) could not help because both share the
  same session and `home_alive` packets. The account-level home login is now
  cached per `home_id` and reused by all of the account's device sessions, so a
  per-device login no longer rotates the shared home secret; only the
  per-device `chat_secret` still refreshes per device. Single-device accounts
  are unaffected. (Home credentials still refresh on their own 30-minute TTL and
  on a full client reset, so a genuinely stale home secret still self-heals.)
- **Disabling then re-enabling the integration spawned a phantom "Emaldo
  Battery" device:** the AI Battery Range entities are backed by the schedule
  coordinator, whose device identity (`device_id`/`model`/`name`) was only
  synced from the parent lazily during its first background data fetch. On
  re-enable, entities are added during platform setup *before* that fetch runs,
  so their `device_info` rendered with `device_id=None` and attached to a second
  device keyed by `home_id` and named "Emaldo Battery" instead of the real
  device (e.g. "Power Store"). The schedule coordinator now seeds its device
  identity from the parent coordinator at construction (the parent's discovery
  is already complete by then), so these entities render on the correct device
  from the first frame. Any empty phantom "Emaldo Battery" device left by a
  previous version can be deleted from Settings → Devices & services.
  **(Note: reverted in beta13m — this seeding changed entity unique_ids and
  caused duplicate sensors; see #47.)**

### Added
- **"Switch to poll mode" recommendation when the stream keeps stalling
  (#41):** on networks that drop the device's push datagrams (restrictive
  NAT/firewall/CGNAT), stream mode repeatedly force-resets (`stream_stall_reset`)
  and the realtime power sensors (e.g. battery power) freeze on their last value
  — no client reset can recover frames that never arrive. After
  `_STREAM_STALL_POLL_HINT_RESETS` (3) consecutive stream resets without a
  successful read in between, the coordinator now logs a one-time WARNING telling
  the user to turn off "Realtime stream mode" (Settings → Devices & services →
  Emaldo Battery → Configure) and use the NAT-friendly poll model. This is
  advisory only — the transport is not changed automatically.

### Changed
- **Auxiliary state reads throttled in stream mode to reduce receiver
  contention (#47):** the balancing / sell-back-to-grid / sell-limit /
  manual-selling states are read on the shared realtime session every 6th
  successful power-flow read. In stream mode these four sequential reads each
  briefly hold the session lock and can consume/discard buffered `0x30` push
  frames meant for the background receiver. Their cadence is now 4x looser in
  stream mode (every 24th read, ~2 min, vs. ~30 s) so they disturb the stream
  less; the states are all slow-changing / user-toggled so responsiveness is
  unaffected. Poll mode is single-threaded with no receiver to starve and keeps
  the tighter cadence. (This trims avoidable contention; it is not a fix for the
  network-level frame drops that cause the stalls — use poll mode for that.)

## v1.0.0-beta13k

### Fixed
- **Poll mode can no longer wedge indefinitely on a stale relay binding (#41):**
  poll mode previously had no counterpart to stream mode's `stream_stall_reset`
  escalation — a run of empty-read reconnect cycles only closed and rebuilt the
  session while reusing the cached 10-minute E2E credentials, so a dead relay
  binding (e.g. after a cloud hiccup) could keep re-handshaking with stale creds
  until an HA restart. After `_POLL_STALL_RESET_RECONNECTS` (3) reconnect cycles
  without recovery, the coordinator now resets the shared REST client and
  rebuilds the session with **freshly fetched** credentials (`poll_stall_reset`
  reconnect reason), firing periodically thereafter to avoid hammering the cloud
  API during a sustained outage. This restores beta9's implicit
  fresh-login-on-every-reconnect behaviour.
- **Failed session rebuilds now force fresh credentials:** any session teardown
  that follows a confirmed failure (empty-read stall, undecryptable responses,
  stream stall) now sets a flag so the next `_ensure_session` pulls
  `force_refresh` credentials instead of the shared cache. The 10-minute cache
  is still used on the happy path but never survives a confirmed session
  failure.
- **Keepalive no longer counts relay silence as success:** a keepalive that got
  no reply to its alive packet previously returned success, masking relay
  unresponsiveness and feeding false positives into the healthy-keepalive
  reconnect-deferral logic. It now reports a distinct `response_timeout` failure
  (a single stray timeout is still tolerated — the loop needs two in a row), so
  "healthy keepalive" once again means "the relay actually replied".

### Added
- **Stale-credential (undecryptable-response) detection:** poll mode now tracks
  reads where the relay *answered* but no frame could be decrypted/parsed — the
  stale-`chat_secret` signature, distinct from an empty read (no response at
  all). After `_UNDECRYPTABLE_RESET_STREAK` (6) consecutive such polls it logs a
  WARNING and forces a fresh re-login, and exposes a lifetime
  `undecryptable_polls` diagnostic.
- **Stall snapshot diagnostic:** when the rolling success window first goes
  fully cold (≥12 consecutive failed polls) the coordinator freezes a one-shot
  `stall_snapshot` of the key counters (power-flow diagnostics, stream
  diagnostics, last handshake response, RTT, reconnect state, timestamp) and
  logs it. Since users typically report a stall hours after onset, this
  preserves the state that actually matters for diagnosis. Re-armed once a
  successful poll re-warms the window.
- **Richer stall logging and diagnostics:** the stream wedged warning now
  appends `stream_diag`, and the new poll-stall warning appends
  `powerflow_last_diag`, so the discriminating counters (relay silent vs.
  responded-but-unparseable) survive a copy-paste of the log line. The
  connection diagnostic sensor gains `keepalive_failures_response_timeout`,
  `undecryptable_polls`, `last_handshake_response`, and `stall_snapshot`
  attributes.
- **Handshake responses are now validated for diagnostics:** the persistent
  session records whether each handshake actually drew a reply from the relay
  (`ok` / `no_response` / `session_expired_21204`) instead of being purely
  fire-and-forget, so reconnect counts can be distinguished from genuine
  session re-establishment.

## v1.0.0-beta13j

### Added
- **Realtime mode is now selectable (Stream vs Poll) in the integration options
  (#41):** the subscribe-and-stream model (beta13d+) relies on the device
  *pushing* UDP frames to Home Assistant. On some networks (restrictive
  NAT/firewall/CGNAT) those device-initiated datagrams are dropped, so realtime
  data stalls even though the official app keeps updating — the coordinator's
  recovery fires repeatedly (`stream_stall_reset`) but no client reset can make
  the router deliver frames that never arrive. Settings → Devices & services →
  Emaldo Battery → Configure now offers a **Realtime stream mode** toggle; turn
  it off to use the legacy request/response **poll** model, which traverses NAT
  reliably. Changing the option reloads the entry so it takes effect
  immediately. Default remains stream mode.

### Fixed
- **Battery module can no longer briefly show a neighbour's values (#44):** the
  ~5-minute battery scan probes cabinet slots one at a time. If a module's
  rightful slot timed out in a given scan, a late reply from that module could
  land in the *next* slot's receive window (after the pre-probe drain) and be
  misassigned — e.g. module 8 showing module 7's values while module 7 went
  un-updated. The scan now carries a serial → slot map from previous scans and
  rejects any reply whose serial is known to belong to a different slot, so a
  stray late datagram is discarded instead of contaminating the neighbouring
  slot. This is independent of the device's own index fields, so it is safe
  across multi-cabinet (HP5000) systems.

## v1.0.0-beta13i

### Changed
- **Stream wedged full-reset escalation now fires at 120 s (was 180 s):**
  `STREAM_STALL_FULL_RESET_SECONDS` lowered from 180 s to 120 s (24 polls at the
  5 s cadence). Overnight logs showed the beta13g escalation working correctly
  but only after ~3 minutes; 120 s recovers a "dead REST token but API is up"
  wedge sooner while still sitting well above the 45 s long-stall watchdog, so a
  healthy in-place self-heal never triggers it. (During a genuine
  `api.emaldo.com` outage, recovery is still bounded by the API returning, not by
  this threshold.)

### Fixed
- **Override/schedule writes now retry on transient E2E errors, not just auth
  (fixes intermittent "Failed to apply bulk override"):** the `set_slot_range`,
  `apply_bulk_schedule` and `reset_to_internal` service handlers previously
  retried only on `EmaldoAuthError` and gave up — dropping the write — on a
  transient `EmaldoE2EError`/`EmaldoConnectionError` or a `set_override` that
  returned `False`. So a brief relay/session hiccup (e.g. a 21204) during a
  battery-optimizer or automation write silently failed. These writes now make
  up to 3 attempts, resetting the session between tries and retrying on auth,
  E2E and connection errors as well as a transient rejection, with a 1 s
  backoff; an error is logged only after all attempts fail. This brings the
  write path up to the resilience the realtime read/write paths already had.
- **Total Energy sensor no longer shows sharp 5-minute dips (#41):** the sensor
  summed `current_energy_wh` over the *latest* battery scan's module list, but a
  scan runs only every ~5 minutes on a one-shot session and not every module
  answers every scan on multi-module systems. A partial scan therefore dropped
  the total for one 5-minute interval before the next full scan restored it,
  producing the sharp saw-tooth history reported in #41. Total Energy (and its
  `maximum_capacity_wh` / `module_count` attributes) now sums over the retained
  per-slot module map, which keeps each slot's last-known value while it is
  briefly silent — so the total stays stable and now matches the sum of the
  per-module energy sensors.

## v1.0.0-beta13h

### Fixed
- **Background threads left "still running" during Home Assistant shutdown
  (#46):** the persistent E2E session was only closed when the config entry
  unloaded, which happens late in the shutdown sequence. Until then the
  realtime coordinator's background stream receiver and keepalive threads
  stayed blocked in `recvfrom()` on the open socket, so Home Assistant logged
  `Task ...emaldo_keepalive... was still running after final writes` and
  `Thread ... is still running at shutdown`. The integration now also listens
  for `EVENT_HOMEASSISTANT_STOP` and closes each device's persistent E2E
  session (and cancels its keepalive/battery-scan tasks) as soon as shutdown
  begins, so the blocked socket reads are interrupted promptly instead of
  lingering into the final-writes stage. The existing unload path still runs
  and is a harmless no-op after an early stop.
- **No empty read / `unknown` sensors on the first poll after a restart:** in
  the subscribe-and-stream model the poll that *starts* the stream previously
  returned immediately with no frame cached yet (the device needs a moment to
  finish the handshake + subscribe and push its first frame), so the first read
  after every restart came back empty and the realtime/E2E sensors sat on their
  restored value or `unknown` for up to ~10 s until the next poll. The poll that
  starts a fresh stream session now waits up to `STREAM_FIRST_FRAME_WAIT` (12 s)
  for that first frame and returns it, so poll #1 already delivers live data.
  The wait runs on the executor thread (never the event loop) and the realtime
  coordinator's first refresh is a background task, so Home Assistant startup is
  not delayed; it exits early the instant a frame arrives and falls through to
  the normal keep-last-values path if the device stays silent for the full
  window. This also removes the ~0.1 % cumulative `success_rate_pct` dent that
  the recurring first-poll miss used to cause. (A brief `unknown`/`unavailable`
  window can still remain on startup while the first frame is awaited and the
  entities are re-added; a restored previous reading reduces it when one is
  available, but it is not always fully eliminated. This is inherent to the
  push-stream cold start.)

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
