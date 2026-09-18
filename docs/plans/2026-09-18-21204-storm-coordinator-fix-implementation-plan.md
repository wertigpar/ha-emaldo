# Implementation plan — 21204 storm: coordinator-layer fix (beta39)

Companion to the analysis in
`docs/issues/2026-09-18-21204-storm-coordinator-layer-analysis.md` (findings F1–F4).
This document is the executable plan: exact files, symbols, signatures, ordering,
tests and rollback.

Base commit: `8bb5a0c` (beta37/38 live).
Target manifest version: `1.0.0-beta39`.

## Design in one paragraph

Every storm counter today lives on an object the coordinator throws away every 120 s
(`EmaldoClient` via `SharedEmaldoClient.reset()`, `PersistentE2ESession` via
`_close_session()`). Introduce one **per-home storm-state object owned by
`hass.data[DOMAIN]`**, which only an HA restart can clear — the same lifetime as the
recovery that is empirically known to work. Client and session read their storm
counters from it and write back to it. Then make the coordinator's 120 s escalation
(a) evidence-gated before it rotates credentials, and (b) itself subject to backoff.

## Phase 0 — scaffolding (no behaviour change)

### 0.1 New module `custom_components/emaldo/storm_state.py`

```python
@dataclass
class HomeStormState:
    """Per-home storm bookkeeping that survives client/session rebuilds.

    Lifetime = hass.data[DOMAIN]; cleared only by an HA restart or entry unload.
    """
    home_id: str
    reconnect_streak: int = 0            # feeds e2e backoff escalation
    ever_decrypted: bool = False         # feeds decrypt gate / long_stall refresh
    last_rotation_monotonic: float = 0.0 # last device e2e_login rotation
    last_home_rotation_monotonic: float = 0.0
    resets_without_frame: int = 0        # consecutive stream_stall_resets
    episode_started_monotonic: float | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def note_frame(self) -> None: ...        # clears streak + resets_without_frame
    def note_frameless_reset(self) -> None: ...
    def reset_interval(self) -> float: ...   # 120 -> 300 -> 900 cap
    def may_rotate(self, min_interval: float) -> bool: ...
    def note_rotation(self) -> None: ...

def async_get_storm_state(hass, home_id) -> HomeStormState:
    return hass.data.setdefault(DOMAIN, {}).setdefault(
        "_storm_state", {}
    ).setdefault(home_id, HomeStormState(home_id))
```

Placement note: `hass.data[DOMAIN]` already holds sibling shared dicts using this exact
pattern — `_home_secrets` (`coordinator.py:1103-1105`) and `_home_primaries`
(`__init__.py:259`). Follow them, and clear `_storm_state[home_id]` in the unload path
next to the existing `hass.data[DOMAIN].pop(entry.entry_id)` (`__init__.py:369`) when
the last entry for that home goes away.

### 0.2 Tests for the holder alone

`tests/test_storm_state.py` — pure unit tests, no HA imports:
`reset_interval()` ladder, `may_rotate()` gating, `note_frame()` clearing.

Phase 0 ships dead code. Safe to merge on its own.

## Phase 1 (P0) — make storm state survive rebuilds (fixes F1)

### 1.1 Split the shared-client reset — `shared_client.py:55-58`

```python
def reset_auth(self) -> None:
    """Drop only the REST session/token; keep E2E caches and storm guards."""
    with self._lock:
        if self.client is not None:
            self.client.invalidate_auth()   # new, see 1.2

def reset(self) -> None:   # unchanged hard reset, now used only for auth errors
    with self._lock:
        self.client = None
```

Callers to repoint:

| Site | Today | After |
|---|---|---|
| `coordinator.py:270`, `:283` (`EmaldoAuthError`) | `_reset_client()` | unchanged (`reset()` hard) |
| `coordinator.py:590`, `:632` | audit per call site | likely `reset_auth()` |
| `coordinator.py:2252` (`stream_stall_reset`) | `_reset_client()` | `_reset_client_auth()` |
| `coordinator.py:2411` (`poll_stall_reset`) | `_reset_client()` | `_reset_client_auth()` |
| `coordinator.py:1138` (`_creds_provider` auth retry) | `_reset_client()` | unchanged (hard) |
| `coordinator.py:1717, 1764, 1814, 1857, 1926, 1932` | audit | hard reset only on auth failures |

Add `EmaldoCoordinator._reset_client_auth()` beside `_reset_client()`
(`coordinator.py:152-154`).

### 1.2 `EmaldoClient.invalidate_auth()` — `emaldo_lib/client.py`

Drops the REST token/session so the next call re-logs-in, while **keeping**
`_e2e_creds_cache`, `_home_e2e_cache`, `_home_refresh_last_attempt`,
`_home_refresh_suppress_logged` (`client.py:162-188`). This is what preserves the
beta35 latch across a stall reset.

### 1.3 Seed/persist session storm counters — `emaldo_lib/e2e.py`

- `start_stream()` (`e2e.py:4199-4210`): add keyword-only
  `storm_state: object | None = None`. Keep it duck-typed (`emaldo_lib` must not
  import HA). Store as `self._storm_state`.
- After `self._stream_stop.clear()` (`e2e.py:4244`), seed when provided:
  `_stream_reconnect_streak`, `_stream_reconnect_backoff_anchor_frames` (seed to the
  live `_stream_frames_received`), `_stream_ever_decrypted`.
- Write-back points:
  - `e2e.py:4863` (streak reset on fresh frame) → `storm_state.note_frame()`;
  - `e2e.py:4871` (streak increment) → mirror into `storm_state.reconnect_streak`;
  - `e2e.py:4676` (`_stream_ever_decrypted = True`) → `storm_state.ever_decrypted = True`;
  - `close()` (`e2e.py:5700`) → final flush.
- Caller: `coordinator.py:1192-1199` passes
  `storm_state=async_get_storm_state(self.hass, home_id)`.

Note: the anchor seed is a no-op on a freshly rebuilt session (`_stream_frames_received`
starts at 0, so seed = 0 and the first frame clears it regardless). Keep it anyway —
defensive and correct when a session is re-seeded without a rebuild.

**Acceptance (1):** in a synthetic storm the effective rebuild interval reaches the
30 s ceiling and *stays* there across ≥2 coordinator stall resets.

## Phase 2 (P1) — stop the 120 s credential rotation (fixes F2, F3)

### 2.1 Evidence-gate `_needs_fresh_creds` — `coordinator.py:2253`

Port the beta38 rule (`e2e.py:4894-4906`) up one layer.

**The evidence must be read live, not from `self._stream_diag`.** `_stream_diag` is
refreshed once per 5 s poll at `coordinator.py:1402`, and several read paths return
before reaching it (read-error path ~`coordinator.py:2200-2207`), so at the decision
point (`coordinator.py:2242`) it can be a whole poll stale — and the stream thread can
change `_stream_last_reconnect_reason` in between. Deciding rotate/no-rotate from a
stale reason immediately before wiping the session is exactly the misjudgement this
phase is meant to remove. Re-read inline:

```python
# module level — pure, unit-testable without constructing a coordinator
def stall_reset_needs_fresh_creds(diag: dict | None) -> bool:
    """Only a rekey-shaped stall justifies rotating the device chat_secret.

    A plain 21204 or a frameless stall proves the *session* died, not that the
    credential generation is stale — rotating there orphans the generation the
    next session binds to and re-arms the next 21204 (beta38 treadmill, one
    layer up).
    """
    sd = diag or {}
    reason = sd.get("last_reconnect_reason") or ""
    if "decrypt_gate_no_frame" in reason or "force_logout" in reason:
        return True
    pkts = int(sd.get("drain_packets", 0) or 0)
    unparsed = int(sd.get("drain_unparsed", 0) or 0)
    return pkts > 0 and unparsed >= max(1, pkts - 1)   # packets arrive, none decrypt
```

Call site, at `coordinator.py:2242` before the teardown:

```python
_live_diag = self._stream_diag
if self._session is not None and not self._session.closed:
    _live_diag = await self.hass.async_add_executor_job(
        self._session.stream_diagnostics
    )
    self._stream_diag = _live_diag          # keep the logged snapshot in sync
self._needs_fresh_creds = stall_reset_needs_fresh_creds(_live_diag)
```

`stream_diagnostics()` takes the session lock (`e2e.py:4380`), which the stream thread
can hold across a socket read — it **must** go through the executor, never inline on
the event loop. Fall back to the cached `_stream_diag` when the session is already
gone.

The function is deliberately module-level and pure: `RealtimeCoordinator.__init__` is
too heavy to instantiate in a unit test, and the 2.1/2.4 test rows below need to call
the gate directly.

Apply the same gate at `coordinator.py:2412` (`poll_stall_reset`).
Leave `coordinator.py:1272, 1287, 1301, 1333` (override failures) alone — those are
genuine per-command session faults, not storm-rate paths.

### 2.2 Home-scoped device-rotation interval — `emaldo_lib/client.py:1027-1074`

Currently only the **home-level** escalation is latched (beta35). Add the same latch to
the **device-level** rotation, because `e2e_login` rotates the device `chat_secret`
server-side and expires every other live UDP session on the home
(`client.py:992-993`) — the F3 cross-cabinet ping-pong.

```python
MIN_DEVICE_ROTATION_INTERVAL = 90.0  # seconds, per home

# inside _get_e2e_credentials, before the e2e_login at client.py:1071
if force_refresh and entry is not None and not self._may_rotate_device(home_id):
    _LOGGER.warning(
        "Device credential rotation held (21204 storm guard) — reusing "
        "generation %d for home %s", entry.generation, home_id,
    )
    entry.last_used_at = now
    return dict(entry.creds)      # serve the cached generation, do NOT rotate
```

Back `_may_rotate_device` / `_note_device_rotation` with a per-home monotonic map on
the client **and**, when the storm-state holder is injected (see 2.3), with
`storm_state.may_rotate()` so the interval survives a hard client reset too.

Only skip when a usable cached entry exists; a TTL-expired or missing entry must still
log in, otherwise a genuinely cold client can never start.

Accepted trade-off: a cached entry whose TTL expired during the hold is still served.
If the backend rotated inside that window, the next handshake surfaces a plain 21204 —
which 2.1 / beta38 now tolerate without rotating, and the hold expires within 90 s.

### 2.3 Inject storm state into the client

`SharedEmaldoClient.ensure_client()` (`shared_client.py:40-53`) gains an optional
`storm_state_provider` so `EmaldoClient` can consult the holder without importing HA.
Alternative (simpler, preferred if it reviews cleanly): keep `EmaldoClient` pure and
enforce the rotation interval in `coordinator._creds_provider`
(`coordinator.py:1131-1140`) + `_ensure_session` (`coordinator.py:1089-1090`) by
downgrading `force_refresh` to `False` when `storm_state.may_rotate()` is False.
Decide at implementation time; prefer the version that keeps `emaldo_lib` HA-free.

### 2.4 Back off the reset cadence itself — `coordinator.py:2242`

Replace the fixed `_STREAM_STALL_RESET_POLLS` threshold with a dynamic one derived
from `storm_state.reset_interval()`:

- ladder `120 s → 300 s → 900 s` (cap), advanced by `note_frameless_reset()`;
- reset to 120 s on the first decrypted frame (`note_frame()`).

Surface it: add `stall_reset_interval_s` and `storm_active` to the realtime diagnostic
attributes. The dict that reaches the sensor is the snapshot at `coordinator.py:1617`
(`"stream_diag": ...`), assembled into entity attributes in `sensor.py`;
`coordinator.py:1403-1412` is only the `_e2e_diag` log string — update both.
Change the repeated stall warning to the actionable form the Sep 17 issue asked for:
`backend refusing E2E handshake — retrying in Xs`.

**Acceptance (2):** during a storm, `e2e_login` calls/hour drop to single digits,
`reconnects` to tens (not ~1400), and the diagnostic sensor shows a growing retry
interval instead of silent 2-minute churn.

## Phase 3 (P2) — restore reachable in-process recovery (fixes F4)

### 3.1 Carry `ever_decrypted` across rebuilds

Already seeded in 1.3. Effect: a session rebuilt mid-storm on a home that *has*
decrypted frames before keeps the #53 decrypt gate armed (`e2e.py:4974`) and the
`long_stall` credential refresh enabled (`e2e.py:4372`). Genuine cold start
(`ever_decrypted=False`) stays ungated, so an idle relay is still never punished.

Depends on Phase 1 only. Independently shippable.

### 3.2 Base the home escalation on durable history — `client.py:1053-1058`

`entry.generation >= 3` is unreachable after a client rebuild (fresh cache always
starts at `generation = 1`, `client.py:1084`). Replace the generation test with a
count of forced refreshes recorded in the storm-state holder over the last 60 s, while
keeping the beta35 `_home_e2e_ttl` latch (`client.py:1049-1070`) exactly as is.

**Depends on 2.3.** The holder must already be reachable from inside
`_get_e2e_credentials`; without 2.3's injection this is dead code. Ship 3.2 with
Phase 2, not with 3.1.

**Acceptance (3):** an injected stale-home-secret scenario recovers in-process, with no
HA restart.

## Test matrix

Extend `tests/test_stream_reconnect_backoff.py` (existing harness already loads
`e2e.py` standalone with a `Crypto` stub — reuse `_load_e2e`, `_Clock`,
`_drive_one_cycle`):

| Test | Asserts | Guards |
|---|---|---|
| `test_backoff_survives_session_recreate` | streak seeded from holder; new session's first backoff is the *escalated* value, not 2.0 | F1 |
| `test_ever_decrypted_survives_session_recreate` | decrypt gate armed on a rebuilt session for a previously-healthy home | F4 |
| `test_stall_reset_plain_21204_does_not_rotate` | pure `stall_reset_needs_fresh_creds(diag)` False for `session_expired_21204` and for frameless stalls | F2 |
| `test_stall_reset_decrypt_gate_rotates` | True for `decrypt_gate_no_frame`, `force_logout`, and all-undecryptable drains | F2 regression guard |
| `test_device_rotation_interval_enforced` | second `get_e2e_credentials(force_refresh=True)` inside 90 s returns the cached generation, no `e2e_login` | F3 |
| `test_two_coordinators_cannot_ping_pong` | two consumers on one home cannot rotate within the interval | F3 |
| `test_reset_interval_ladder` | 120 → 300 → 900 while frameless; back to 120 on first frame | P1 |

The two gate tests import `stall_reset_needs_fresh_creds` as a module-level function
and feed it plain dicts — no coordinator instance, no HA test harness. The ladder tests
live in `tests/test_storm_state.py` against the holder directly.

Keep the existing four tests passing unchanged — they encode the beta37/38 contracts.

Run: `python -m pytest tests -q`.

## Merge order and risk

| Phase | Risk | Independently shippable |
|---|---|---|
| 0 scaffolding + holder tests | none (dead code) | yes |
| 1 state survival | low — pure state plumbing, no new network behaviour | yes |
| 2 rotation gating + reset backoff (2.1–2.4) | **medium** — a wrongly-held rotation could delay a legitimate rekey by up to 90 s; mitigated by 2.1's decrypt-gate exception, which is the path that genuinely needs a rekey | ship with 1 |
| 3.1 `ever_decrypted` carry-over | low | yes (needs Phase 1) |
| 3.2 home escalation on durable history | low | **no — requires 2.3's holder injection**; ship with Phase 2 |

Do **not** ship Phase 2 without Phase 1: gating rotation while the backoff still resets
every 120 s would slow recovery without slowing the churn.

## Rollback

Each phase is a self-contained commit.
- Phase 2 regression (slow legitimate rekey) → revert 2.2/2.3, keep 2.1 and 2.4.
  **This also removes 3.2's holder injection, so revert 3.2 in the same step** — left
  in place it would reference an uninjected holder. 3.1 is unaffected.
- Phase 1 regression → revert the `storm_state=` argument at `coordinator.py:1192-1199`;
  `start_stream()` defaults to `None` and behaviour returns exactly to beta38. This also
  neutralises 3.1.

## Field validation

Unchanged window: 01:00–02:00 UTC (= 04:00–05:00 local, `coordinator.py:2235-2241`).
Read the realtime diagnostic sensor the morning after:

- `reconnects` delta over the episode: **tens**, not ~1400;
- `reconnect_reasons` dominated by `session_expired_21204` with no matching count of
  forced credential refreshes;
- `stall_reset_interval_s` observed climbing 120 → 300 → 900;
- recovery without an HA restart (as already seen on Sep 11 and Sep 15, which ended on
  their own when the backend window closed).

If the storm still runs at a ~2 min cadence after this lands, the remaining cause is
server-side within the window and the correct next step is the user-facing "backend
refusing E2E handshake" state from 2.4 rather than further client-side rebuilding.
