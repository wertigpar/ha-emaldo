# 21204 storm: coordinator layer still re-arms the loop (beta38 code review)

**Status: DRAFT — analysis only, no code changed.**
Reviewed tree: `8bb5a0c` (beta37/38 mitigations live, under nightly test).

## Verdict

Session-layer mitigations (beta35 home-rotation latch, beta37 frameless backoff
escalation, beta38 plain-21204 no-pre-rotate) are individually correct, but all of
them keep their state on objects that the **coordinator destroys every ~120 s while a
storm is in progress**. During the exact conditions they were written for, they are
reset before they can take effect. The same 120 s path also force-rotates device
credentials, which is the treadmill beta38 blocked one layer lower.

Conclusion: the issue is **not fixed** by beta35–beta38 alone.

## Findings

### F1 (critical) — all storm-guard state wiped every 120 s

- `coordinator.py:2242-2255` — when `_empty_reads >= _STREAM_STALL_RESET_POLLS`
  (`STREAM_STALL_FULL_RESET_SECONDS=120` / `REALTIME_SCAN_INTERVAL=5` → 24 polls),
  the coordinator calls `_reset_client()` and `_close_session()`.
- `shared_client.py:55-58` — `SharedEmaldoClient.reset()` sets `client = None`; the
  next `ensure_client()` constructs a **new `EmaldoClient`**. That discards:
  - `_home_refresh_last_attempt` / `_home_refresh_suppress_logged`
    (`client.py:181-188`) — the entire beta35 home-TTL rotation latch;
  - `_e2e_creds_cache` and its `generation` counters (`client.py:1085-1091`).
- The rebuilt `PersistentE2ESession` starts from `e2e.py:3742-3828` defaults:
  `_stream_reconnect_streak=0`, `_stream_reconnect_backoff_anchor_frames=0`,
  `_stream_last_reconnect_reason=None`, `_stream_needs_creds_refresh=False`,
  `_stream_ever_decrypted=False`. That discards:
  - beta37 escalating backoff (`e2e.py:4856-4879`) — restarts at the 2 s base every
    two minutes, so the 30 s ceiling (`_stream_reconnect_backoff_max`) never holds;
  - beta38 `is_plain_21204` latch (`e2e.py:4904`), which reads
    `_stream_last_reconnect_reason`.

Result: rebuild cadence during a storm stays at ~1–2 min — matching the Sep 17
telemetry (29 wedges / 27 recoveries, 1394 reconnects in ~3 h).

### F2 (critical) — coordinator-level credential treadmill (beta38 gap)

- `coordinator.py:2253` sets `_needs_fresh_creds = True` on every stall reset.
- `coordinator.py:1089-1097` then calls
  `get_e2e_credentials(..., force_refresh=True)`.
- `client.py:1027,1071` — a forced refresh always calls `e2e_login`, and per the
  method's own docstring (`client.py:992-993`) `e2e_login` **rotates the device
  `chat_secret` server-side, expiring any other live UDP session and triggering a
  21204 storm**.

beta38 only suppressed pre-rotation inside `_stream_reconnect_locked`
(`e2e.py:4894-4906`). The coordinator path is ungated and fires every 120 s.

Same flag is also set on override failures — `coordinator.py:1272, 1287, 1301, 1333`
— and on `poll_stall_reset` (`coordinator.py:2412`).

### F3 (high) — multi-entry amplification (regression surface from beta36)

beta36 (`9e2dd4f`, "one config entry per cabinet") means N independent
`RealtimeCoordinator` instances, each with its own `_empty_reads` timer, each able to
trigger F2. Device A's rotation expires Device B's live session → B sees empty reads →
B resets and rotates → reciprocal 21204. The shared **home** secret ping-pong was
fixed (`coordinator.py:1099-1121`, primary publishes / secondary consumes); the
per-device **`chat_secret`** ping-pong has no equivalent guard.

### F4 (medium) — in-process recovery escalations become unreachable after a reset

- `client.py:1053-1058` — home-secret escalation requires `entry.generation >= 3`
  within 60 s, but a fresh client always starts at `generation = 1`
  (`client.py:1084`). With the 120 s client reset active, generation rarely reaches 3,
  so if the home secret genuinely is stale there is no in-process path back; only an
  HA restart re-synchronises everything. This matches the "restart always fixes it"
  observation.
- `e2e.py:4974` — the #53 decrypt gate arms only when `_stream_ever_decrypted` is
  True; `e2e.py:4372` gates the `long_stall` credential refresh the same way. A
  session rebuilt mid-storm never decrypts a frame, so both escalations stay disabled
  for its whole (≤120 s) life.

### Verified correct — not findings

- `_stream_frames_received` increments only on a genuinely decrypted power-flow frame
  (`e2e.py:4663-4667`), so the beta37 streak reset (`e2e.py:4859-4866`) cannot be
  spoofed by keepalive/relay chatter.
- Subscribe spacing is consistent: `e2e.py:4514-4520` hard gap plus
  `e2e.py:4926-4937` post-rebuild timestamp preservation. No self-inflicted <10 s
  resubscribe remains.
- `_creds_provider` (`coordinator.py:1131-1140`) re-resolves the client on each call —
  no stale client pinning (RC2 stays fixed).

## Fix plan

Ordering matters: P0 stops the self-sustaining loop, P1 removes the rotation trigger,
P2 restores in-process recovery. Ship P0+P1 together; they are coupled.

### P0 — make storm state survive client/session rebuilds

Move the storm bookkeeping out of the objects that get thrown away.

1. Introduce a per-home **storm state holder** that lives in `hass.data[DOMAIN]`
   (same lifetime as the config entries, i.e. reset only by an HA restart).
   Suggested fields: `reconnect_streak`, `last_rotation_monotonic`,
   `consecutive_resets_without_frame`, `episode_started_monotonic`.
2. `SharedEmaldoClient` (`shared_client.py`): stop dropping the whole client on
   `reset()`. Split into
   - `reset_auth()` — re-login only, keeping `_e2e_creds_cache`,
     `_home_refresh_last_attempt`, `_home_refresh_suppress_logged`; and
   - `reset_hard()` — today's behaviour, reserved for auth failures
     (`coordinator.py:270, 283`).
   Point `coordinator.py:2252` and `coordinator.py:2411` at `reset_auth()`.
3. `PersistentE2ESession.start_stream()`: accept the storm state holder and seed
   `_stream_reconnect_streak` / `_stream_reconnect_backoff_anchor_frames` /
   `_stream_ever_decrypted` from it; write them back on close. A rebuilt session then
   inherits the escalated backoff instead of restarting at 2 s.

Acceptance: during a synthetic storm the effective rebuild interval reaches the 30 s
ceiling and stays there across at least two coordinator stall resets.

### P1 — stop the 120 s forced credential rotation

4. `coordinator.py:2253` — do **not** set `_needs_fresh_creds = True`
   unconditionally. Apply the beta38 rule at this layer too: force fresh credentials
   only when the evidence is rekey-shaped, i.e.
   `stream_diag["last_reconnect_reason"]` contains `decrypt_gate_no_frame` /
   `force_logout`, or `drain_unparsed >= max(1, drain_packets - 1)` (packets arriving
   but nothing decrypts). A plain `session_expired_21204` or a frameless stall gets a
   plain rebuild with the cached credential generation.
5. Add a home-scoped minimum interval between device `e2e_login` rotations (reuse the
   P0 holder's `last_rotation_monotonic`), enforced in
   `client._get_e2e_credentials` before the `e2e_login` at `client.py:1071`, not just
   for the home-level escalation. This is the cross-cabinet guard missing for F3.
6. Back off the reset cadence itself while an episode is active: after N consecutive
   `stream_stall_reset` without a single decrypted frame, grow the reset interval
   (120 s → 5 min → 15 min cap) instead of firing every 120 s, and surface the state
   on the diagnostic sensor (`backend refusing E2E handshake — retrying in X`), which
   was suggested-fix #1 in the Sep 17 issue and is still unimplemented.

Acceptance: during a storm, `e2e_login` calls per hour drop to single digits; REST and
Battery Optimizer remain unaffected (they already are); the diagnostic sensor shows a
growing retry interval rather than silent churn.

### P2 — restore reachable in-process recovery

7. Carry `_stream_ever_decrypted` in the P0 holder so a rebuilt session keeps the
   decrypt gate (`e2e.py:4974`) and the `long_stall` refresh (`e2e.py:4372`) armed
   when the *home* has decrypted frames before. Cold start stays ungated.
8. Base the `client.py:1053-1058` home escalation on the persistent holder's rotation
   history instead of the per-client `entry.generation`, so the escalation is still
   reachable after a client rebuild — while remaining latched by the beta35 home-TTL
   guard.

Acceptance: a forced stale-home-secret scenario recovers in-process (no HA restart).

### Tests

- Extend `tests/test_stream_reconnect_backoff.py`: assert the streak survives a
  session close/recreate cycle via the holder.
- New test: `stream_stall_reset` with reason `session_expired_21204` performs **no**
  forced `e2e_login`; with reason `decrypt_gate_no_frame` it does.
- New test: two coordinators on one home cannot rotate `chat_secret` within the
  minimum interval (F3 regression guard).
- New test: reset-cadence backoff grows 120 s → 5 min → 15 min while frameless, and
  resets on the first decrypted frame.

### Validation in the field

Unchanged from the Sep 17 issue: observe the 01:00–02:00 UTC window. Success criteria
now measurable from the diagnostic sensor alone — during a backend window, expect
`reconnects` in the tens (not ~1400), `e2e_login` rotations in single digits, and a
visible growing retry interval.
