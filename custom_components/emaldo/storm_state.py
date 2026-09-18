"""Per-home storm-state holder shared across coordinator rebuilds.

The 21204 reconnect storm (Sep 2026) survives every client/session rebuild:
``_stream_reconnect_streak``, ``_stream_ever_decrypted`` and the
credential-rotation latches lived on the client or the session, so each
``_reset_client()`` / session teardown forgot the storm was in progress and
the churn restarted at base cadence.

This holder is a pure-Python, thread-safe state bag created once per home
and handed to every rebuilt session (Phase 1) and to the credential path
(Phase 2/3), so the guards keep their memory — without importing
homeassistant (``emaldo_lib`` must stay HA-free).

Duck-typed protocol consumed by ``e2e.PersistentE2ESession`` and
``emaldo_lib.client.EmaldoClient``:
- ``note_frame()``              — a fresh decrypted frame arrived (clears the
  streak and the frameless-reset counter so the cadence drops back to base)
- ``note_frameless_reset()``    — a full stall reset fired between frames
- ``reset_interval() -> float`` — current stall-reset cadence (s), derived
  from ``resets_without_frame`` via the 120 → 300 → 900 ladder
- ``may_rotate(min_interval, now) -> bool`` — device credential rotation
  allowed? (``min_interval`` injected by the caller so the guard survives
  client rebuilds without importing this module in ``emaldo_lib``)
- ``note_rotation(now)``        — record a device-level rotation
- ``note_forced_refresh(now)``  — record a forced creds refresh (3.2)
- ``forced_refresh_count(now)`` — forced refreshes in the last 60 s
- ``reconnect_streak`` / ``ever_decrypted`` — seeded and written back by the
  session; ``note_frame()`` clears the streak
"""

from __future__ import annotations

import threading
import time

# Plan 2.4: full-stall reset cadence ladder (s). index = resets_without_frame,
# advanced by note_frameless_reset(), restored to base by the first decrypted
# frame via note_frame().
STALL_RESET_LADDER: tuple[float, ...] = (120.0, 300.0, 900.0)

# Plan 3.2: durable-history window for home-secret escalation.
FORCED_REFRESH_HISTORY_WINDOW = 60.0


def stall_reset_needs_fresh_creds(diag: dict | None) -> bool:
    """Decide whether a stall reset should force fresh credentials.

    Plan 2.1 evidence gate. Rekey-shaped stalls — where the relay rejects
    our (old) secrets — must trigger a credential rotation; plain 21204
    windows and silent frame gaps must NOT (rotating there feeds the
    rotation storm). Port of the beta38 drain rule (e2e.py) up one layer.

    Rotate when:
    - the reconnect reason is ``decrypt_gate_no_frame`` or ``force_logout``
      (definitive rekey signals), or
    - every drained packet failed to decrypt/parse
      (``drain_packets > 0`` and ``drain_unparsed >= max(1, pkts - 1)``).

    Missing/empty diagnostics are non-evidence -> no rotation.
    """
    sd = diag or {}
    reason = str(sd.get("last_reconnect_reason") or "")
    if "decrypt_gate_no_frame" in reason or "force_logout" in reason:
        return True
    pkts = int(sd.get("drain_packets") or 0)
    unparsed = int(sd.get("drain_unparsed") or 0)
    return pkts > 0 and unparsed >= max(1, pkts - 1)


class HomeStormState:
    """Thread-safe per-home storm memory surviving client/session rebuilds."""

    def __init__(self, home_id: str) -> None:
        self.home_id = home_id
        self._lock = threading.RLock()
        self.reconnect_streak = 0  # feeds e2e backoff escalation
        self.ever_decrypted = False  # feeds decrypt gate / long_stall refresh
        self.last_rotation_monotonic = 0.0  # last device e2e_login rotation
        self.last_home_rotation_monotonic = 0.0
        self.resets_without_frame = 0  # consecutive stream_stall_resets
        self.episode_started_monotonic: float | None = None
        self._forced_refresh_times: list[float] = []

    # ------------------------------------------------------------------
    # Frame / cadence state
    # ------------------------------------------------------------------

    def note_frame(self) -> None:
        """Record a fresh decrypted frame: clear the streak and the
        frameless-reset counter, restoring the base cadence."""
        with self._lock:
            self.reconnect_streak = 0
            self.resets_without_frame = 0
            self.episode_started_monotonic = None

    def note_frameless_reset(self) -> None:
        """Record a full stall reset that fired between frames; advance the
        cadence one ladder step (capped)."""
        with self._lock:
            if self.resets_without_frame == 0 and self.episode_started_monotonic is None:
                self.episode_started_monotonic = time.monotonic()
            self.resets_without_frame += 1

    def reset_interval(self) -> float:
        """Current stall-reset cadence in seconds (120 → 300 → 900, capped)."""
        with self._lock:
            idx = min(self.resets_without_frame, len(STALL_RESET_LADDER) - 1)
            return STALL_RESET_LADDER[idx]

    # ------------------------------------------------------------------
    # Credential rotation guard (2.2 / F3)
    # ------------------------------------------------------------------

    def may_rotate(
        self, min_interval: float, now: float | None = None
    ) -> bool:
        """True when the device credential may be rotated right now.

        ``min_interval`` is injected by the caller (keeps ``emaldo_lib`` free
        of this module). ``now`` is injectable for tests; defaults to
        ``time.monotonic()``.
        """
        with self._lock:
            now = time.monotonic() if now is None else now
            return now - self.last_rotation_monotonic >= min_interval

    def note_rotation(self, now: float | None = None) -> None:
        """Record a device-level credential rotation."""
        with self._lock:
            self.last_rotation_monotonic = (
                time.monotonic() if now is None else now
            )

    def note_home_rotation(self, now: float | None = None) -> None:
        """Record a home-secret rotation (3.2 escalation)."""
        with self._lock:
            self.last_home_rotation_monotonic = (
                time.monotonic() if now is None else now
            )

    # ------------------------------------------------------------------
    # Forced-refresh history (3.2)
    # ------------------------------------------------------------------

    def note_forced_refresh(self, now: float | None = None) -> None:
        """Record a forced credential refresh (device-level)."""
        with self._lock:
            self._forced_refresh_times.append(
                time.monotonic() if now is None else now
            )

    def forced_refresh_count(
        self, now: float | None = None, window: float = FORCED_REFRESH_HISTORY_WINDOW
    ) -> int:
        """Number of forced refreshes recorded within ``window`` seconds."""
        with self._lock:
            now = time.monotonic() if now is None else now
            cutoff = now - window
            kept = [t for t in self._forced_refresh_times if t > cutoff]
            self._forced_refresh_times = kept
            return len(kept)