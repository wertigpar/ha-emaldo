# 21204 storm recurs nightly in same window; in-process rebuild cannot break it, restart always does

**Status: DRAFT — not posted to GitHub. User posts.**

## Summary

E2E realtime stream enters a 21204 (session expired) reconnect storm lasting 1–3.5 hours,
recurring almost nightly in the same backend window. Every full in-process recovery
(27×: fresh client, fresh credentials, fresh caches, session close + rebuild) is rejected
with 21204 again while the window persists. A HA restart clears it instantly. REST and
Battery Optimizer stay healthy throughout — only the E2E relay stream loops.

## Evidence (live HA telemetry, 2026-09-10 → 2026-09-17)

7-day `sensor.power_store_realtime_connection` history — stale episodes:

| When (local +03:00) | Duration | Notes |
|---|---|---|
| Sep 10 23:05 | ~1h54m | overnight |
| Sep 11 03:53 | 1 min | self-healed |
| Sep 11 23:35 | ~3h36m | ended 03:11, no restart |
| Sep 12 03:15 / 03:46 / 04:58 | 2 min each | self-healed |
| Sep 12 11:02 | ~8 min | daytime storm |
| Sep 13 15:40 | ~27 min | afternoon storm |
| Sep 15 16:00 | 5 min | self-healed |
| Sep 15 20:59 | ~1h05m | ended 22:04, no restart |
| Sep 16 03:44 | 3 min | self-healed |
| **Sep 17 04:03** | **~3h07m** | **full incident analyzed below** |

Short (1–5 min) stales self-heal constantly — normal backend hiccups. The multi-hour
storm is the pathological tail and recurs in the same nightly window.

## Sep 17 incident (analyzed in depth)

Timeline (Europe/Helsinki, +03:00):

- 04:03:28 — first stale. Loop starts.
- 04:03–07:11 — connected/stale oscillation at 1–2 min cadence. Each "connected" is a
  fresh rebuild session; each "stale" is the next 21204 cycle. 29 wedges, 27 recoveries.
- Cumulative: 1394 reconnects, 21204 × 1397, 6371 resubscribes, 1358 empty reads.
- 07:11:51 — last "connected" recorded.
- 07:13:15 — HA restart (deploy of subscribe-spacing fix, e2e.py:4828-4839).
- Post-restart: clean. `reconnects=1` (a single 21204 that self-recovered), 0 keepalive
  failures, 100% success rate.

Ambiguity: last connected is only 84s before restart. Prior stale→connected cycles
survived 10s–2min before the next stale, so 84s is indistinguishable from the storm still
cycling. Restart is a decisive break point, but self-termination within ≤84s cannot be
ruled out (Sep 11 and Sep 15 storms ended without any restart — the storm dies when the
backend window closes).

## During the storm, everything else was fine

- REST: `sensor.power_store_battery_soc` ticked 84→83→…→87 continuously; every poll
  `success:True`.
- Battery Optimizer: `sensor.battery_optimizer_schedule_chart` re-planned every ~15 min,
  01:31→07:13, no gap.
- Only the E2E realtime relay stream (21204) was in the loop.

## Root-cause analysis

First stale is backend-initiated: onset 04:03 local = 01:03 UTC, matching the documented
"api.emaldo.com is flaky ~01:00–02:00 UTC" window (coordinator.py:2235-2241) and the
user's nightly recurrence observation.

The storm then self-sustains locally because the in-process recovery loop cannot land a
clean handshake while the degraded window persists:

1. Stall escalation (coordinator.py:2242-2256) → `_reset_client()` → new `EmaldoClient`
   with fresh `_e2e_creds_cache` + `_home_e2e_cache` + fresh login. Verified correct.
2. `_creds_provider` (coordinator.py:1080-1097) fetches credentials fresh via
   `_ensure_client()` each call — verified RC2-correct, no stale client pinning.
3. `_close_session` (coordinator.py:2966-2982) closes via executor, pops device session,
   invalidates session ref — verified proper teardown.
4. Escalation path: ≥3 credential refreshes in 60s → `force_home_refresh=True` →
   server rotates `home_end_secret` (client.py:1005-1100). That rotation invalidates the
   secret the very next rebuild uses — the client rotates home_secret, then builds a
   session with a secret the rotation already orphaned. Read-modify-write race across a
   flaky network. Loop self-sustains until one refresh lands during a clean backend beat.
5. 27 fresh-rebuild attempts, all 21204 → rejection source is server-side relay/device
   session state during the degraded window, not persistent local sticky credentials.

Restart clears it instantly because it collapses the whole chain into one atomic rebuild
(clears shared `_home_secrets` dict, all in-process session state, all caches at once).

## Why official Emaldo apps are unaffected

They use a different (non-E2E-relay) API path. The server is healthy end-to-end except
the relay handshake during the degraded window.

## Suggested fix directions (not yet implemented)

1. **Retry shield / backoff:** after N consecutive forced-refresh rebuilds still hitting
   21204, stop cycling and surface an actionable state ("backend refusing E2E handshake —
   retrying in X"), instead of silent 1–2 min churn for hours.
2. **Don't rotate home_secret mid-storm:** when consecutive copies of the same secret keep
   failing, hold `force_home_refresh` — rotation is self-defeating while the backend
   window is open.
3. **Backend window awareness:** if the degraded window (~01:00–02:00 UTC) is known,
   suppress rebuild storms during it and retry once on a clean beat.

## Validation

- Recurrence base rate (7-day history): captured above.
- Nightly test: fix live, observe 04:00–05:00 local window. Clean → fix confirmed.
  Recurred → insufficient, revisit.
- Confound to date: design fix deploy + restart + backend window close coincide —
  cannot attribute post-restart health to the subscribe-spacing fix alone.

## 21204 storm historical background (prior incidents)

- #47: home-secret cache grace window blocked rotation → 90s death cycle (fixed, RC5).
- #41: stream reconnect silently succeeded after 21204 → never recovered (fixed, RC4/RC1).
- RC1/RC2: dual-unit home-secret ping-pong + stale `_creds_provider` client (fixed).
- This issue = recurring nightly window + in-process rebuild cannot break mid-window,
  restart always does.