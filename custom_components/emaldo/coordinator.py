"""DataUpdateCoordinators for Emaldo.

Two coordinators:
* :class:`EmaldoCoordinator` — slow REST + battery details (60s interval)
* :class:`EmaldoRealtimeCoordinator` — fast E2E power flow via a persistent
  UDP session (10s interval).
"""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
import struct
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .emaldo_lib import (
    EmaldoClient,
    EmaldoAuthError,
    EmaldoConnectionError,
    EmaldoE2ESessionExpired,
    PersistentE2ESession,
)
from .emaldo_lib.exceptions import EmaldoE2EError
from .emaldo_lib.e2e import (
    build_subscription_packet,
    decrypt_response,
    generate_nonce,
    _run_session,
    parse_ev_charging_info,
    set_ev_charging_mode_smart,
    set_ev_charging_mode_instant,
    EV_MODE_INSTANT_FULL,
    EV_MODE_INSTANT_FIXED,
    _EV_TYPE_GET_STATE,
    _SELLING_PROTECTION_SET_TYPE,
    _VIRTUALPOWERPLANT_SET_TYPE,
    _build_vpp_payload,
    read_battery_info as _standalone_read_battery_info,
    read_power_flow as _standalone_read_power_flow,
)

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_DEVICE_ID,
    CONF_DEVICE_MODEL,
    CONF_DEVICE_NAME,
    DEFAULT_SCAN_INTERVAL,
    REALTIME_SCAN_INTERVAL,
    KEEPALIVE_INTERVAL,
    REALTIME_STREAM_MODE,
    CONF_REALTIME_STREAM_MODE,
    REALTIME_SUCCESS_WINDOW,
    RESUBSCRIBE_INTERVAL,
    STREAM_STALE_AFTER,
    STREAM_FRAME_GAP_RESUBSCRIBE,
    STREAM_MIN_RESUBSCRIBE_GAP,
    STREAM_LONG_STALL_RECONNECT,
    STREAM_STALL_FULL_RESET_SECONDS,
    STREAM_FIRST_FRAME_WAIT,
)
from .realtime_sanity import (
    REALTIME_POWER_ABS_MAX_W,
    get_invalid_realtime_power_channels,
)
from .shared_client import SharedEmaldoClient

_LOGGER = logging.getLogger(__name__)


class EmaldoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Slow coordinator for REST battery/power data (60s)."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        shared_client: SharedEmaldoClient,
        *,
        device_id: str | None = None,
        device_model: str | None = None,
        device_name: str | None = None,
        persist_device_binding: bool = True,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._entry = entry
        self._shared_client = shared_client
        self._device_id: str | None = device_id or entry.data.get(CONF_DEVICE_ID)
        self._model: str | None = device_model or entry.data.get(CONF_DEVICE_MODEL)
        self._device_name: str | None = device_name or entry.data.get(CONF_DEVICE_NAME)
        self._persist_device_binding = persist_device_binding
        self._emergency_charge_active: bool = False
        self._emergency_charge_start_t: object = None  # datetime.time | None
        self._emergency_charge_end_t: object = None    # datetime.time | None
        self._ev_poll_counter: int = 0
        self._dual_power_fail_count: int = 0
        self._dual_power_fail_since: float | None = None
        self._dual_power_last_log: float = 0.0
        self._rest_fail_count: int = 0
        self._rest_fail_since: float | None = None
        self._rest_last_log: float = 0.0

    @property
    def home_id(self) -> str:
        """Return the configured home ID."""
        return self._entry.data[CONF_HOME_ID]

    @property
    def device_id(self) -> str | None:
        """Return the discovered device ID."""
        return self._device_id

    @property
    def device_model(self) -> str | None:
        """Return the discovered device model."""
        return self._model

    @property
    def device_name(self) -> str | None:
        """Return the discovered device name."""
        return self._device_name

    def _reset_client(self) -> None:
        """Invalidate the shared REST client so the next request re-authenticates."""
        self._shared_client.reset()

    def _ensure_client(self) -> EmaldoClient:
        """Create and authenticate the client if needed."""
        client = self._shared_client.ensure_client()

        if self._device_id is None:
            did, model, name = client.find_device(self.home_id)
            self._device_id = did
            self._model = model
            self._device_name = name
        elif self._model is None or self._device_name is None:
            devices = client.list_devices(self.home_id)
            selected = next(
                (d for d in devices if str(d.get("id", "")) == self._device_id),
                None,
            )
            if selected is not None:
                self._model = selected.get("model")
                self._device_name = selected.get("name", self._device_id)
            else:
                _LOGGER.warning(
                    "Configured device_id %s not found in home %s, falling back to first discovered device",
                    self._device_id,
                    self.home_id,
                )
                did, model, name = client.find_device(self.home_id)
                self._device_id = did
                self._model = model
                self._device_name = name

        return client

    def _on_rest_recovered(self) -> None:
        """Reset REST failure counters and log recovery after outages."""
        if self._rest_fail_count > 0:
            _LOGGER.info(
                "REST data fetch recovered after %d consecutive connection failure(s)",
                self._rest_fail_count,
            )
        self._rest_fail_count = 0
        self._rest_fail_since = None
        self._rest_last_log = 0.0

    def _fallback_to_last_data_on_rest_failure(
        self, err: EmaldoConnectionError
    ) -> dict[str, Any] | None:
        """Return last known data on transient REST failures when available."""
        if self.data is None:
            return None

        import time as _time
        now = _time.time()
        if self._rest_fail_count == 0:
            self._rest_fail_since = now
        self._rest_fail_count += 1

        fail_dur = now - (self._rest_fail_since or now)
        if self._rest_fail_count == 1:
            _LOGGER.warning(
                "REST fetch failed (%s); keeping previous data",
                err,
            )
            self._rest_last_log = now
        elif fail_dur >= 3600 and now - self._rest_last_log >= 3600:
            _LOGGER.error(
                "REST fetch has failed for >%.0fh; keeping previous data (latest: %s)",
                fail_dur / 3600,
                err,
            )
            self._rest_last_log = now
        elif now - self._rest_last_log >= 600:
            _LOGGER.warning(
                "REST fetch still failing (%d consecutive); keeping previous data",
                self._rest_fail_count,
            )
            self._rest_last_log = now

        return self.data

    async def _async_persist_device_binding(self) -> None:
        """Persist resolved device binding into config entry data."""
        if not self._persist_device_binding:
            return
        if self._device_id is None or self._model is None:
            return

        current = self._entry.data
        if (
            current.get(CONF_DEVICE_ID) == self._device_id
            and current.get(CONF_DEVICE_MODEL) == self._model
            and current.get(CONF_DEVICE_NAME) == self._device_name
        ):
            return

        updated = dict(current)
        updated[CONF_DEVICE_ID] = self._device_id
        updated[CONF_DEVICE_MODEL] = self._model
        updated[CONF_DEVICE_NAME] = self._device_name or self._device_id
        self.hass.config_entries.async_update_entry(self._entry, data=updated)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch battery + power data from the REST API."""
        client: EmaldoClient | None = None
        for attempt in range(2):
            try:
                client = await self.hass.async_add_executor_job(self._ensure_client)
                await self._async_persist_device_binding()
                battery = await self.hass.async_add_executor_job(
                    client.get_battery, self.home_id, self._device_id, self._model
                )
                power = await self.hass.async_add_executor_job(
                    client.get_power, self.home_id, self._device_id, self._model
                )
                break
            except EmaldoAuthError:
                self._reset_client()
                if attempt == 0:
                    _LOGGER.debug("Session expired, re-authenticating")
                    continue
                raise UpdateFailed("Authentication failed after retry") from None
            except EmaldoConnectionError as err:
                if attempt == 0:
                    _LOGGER.debug(
                        "REST connection error on attempt %d/2, retrying once: %s",
                        attempt + 1,
                        err,
                    )
                    # Recreate Session/connection pool in case a stale socket caused the drop.
                    self._reset_client()
                    await asyncio.sleep(1)
                    continue
                if (fallback := self._fallback_to_last_data_on_rest_failure(err)) is not None:
                    return fallback
                raise UpdateFailed(f"Connection error: {err}") from err
            except Exception as err:
                raise UpdateFailed(f"Error fetching Emaldo data: {err}") from err

        self._on_rest_recovered()

        # Solar MPPT stats — best-effort, not all devices have solar
        solar = None
        try:
            solar = await self.hass.async_add_executor_job(
                client.get_solar, self.home_id, self._device_id, self._model
            )
        except Exception as err:
            _LOGGER.debug("Solar stats fetch failed: %s", err)

        # EV charging mode + schedule — best-effort, only Power Core
        # hardware (with EV charger) exposes these commands. Errors are
        # swallowed so a device without EV support doesn't break the
        # whole coordinator refresh.
        # Throttle to every 5th poll (~5 min) to avoid opening a competing
        # UDP session on every 60s cycle.
        ev = self.data.get("ev") if self.data else None
        self._ev_poll_counter += 1
        if self._ev_poll_counter >= 5:
            self._ev_poll_counter = 0
            try:
                ev = await self.hass.async_add_executor_job(
                    self._read_ev_state
                )
            except Exception as err:
                _LOGGER.debug("EV state fetch failed: %s", err)

        import time as _time
        dp_ok = (
            battery.get("dual_power") is not None
            or power.get("dual_power") is not None
        )
        if not dp_ok:
            now = _time.time()
            if self._dual_power_fail_count == 0:
                self._dual_power_fail_since = now
            self._dual_power_fail_count += 1
            fail_dur = now - (self._dual_power_fail_since or now)
            if self._dual_power_fail_count == 1:
                _LOGGER.debug("is-dual-power-open unreachable (first occurrence)")
            elif fail_dur >= 3600 and now - self._dual_power_last_log >= 3600:
                _LOGGER.error(
                    "is-dual-power-open has been unreachable for >%.0fh",
                    fail_dur / 3600,
                )
                self._dual_power_last_log = now
            elif self._dual_power_fail_count == 2 or now - self._dual_power_last_log >= 600:
                _LOGGER.warning(
                    "is-dual-power-open unreachable (attempt %d)",
                    self._dual_power_fail_count,
                )
                self._dual_power_last_log = now
        else:
            if self._dual_power_fail_count > 1:
                _LOGGER.info(
                    "is-dual-power-open recovered after %d consecutive failures",
                    self._dual_power_fail_count,
                )
            self._dual_power_fail_count = 0
            self._dual_power_fail_since = None
            self._dual_power_last_log = 0.0

        return {
            "battery": battery,
            "power": power,
            "solar": solar,
            "ev": ev,
            "emergency_charge_active": self._emergency_charge_active,
            "emergency_charge_start_t": self._emergency_charge_start_t,
            "emergency_charge_end_t": self._emergency_charge_end_t,
        }

    def _read_ev_state(self) -> dict | None:
        """Read EV charging mode (wire 0x20) and schedule (wire 0x21).

        Runs synchronously in the executor. Returns a dict with keys
        ``mode``, ``fixed_kwh``, ``fixed_full_kwh``, ``price_percent``,
        ``weekdays`` (list of 24 ints), ``weekend`` (list of 24 ints),
        and ``sync`` (int), or *None* if the device doesn't support EV
        commands / both reads failed.
        """
        client = self._ensure_client()
        creds = client.get_e2e_credentials(self.home_id, self._device_id, self._model)

        # 1) Current mode + fixed values (0x20 → 6 bytes)
        session_nonce = generate_nonce()
        mode_pkt = build_subscription_packet(
            creds, _EV_TYPE_GET_STATE, session_nonce, payload=b"",
        )
        mode_results = _run_session(
            creds, [("Read EV mode", mode_pkt)], timeout=3.0,
        )
        _, mode_resp = mode_results[0]
        mode_info = None
        if mode_resp is not None:
            mode_dec = decrypt_response(
                mode_resp, creds["chat_secret"],
                payload_validator=lambda p: len(p) == 6,
            )
            mode_info = parse_ev_charging_info(mode_dec)

        if mode_info is None:
            return None

        # 2) Schedule bitmaps (0x21 → 7 bytes)
        sched_nonce = generate_nonce()
        sched_pkt = build_subscription_packet(
            creds, 0x21, sched_nonce, payload=b"",
        )
        sched_results = _run_session(
            creds, [("Read EV schedule", sched_pkt)], timeout=3.0,
        )
        _, sched_resp = sched_results[0]
        weekdays = [0] * 24
        weekend = [0] * 24
        sync_flag = 0
        if sched_resp is not None:
            sched_dec = decrypt_response(
                sched_resp, creds["chat_secret"],
                payload_validator=lambda p: len(p) >= 7,
            )
            if sched_dec is not None and len(sched_dec) >= 7:
                # Bytes 0..2 = weekdays bitmap, 3..5 = weekend, 6 = sync
                def _unpack(bs: bytes) -> list[int]:
                    return [(b >> i) & 1 for b in bs for i in range(8)]
                weekdays = _unpack(sched_dec[0:3])
                weekend = _unpack(sched_dec[3:6])
                sync_flag = sched_dec[6]

        return {
            **mode_info,
            "weekdays": weekdays,
            "weekend": weekend,
            "sync": sync_flag,
        }

    def _write_ev_mode(self, mode: int, fixed_kwh: int = 0) -> bool:
        """Write EV charging mode via E2E (synchronous, call via executor).

        Modes 1-3 are Smart sub-modes; 4-5 are Instant sub-modes.
        Returns True if the device acknowledged the write.
        """
        client = self._ensure_client()
        creds = client.get_e2e_credentials(self.home_id, self._device_id, self._model)
        if mode in (1, 2, 3):
            return set_ev_charging_mode_smart(creds, mode)
        if mode in (EV_MODE_INSTANT_FULL, EV_MODE_INSTANT_FIXED):
            return set_ev_charging_mode_instant(creds, mode, fixed_kwh=fixed_kwh)
        raise ValueError(f"Unknown EV mode: {mode}")

    def _send_emergency_charge_via_stream(
        self, payload: bytes, label: str,
    ) -> bool:
        """Send E2E command on the persistent stream socket.

        Ensures the stream session exists (creates if needed) and sends the
        command through it, avoiding the second-socket conflict entirely.

        Returns True if relay acknowledged, False otherwise.
        """
        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            if not entry_data:
                return False
            for item in entry_data.get("devices", [entry_data]):
                if item.get("power") is self:
                    rt = item.get("realtime")
                    if rt is None:
                        return False
                    session = rt._ensure_session()  # noqa: SLF001
                    resp = session.send_command(0x01, payload)
                    if resp is None:
                        _LOGGER.debug(
                            "[EmergencyCharge] %s stream cmd: timeout / no response "
                            "device=%s is_primary=%s",
                            label, self._device_id, getattr(rt, "_is_primary", True),
                        )
                        return False
                    if b"CONN_NOT_ESTABLISHED" in resp:
                        _LOGGER.debug(
                            "[EmergencyCharge] %s stream cmd: CONN_NOT_ESTABLISHED "
                            "device=%s is_primary=%s",
                            label, self._device_id, getattr(rt, "_is_primary", True),
                        )
                        return False
                    _LOGGER.debug(
                        "[EmergencyCharge] %s stream cmd: OK resp_len=%s "
                        "device=%s is_primary=%s",
                        label, len(resp), self._device_id,
                        getattr(rt, "_is_primary", True),
                    )
                    return True
        except Exception:
            _LOGGER.debug(
                "[EmergencyCharge] %s stream cmd: exception", label, exc_info=True,
            )
        return False

    def _write_emergency_charge_on(
        self, start_unix: int | None = None, end_unix: int | None = None
    ) -> None:
        """Start emergency charge via the persistent stream E2E session.

        If *start_unix* is None the device starts immediately (now).
        If *end_unix* is None the fallback is 1 hour from now.

        Attempt 0: stream path.  Attempt 1: standalone legacy command with
        fresh credentials (bypasses broken stream, #47).
        """
        import time as _time
        if start_unix is None:
            start_unix = int(_time.time())
        if end_unix is None:
            end_unix = start_unix + 3600
        payload = struct.pack("<BII", 1, start_unix, end_unix)
        _LOGGER.debug(
            "[EmergencyCharge] _write_emergency_charge_on start=%d end=%d device=%s",
            start_unix, end_unix, self._device_id,
        )
        client = self._ensure_client()
        for attempt in range(2):
            try:
                if attempt == 1:
                    rt = self._get_paired_realtime()
                    if rt is not None and not rt._is_primary:
                        _LOGGER.warning(
                            "[EmergencyCharge] ON failed on stream path for "
                            "secondary device %s — legacy fallback skipped "
                            "(would collide with shared session)",
                            self._device_id,
                        )
                        raise EmaldoE2ESessionExpired(
                            "Emergency charge ON: stream path failed, "
                            "secondary device cannot use legacy fallback"
                        )
                    _LOGGER.debug(
                        "[EmergencyCharge] ON retry via legacy standalone command"
                    )
                    ok = client.emergency_charge_window(
                        self.home_id, self._device_id, self._model,
                        start_unix=start_unix, end_unix=end_unix,
                    )
                else:
                    ok = self._send_emergency_charge_via_stream(payload, "ON")
                if not ok:
                    raise EmaldoE2ESessionExpired(
                        "Emergency charge ON rejected by relay"
                    )
                self._emergency_charge_active = True
                return
            except EmaldoE2ESessionExpired:
                self._close_realtime_session()
                client.invalidate_e2e_session(
                    self.home_id, self._device_id, self._model
                )
                if attempt == 1:
                    raise
            except EmaldoAuthError:
                self._reset_client()
                client = self._ensure_client()
                if attempt == 1:
                    raise

    def _write_emergency_charge_off(self) -> None:
        """Cancel active emergency charge.

        Attempt 0: stream path.  Attempt 1: standalone legacy command with
        fresh credentials (bypasses broken stream, #47).
        """
        payload = bytes(9)
        _LOGGER.debug(
            "[EmergencyCharge] _write_emergency_charge_off device=%s",
            self._device_id,
        )
        client = self._ensure_client()
        for attempt in range(2):
            try:
                if attempt == 1:
                    rt = self._get_paired_realtime()
                    if rt is not None and not rt._is_primary:
                        _LOGGER.warning(
                            "[EmergencyCharge] OFF failed on stream path for "
                            "secondary device %s — legacy fallback skipped "
                            "(would collide with shared session)",
                            self._device_id,
                        )
                        raise EmaldoE2ESessionExpired(
                            "Emergency charge OFF: stream path failed, "
                            "secondary device cannot use legacy fallback"
                        )
                    _LOGGER.debug(
                        "[EmergencyCharge] OFF retry via legacy standalone command"
                    )
                    ok = client.emergency_charge_off(
                        self.home_id, self._device_id, self._model,
                    )
                else:
                    ok = self._send_emergency_charge_via_stream(payload, "OFF")
                if not ok:
                    raise EmaldoE2ESessionExpired(
                        "Emergency charge OFF rejected by relay"
                    )
                self._emergency_charge_active = False
                return
            except EmaldoE2ESessionExpired:
                self._close_realtime_session()
                client.invalidate_e2e_session(
                    self.home_id, self._device_id, self._model
                )
                if attempt == 1:
                    raise
            except EmaldoAuthError:
                self._reset_client()
                client = self._ensure_client()
                if attempt == 1:
                    raise

    def _close_realtime_session(self) -> None:
        """Close paired realtime coordinator's E2E stream session.

        Used after a stream-path command failure to force the next
        ``_ensure_session()`` call to create a fresh session (with a new
        handshake and potentially fresh credentials).
        """
        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            if not entry_data:
                return
            for item in entry_data.get("devices", [entry_data]):
                if item.get("power") is self:
                    realtime = item.get("realtime")
                    if realtime is not None:
                        future = asyncio.run_coroutine_threadsafe(
                            realtime._close_session(), self.hass.loop
                        )
                        future.result(timeout=5)
                    return
        except Exception:
            _LOGGER.debug("Failed to close paired stream session", exc_info=True)

    def _get_paired_realtime(self) -> EmaldoRealtimeCoordinator | None:
        """Return the paired realtime coordinator for this device."""
        try:
            entry_data = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            if not entry_data:
                return None
            for item in entry_data.get("devices", [entry_data]):
                if item.get("power") is self:
                    return item.get("realtime")
        except Exception:
            return None
        return None


class EmaldoRealtimeCoordinator(DataUpdateCoordinator[dict[str, Any] | None]):
    """Fast coordinator for E2E real-time power flow (10s).

    Uses :class:`PersistentE2ESession` to keep a UDP socket open across polls,
    reducing latency from ~500ms to ~85ms per read. A background task sends
    keepalive messages every 7 seconds to prevent the relay server from
    dropping the session (relay TTL ~10 s).
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        parent: EmaldoCoordinator,
        *,
        is_primary: bool = True,
    ) -> None:
        """Initialize the realtime coordinator.

        Args:
            hass: Home Assistant instance.
            entry: Config entry.
            parent: The slow :class:`EmaldoCoordinator` — used to share the
                authenticated REST client and device discovery.
            is_primary: ``True`` (default) means this coordinator owns the
                session (sends keepalive, reconnects on expiry). ``False`` —
                secondary device shares the primary's session via
                :meth:`PersistentE2ESession.read_power_flow_for_creds` and
                never sends ``Alive(home)`` (#47 multi-device collision fix).
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_realtime",
            update_interval=timedelta(seconds=REALTIME_SCAN_INTERVAL),
        )
        self._entry = entry
        self._parent = parent
        self._is_primary = is_primary
        # Realtime transport mode is per-entry (default from const). Some
        # networks drop the device-initiated push datagrams the stream model
        # needs; those users can select the poll model in the options flow (#41).
        self._stream_mode: bool = bool(
            entry.options.get(CONF_REALTIME_STREAM_MODE, REALTIME_STREAM_MODE)
        )
        self._session: PersistentE2ESession | None = None
        self._session_binding: tuple[str, str, str] | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._empty_reads: int = 0
        self._consecutive_read_errors: int = 0
        self._consecutive_reconnects: int = 0
        self._episode_drops: int = 0
        # Set when a session teardown follows a confirmed failure (empty-read
        # stall, undecryptable responses). The next `_ensure_session` then pulls
        # genuinely fresh E2E credentials instead of reusing the shared 10-min
        # cache, matching beta9's implicit "fresh e2e_login on every reconnect".
        self._needs_fresh_creds: bool = False
        # Consecutive poll-mode reads where the relay answered but nothing could
        # be decrypted/parsed (the stale-`chat_secret` signature). Distinct from
        # empty reads (no response at all).
        self._undecryptable_streak: int = 0
        # Frozen snapshot of key counters captured the moment a stall begins
        # (recent success rate first hits 0), preserved for after-the-fact
        # diagnosis since users report long after the stall started.
        self._stall_active: bool = False
        self._stall_snapshot: dict | None = None
        # Consecutive stream-mode full-reset escalations without a successful
        # read in between. Repeated resets mean the device-push frames simply
        # aren't reaching Home Assistant (restrictive NAT/firewall/CGNAT) — a
        # transport limitation no client reset can fix (#41, #47). Used to emit
        # a one-time "switch to poll mode" recommendation.
        self._consecutive_stream_stall_resets: int = 0
        self._stream_poll_mode_hint_logged: bool = False
        self._regulate_frequency: dict | None = None
        # Seed so the first successful poll triggers an auxiliary-state read
        # immediately, regardless of the (mode-dependent) cadence below.
        self._balancing_poll_counter: int = (
            self._BALANCING_POLL_INTERVAL_STREAM
            if self._stream_mode
            else self._BALANCING_POLL_INTERVAL
        ) - 1
        self._battery_modules_poll_counter: int = 59  # trigger on first successful poll
        self._battery_modules: list[dict] = []
        # Currently-running battery module scan task (if any). The 0x06 scan
        # probes up to 13 cabinet slots and can take tens of seconds; running
        # it in the background keeps `_async_update_data` from blocking, so the
        # first power-flow reading reaches sensors immediately.
        self._battery_scan_task: asyncio.Task | None = None
        # Modules keyed by scan slot index so each physical cabinet slot keeps
        # a stable HA sensor even when modules respond in different orders or
        # some slots are temporarily silent (#23).
        self._battery_module_slots: dict[int, dict] = {}
        # -- Stats for diagnostic sensor --
        self.stats_total_polls: int = 0
        self.stats_successful_polls: int = 0
        # Rolling success window (beta13e): outcome (True/False) of the most
        # recent polls, used for a "recent" success rate that recovers after an
        # outage instead of being permanently dragged down like the cumulative
        # rate. The previous poll's outcome is finalised at the top of the next
        # poll (a one-poll lag, negligible for a 240-sample window).
        self._recent_poll_outcomes: deque[bool] = deque(
            maxlen=REALTIME_SUCCESS_WINDOW
        )
        self._recent_window_prev_success: int = 0
        self.stats_empty_reads: int = 0
        self.stats_reconnects: int = 0
        self.stats_keepalive_failures: int = 0
        self.stats_reconnect_probes: int = 0
        self.stats_reconnects_avoided: int = 0
        self.stats_last_success: float | None = None
        self.stats_last_failure: float | None = None
        self.stats_last_read_error: str | None = None
        self.stats_last_reconnect: float | None = None
        self.stats_last_reconnect_reason: str | None = None
        self.stats_reconnect_reasons: dict[str, int] = {
            "auth_expired": 0,
            "read_error": 0,
            "empty_reads": 0,
            "invalid_payload": 0,
            "poll_stall_reset": 0,
            "stream_stall_reset": 0,
            "keepalive_session_expired_21204": 0,
            "keepalive_closed": 0,
            "keepalive_exception": 0,
            "keepalive_response_timeout": 0,
            "keepalive_other": 0,
        }
        self.stats_keepalive_failures_session_expired: int = 0
        self.stats_keepalive_failures_closed: int = 0
        self.stats_keepalive_failures_exception: int = 0
        self.stats_keepalive_failures_other: int = 0
        self.stats_keepalive_failures_response_timeout: int = 0
        # Lifetime count of poll-mode reads where the relay answered but the
        # payload could not be decrypted/parsed (stale-secret signature).
        self.stats_undecryptable_polls: int = 0
        # Outcome of the most recent session handshake (ok/no_response/21204).
        self.stats_last_handshake_response: str | None = None
        self.stats_e2e_rtt_last_ms: float | None = None
        self.stats_e2e_rtt_min_ms: float | None = None
        self.stats_e2e_rtt_max_ms: float | None = None
        self.stats_e2e_rtt_total_ms: float = 0.0
        self.stats_e2e_rtt_samples: int = 0
        self.stats_empty_reconnect_deferrals_healthy_keepalive: int = 0
        self.stats_powerflow_initial_timeouts: int = 0
        self.stats_powerflow_initial_session_expired: int = 0
        self.stats_powerflow_initial_nonmatching: int = 0
        self.stats_powerflow_drain_packets_seen: int = 0
        self.stats_powerflow_drain_regfreq_hits: int = 0
        self.stats_powerflow_drain_powerflow_hits: int = 0
        self.stats_powerflow_drain_session_expired: int = 0
        self.stats_powerflow_drain_timeouts: int = 0
        self.stats_powerflow_drain_exhausted: int = 0
        self.stats_powerflow_last_diag: dict[str, int | bool] = {}
        # -- Cumulative subscribe-and-stream counters (survive reconnects) --
        self.stats_stream_frames_total: int = 0
        self.stats_stream_resubscribes_total: int = 0
        self.stats_stream_reconnects_total: int = 0
        self._last_stream_frames_seen: int = 0
        self._last_stream_resubs_seen: int = 0
        self._last_stream_reconnects_seen: int = 0
        # Latest stream diagnostics snapshot (reconnect-reason breakdown etc.)
        self._stream_diag: dict = {}
        # Diagnostic info collected by executor thread, logged from main thread
        self._e2e_diag: str = "(not yet called)"
        self._last_keepalive_success: float | None = None
        self._empty_reconnect_deferral_streak: int = 0
        #: Set to ``True`` once the very first successful E2E read completes.
        #: Entities use this to suppress state writes during the initial
        #: handshake/reconnect phase, preventing a flash of "unavailable" over
        #: previously restored sensor values on HA restart.
        self._successful_first_refresh: bool = False
        # -- Legacy (beta9-style) fallback -----------------------------------
        # When the persistent-session model cannot recover on a network (the
        # stream/poll full-reset escalations keep firing with no successful read
        # in between), fall back to the beta9 read model: a fresh UDP socket +
        # full handshake + single 0x30 read per poll, no persistent session.
        # Both #41 and #47 reporters confirm beta9 is stable on their networks
        # while the persistent model yields zero usable frames even after full
        # client resets. Latched until the integration is reloaded.
        self._legacy_fallback_active: bool = False
        # Full-reset escalations (stream_stall_reset / poll_stall_reset) since
        # the last successful read. Any successful read resets this to 0.
        self._stall_resets_without_success: int = 0

    # -- Proxy properties so sensors can share one class across coordinators --

    @property
    def home_id(self) -> str:
        return self._parent.home_id

    @property
    def device_id(self) -> str | None:
        return self._parent.device_id

    @property
    def device_model(self) -> str | None:
        return self._parent.device_model

    @property
    def device_name(self) -> str | None:
        return self._parent.device_name

    async def async_shutdown(self) -> None:
        """Cancel keepalive, battery scan and close the UDP session."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        if self._battery_scan_task is not None:
            self._battery_scan_task.cancel()
            try:
                await self._battery_scan_task
            except (asyncio.CancelledError, Exception):
                pass
            self._battery_scan_task = None
        if self._session is not None:
            if self._is_primary:
                _LOGGER.debug(
                    "Shutdown: closing shared E2E session (primary device %s)",
                    self.device_id,
                )
                await self.hass.async_add_executor_job(self._session.close)
                self._pop_shared_session()
            else:
                _LOGGER.debug(
                    "Shutdown: not closing E2E session for secondary device %s "
                    "(shared with primary)",
                    self.device_id,
                )
            self._invalidate_session_ref()

    def _invalidate_session_ref(self) -> None:
        """Drop local references to the active E2E session and binding."""
        self._session = None
        self._session_binding = None
        # Restart per-session stream counter tracking from zero so the next
        # session's counters accumulate correctly into the cumulative totals.
        self._last_stream_frames_seen = 0
        self._last_stream_resubs_seen = 0
        self._last_stream_reconnects_seen = 0

    # -- Shared E2E session (one per config entry, #47) --------------------- #

    def _get_shared_session(self) -> PersistentE2ESession | None:
        """Return the config-entry-wide shared E2E session, or *None*."""
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if data is None:
            return None
        return data.get("e2e_session")

    def _set_shared_session(self, session: PersistentE2ESession) -> None:
        """Store the config-entry-wide shared E2E session (primary only)."""
        self.hass.data.setdefault(DOMAIN, {}).setdefault(
            self._entry.entry_id, {}
        )["e2e_session"] = session

    def _pop_shared_session(self) -> PersistentE2ESession | None:
        """Remove and return the shared E2E session (primary only)."""
        data = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id)
        if data is None:
            return None
        return data.pop("e2e_session", None)

    # ----------------------------------------------------------------------- #

    def _ensure_session(self) -> PersistentE2ESession:
        """Return the config-entry-wide shared E2E session.

        The primary coordinator owns the session — creates it, sends
        ``Alive(home)``, handles keepalive and reconnection. Secondary
        coordinators look up the shared session and never send
        ``Alive(home)``, preventing relay collisions (#47).

        Session scope is one per config entry (home), not one per device.
        """
        shared = self._get_shared_session()
        if shared is not None and not shared.closed:
            # Shared session alive — use it regardless of primary/secondary.
            # Secondary coordinators never create their own session.
            self._session = shared
            if not self._is_primary:
                _LOGGER.debug(
                    "Shared E2E session acquired for secondary device %s",
                    self.device_id,
                )
            return shared

        if not self._is_primary:
            # Session closed and we're not the owner — cannot recreate.
            # The primary coordinator will recreate it on its next poll.
            _LOGGER.debug(
                "Shared E2E session not available for secondary device %s "
                "(waiting for primary)",
                self.device_id,
            )
            raise EmaldoE2EError(
                "Shared E2E session closed; waiting for primary reconnect"
            )

        # -- Primary device path: create or recreate the shared session ------ #
        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")

        binding = (home_id, device_id, model)

        # Close any stale session from a previous binding change
        if self._session is not None and not self._session.closed:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._invalidate_session_ref()

        creds = client.get_e2e_credentials(
            home_id, device_id, model, force_refresh=self._needs_fresh_creds
        )
        if self._needs_fresh_creds:
            _LOGGER.info(
                "E2E session rebuild after failure — forced fresh credentials"
            )
        self._needs_fresh_creds = False
        _LOGGER.debug("E2E session host: %s", creds.get("host", "(default)"))
        # Provider so the stream thread can pull the latest shared chat_secret
        # on reconnect (a concurrent REST e2e_login rotates it; re-handshaking
        # with the stale secret would 21204 forever).
        def _creds_provider(*, force_refresh: bool = False) -> dict:
            return client.get_e2e_credentials(
                home_id, device_id, model, force_refresh=force_refresh
            )

        self._session = PersistentE2ESession(
            creds,
            log=lambda msg: _LOGGER.debug("[E2E] %s", msg),
            creds_provider=_creds_provider,
        )
        self._session.connect()
        self.stats_last_handshake_response = self._session.last_handshake_response
        # Register as the config-entry-wide shared session. Secondary
        # coordinators will find it via _get_shared_session().
        self._set_shared_session(self._session)
        # Set binding BEFORE frame wait so racing threads see it and avoid
        # closing this session out from under the frame-wait loop (#47).
        self._session_binding = binding
        if self._stream_mode:
            # Subscribe-and-stream: a background thread owns the power-flow
            # subscription and keepalive cadence (beta13d). The coordinator's
            # separate keepalive loop is not started in this mode.
            self._session.start_stream(
                resubscribe_interval=RESUBSCRIBE_INTERVAL,
                keepalive_interval=KEEPALIVE_INTERVAL,
                frame_gap_resubscribe=STREAM_FRAME_GAP_RESUBSCRIBE,
                min_resubscribe_gap=STREAM_MIN_RESUBSCRIBE_GAP,
                stale_after=STREAM_STALE_AFTER,
                long_stall=STREAM_LONG_STALL_RECONNECT,
            )
            # Cold start: give the freshly started stream a brief moment to
            # finish the handshake + subscribe and cache the device's first
            # pushed frame, so the poll that started the stream already returns
            # data instead of an empty read (which would otherwise leave the
            # realtime sensors on the restored/"unknown" value until the next
            # poll). This runs on the executor thread, so it never blocks the
            # event loop, and the first refresh is a background task so HA
            # startup is unaffected. Exits early the instant a frame arrives.
            import time as _time
            _first_frame_deadline = _time.perf_counter() + STREAM_FIRST_FRAME_WAIT
            _session = self._session
            while _time.perf_counter() < _first_frame_deadline:
                if (
                    _session.get_latest_power_flow(max_age=STREAM_STALE_AFTER)
                    is not None
                ):
                    break
                _time.sleep(0.2)
        return self._session

    def _read_power_flow(self) -> dict | None:
        """Synchronous helper that runs in the executor."""
        if self._legacy_fallback_active:
            if not self._is_primary:
                # Secondary cannot use legacy fallback (would send
                # Alive(home) and collide with primary session).
                return None
            # Persistent/stream session gave up on this network — use the
            # beta9-style one-shot read model (see _read_power_flow_legacy).
            return self._read_power_flow_legacy()
        session = self._ensure_session()
        if self._is_primary and self._stream_mode:
            # Stream mode: the background receiver keeps the freshest frame
            # cached. Just read it; the subscribe/keepalive/reconnect work is
            # all handled by the stream thread.
            data = session.get_latest_power_flow(max_age=STREAM_STALE_AFTER)
            self._accumulate_stream_stats(session)
            self._stream_diag = session.stream_diagnostics()
            self._e2e_diag = (
                f"stream frames={self.stats_stream_frames_total} "
                f"resubs={self.stats_stream_resubscribes_total} "
                f"reconnects={self.stats_stream_reconnects_total} "
                f"pkts={self._stream_diag.get('drain_packets')} "
                f"unparsed={self._stream_diag.get('drain_unparsed')} "
                f"last_reason={self._stream_diag.get('last_reconnect_reason')} "
                f"result={'data' if data else 'None'}"
            )
            if data is None and session.closed:
                self._invalidate_session_ref()
            return data
        # Collect diagnostic info — will be logged from main thread in _async_update_data
        self._e2e_diag = f"host={session._creds.get('host','?')} has_log={session._log is not None} closed={session.closed}"
        if self._is_primary:
            data = session.read_power_flow()
        else:
            # Secondary device: use own credentials for 0x30 subscription
            # without sending Alive(home) (#47).
            data = self._read_power_flow_secondary(session)
        self._e2e_diag += f" result={'data' if data else 'None'}"
        diag = session.last_power_flow_diag
        self.stats_powerflow_last_diag = diag
        self.stats_powerflow_initial_timeouts += int(diag.get("initial_timeout", 0))
        self.stats_powerflow_initial_session_expired += int(
            diag.get("initial_session_expired", 0)
        )
        self.stats_powerflow_initial_nonmatching += int(
            diag.get("initial_nonmatching", 0)
        )
        self.stats_powerflow_drain_packets_seen += int(
            diag.get("drain_packets_seen", 0)
        )
        self.stats_powerflow_drain_regfreq_hits += int(
            diag.get("drain_regfreq_hits", 0)
        )
        self.stats_powerflow_drain_powerflow_hits += int(
            diag.get("drain_powerflow_hits", 0)
        )
        self.stats_powerflow_drain_session_expired += int(
            diag.get("drain_session_expired", 0)
        )
        self.stats_powerflow_drain_timeouts += int(
            diag.get("drain_timeout", 0)
        )
        self.stats_powerflow_drain_exhausted += int(
            diag.get("drain_exhausted", 0)
        )
        rtt_ms = session.last_rtt_ms
        if rtt_ms is not None:
            self.stats_e2e_rtt_last_ms = round(rtt_ms, 1)
            self.stats_e2e_rtt_total_ms += rtt_ms
            self.stats_e2e_rtt_samples += 1
            if self.stats_e2e_rtt_min_ms is None or rtt_ms < self.stats_e2e_rtt_min_ms:
                self.stats_e2e_rtt_min_ms = rtt_ms
            if self.stats_e2e_rtt_max_ms is None or rtt_ms > self.stats_e2e_rtt_max_ms:
                self.stats_e2e_rtt_max_ms = rtt_ms
        if data is None and session.closed:
            # Session died mid-read — force recreation on next call
            self._invalidate_session_ref()
        return data

    def _read_power_flow_secondary(self, session: PersistentE2ESession) -> dict | None:
        """Read power flow for a secondary device via shared session.

        Fetches own E2E credentials and sends 0x30 subscription through the
        primary's session socket without sending ``Alive(home)``.
        """
        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")
        _LOGGER.debug(
            "Secondary power-flow read for device %s via shared session",
            device_id,
        )
        creds = client.get_e2e_credentials(
            home_id, device_id, model, force_refresh=self._needs_fresh_creds
        )
        if self._needs_fresh_creds:
            _LOGGER.info(
                "Secondary E2E read with forced fresh credentials"
            )
        self._needs_fresh_creds = False
        return session.read_power_flow_for_creds(creds)

    def _accumulate_stream_stats(self, session: PersistentE2ESession) -> None:
        """Roll per-session stream counters into cumulative coordinator totals.

        The session's ``_stream_*`` counters reset to 0 whenever the session is
        rebuilt (reconnect / rebind). Tracking the last-seen value and adding
        deltas — or the full value after a reset — keeps the diagnostic totals
        monotonic across the whole integration lifetime.
        """
        for cur, last_attr, total_attr in (
            (session._stream_frames_received, "_last_stream_frames_seen", "stats_stream_frames_total"),  # noqa: SLF001
            (session._stream_resubscribes, "_last_stream_resubs_seen", "stats_stream_resubscribes_total"),  # noqa: SLF001
            (session._stream_reconnects, "_last_stream_reconnects_seen", "stats_stream_reconnects_total"),  # noqa: SLF001
        ):
            last = getattr(self, last_attr)
            if cur >= last:
                setattr(self, total_attr, getattr(self, total_attr) + (cur - last))
            else:
                # Counter reset (new session object) — add the new value whole.
                setattr(self, total_attr, getattr(self, total_attr) + cur)
            setattr(self, last_attr, cur)

    def _record_reconnect(self, reason: str, ts: float) -> None:
        """Record a reconnect/reset event with a classified root cause."""
        self.stats_reconnects += 1
        self.stats_last_reconnect = ts
        self.stats_last_reconnect_reason = reason
        self.stats_reconnect_reasons[reason] = (
            self.stats_reconnect_reasons.get(reason, 0) + 1
        )

    def _maybe_update_stall_snapshot(self) -> None:
        """Freeze diagnostics the moment the recent window goes fully cold.

        Users report stalls hours after they begin, by which point the live
        counters have moved on. Capturing a one-shot snapshot at stall onset
        preserves the state that actually matters for diagnosis. The snapshot is
        re-armed once a successful poll re-warms the rolling window.
        """
        window = self._recent_poll_outcomes
        min_samples = min(REALTIME_SUCCESS_WINDOW, 12)
        recent_cold = len(window) >= min_samples and not any(window)
        if recent_cold and not self._stall_active:
            self._stall_active = True
            self._stall_snapshot = self._build_stall_snapshot()
            _LOGGER.warning(
                "E2E realtime stall detected (no successful poll in the last %d "
                "polls) — snapshot captured for diagnostics: %s",
                len(window),
                self._stall_snapshot,
            )
        elif any(window):
            self._stall_active = False

    def _build_stall_snapshot(self) -> dict[str, Any]:
        """Assemble the diagnostic snapshot recorded at stall onset."""
        return {
            "captured": datetime.now().astimezone().isoformat(timespec="seconds"),
            "stream_mode": self._stream_mode,
            "total_polls": self.stats_total_polls,
            "successful_polls": self.stats_successful_polls,
            "empty_reads_lifetime": self.stats_empty_reads,
            "consecutive_reconnects": self._consecutive_reconnects,
            "reconnects_lifetime": self.stats_reconnects,
            "undecryptable_streak": self._undecryptable_streak,
            "undecryptable_polls_lifetime": self.stats_undecryptable_polls,
            "last_reconnect_reason": self.stats_last_reconnect_reason,
            "last_read_error": self.stats_last_read_error,
            "last_handshake_response": self.stats_last_handshake_response,
            "powerflow_last_diag": dict(self.stats_powerflow_last_diag),
            "stream_diag": dict(self._stream_diag) if self._stream_mode else None,
            "e2e_rtt_last_ms": self.stats_e2e_rtt_last_ms,
        }

    def _write_thirdparty_pv(self, enabled: bool) -> None:
        """Send SET_THIRDPARTYPV_ON (0x41) via the existing persistent session.

        Routing the command through the active session socket avoids the
        conflict that would arise from opening a second socket with
        ``_run_session`` while the keepalive loop is running.

        On auth or session expiry the client and session are reset and the
        command is retried once with fresh credentials.
        """
        payload = bytes([0x01 if enabled else 0x00])
        for attempt in range(2):
            try:
                session = self._ensure_session()
                session.send_command(0x41, payload)
                return
            except EmaldoAuthError:
                # REST token expired — force full re-login on next _ensure_session
                self._parent._reset_client()  # noqa: SLF001
                self._invalidate_session_ref()
                if attempt == 1:
                    raise
            except EmaldoE2EError:
                # UDP session closed/expired — drop it so _ensure_session reconnects
                self._invalidate_session_ref()
                if attempt == 1:
                    raise

    def _write_sell_back_to_grid(self, enabled: bool) -> None:
        """Enable or disable grid export (sell-back to grid) via set_virtualpowerplant.

        Uses ``set_virtualpowerplant`` (type 0x05).  The APK dispatcher
        (case 47) includes the account user-id in the payload when the user is
        logged in; the device firmware uses it for VPP authorisation.
        Payload: ``[on(1B), len(1B), user_id_utf8(N B)]`` when user_id is
        available, or ``[on(1B)]`` only as a fallback.
        """
        for attempt in range(2):
            try:
                session = self._ensure_session()
                user_id: str = session._creds.get("user_id", "")  # noqa: SLF001
                payload = _build_vpp_payload(enabled, user_id)
                session.send_command(_VIRTUALPOWERPLANT_SET_TYPE, payload)
                return
            except EmaldoAuthError:
                self._parent._reset_client()  # noqa: SLF001
                self._invalidate_session_ref()
                if attempt == 1:
                    raise
            except EmaldoE2EError:
                self._invalidate_session_ref()
                if attempt == 1:
                    raise

    def _read_virtualpowerplant(self) -> dict | None:
        """Read sell-back-to-grid state (0x06) via the persistent session."""
        session = self._ensure_session()
        return session.read_virtualpowerplant()

    def _write_sell_limit(self, enabled: bool, threshold: int) -> None:
        """Set sell-limit protection (set_sellingprotection, type 0x5E).

        Args:
            enabled:   True = sell limit active; False = no limit.
            threshold: Daily limit in kWh/day (1-300).
        """
        payload = struct.pack("<BI", 0x01 if enabled else 0x00, max(0, threshold))
        for attempt in range(2):
            try:
                session = self._ensure_session()
                session.send_command(_SELLING_PROTECTION_SET_TYPE, payload)
                return
            except EmaldoAuthError:
                self._parent._reset_client()  # noqa: SLF001
                self._invalidate_session_ref()
                if attempt == 1:
                    raise
            except EmaldoE2EError:
                self._invalidate_session_ref()
                if attempt == 1:
                    raise

    def _read_sell_limit(self) -> dict | None:
        """Read sell-limit (selling protection) state via the persistent session."""
        session = self._ensure_session()
        return session.read_selling_protection()

    def _write_manual_selling(self, on: bool, target_kwh: int) -> None:
        """Enable or disable manual energy selling (type 0x80).

        Args:
            on:         True = start selling; False = stop selling.
            target_kwh: Target energy to sell in kWh (integer, ignored when off).
        """
        payload = struct.pack("<BIB", 1 if on else 0, max(0, target_kwh), 0)
        for attempt in range(2):
            try:
                session = self._ensure_session()
                session.send_command(0x80, payload)
                return
            except EmaldoAuthError:
                self._parent._reset_client()  # noqa: SLF001
                self._invalidate_session_ref()
                if attempt == 1:
                    raise
            except EmaldoE2EError:
                self._invalidate_session_ref()
                if attempt == 1:
                    raise

    def _read_manual_selling(self) -> dict | None:
        """Read manual-selling state (0x81) via the persistent session."""
        session = self._ensure_session()
        return session.read_manual_selling()

    def _read_battery_info_standalone(self) -> list[dict]:
        """Read per-module battery info on a throwaway one-shot E2E session.

        The 0x06 scan probes up to 13 cabinet slots, each with its own
        request/response round trip.  Running it on the persistent realtime
        socket would hold the session lock for tens of seconds, starving the
        keepalive task (7 s) and letting the relay drop the realtime session
        (#37).  Opening a dedicated socket that is torn down immediately after
        the scan keeps the realtime session healthy.
        """
        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            return self._battery_modules

        import time as _time
        # Serial -> slot map from prior scans so a stray late datagram (a module
        # whose rightful slot timed out this round) cannot be misassigned to the
        # slot it leaked into (#44).
        known_serial_slots = {
            m["serial"]: slot
            for slot, m in self._battery_module_slots.items()
            if m.get("serial")
        }
        for attempt in range(2):
            try:
                creds = client.get_e2e_credentials(
                    home_id, device_id, model, force_refresh=(attempt > 0)
                )
                return _standalone_read_battery_info(
                    creds,
                    known_serial_slots=known_serial_slots,
                    log=lambda msg: _LOGGER.debug("[E2E battery] %s", msg),
                )
            except EmaldoAuthError:
                # Token/session may have expired between polls.
                self._parent._reset_client()  # noqa: SLF001
                if attempt == 0:
                    continue
                raise
            except EmaldoConnectionError:
                # Short cloud/API disconnect — rebuild client once and retry.
                self._parent._reset_client()  # noqa: SLF001
                if attempt == 0:
                    _time.sleep(1)
                    continue
                raise

        return self._battery_modules

    #: Tolerate this many consecutive empty reads before surfacing unavailable.
    _MAX_EMPTY_READS = 3
    #: In stream mode, after this many consecutive empty reads (no fresh frame)
    #: the in-thread reconnect is presumed wedged (e.g. a dead shared REST token
    #: after a cloud outage). Escalate to a full REST-client reset + session
    #: rebuild so recovery does not depend on an HA restart (beta13g).
    _STREAM_STALL_RESET_POLLS = max(
        1, STREAM_STALL_FULL_RESET_SECONDS // REALTIME_SCAN_INTERVAL
    )
    #: After this many consecutive stream full-reset escalations with no
    #: successful read in between, the device-push frames are almost certainly
    #: being dropped by the network — recommend poll mode once (#41, #47).
    _STREAM_STALL_POLL_HINT_RESETS = 3
    #: Auxiliary-state (balancing / VPP / sell-limit / manual-selling) poll
    #: cadence, counted in successful power-flow reads. In STREAM mode these
    #: reads share the session socket with the background receiver: each one
    #: briefly holds the session lock and can consume/discard buffered 0x30
    #: push frames, so they run 4x less often to minimise contention with the
    #: stream (all four states are slow-changing / user-toggled). Poll mode is
    #: single-threaded with no receiver to starve, so it keeps the tighter
    #: cadence.
    _BALANCING_POLL_INTERVAL = 6
    _BALANCING_POLL_INTERVAL_STREAM = 24
    #: After this many consecutive reconnect cycles with no recovery, switch from
    #: WARNING to DEBUG to avoid flooding the log during persistent failures.
    _WARN_RECONNECT_THRESHOLD = 3
    #: Max times to defer reconnect on empty reads while keepalive remains healthy.
    _MAX_EMPTY_RECONNECT_DEFERRALS = 3
    #: Poll mode: after this many consecutive empty-read reconnect cycles with no
    #: recovery, escalate to a full REST-client reset + fresh-credential session
    #: rebuild (the poll-mode counterpart of stream mode's stall reset). Without
    #: this, poll-mode reconnects only reuse the cached 10-min credentials and a
    #: dead relay binding can wedge indefinitely until an HA restart (#41).
    _POLL_STALL_RESET_RECONNECTS = 3
    #: Poll mode: after this many consecutive reads where the relay answered but
    #: nothing could be decrypted (stale-`chat_secret` signature), force a fresh
    #: re-login + session rebuild.
    _UNDECRYPTABLE_RESET_STREAK = 6

    #: After this many consecutive full-reset escalations (stream_stall_reset or
    #: poll_stall_reset) without a single successful read in between, the
    #: persistent-session model has failed to recover on this network. Latch
    #: into the beta9-style legacy read model (fresh socket + handshake + one
    #: 0x30 read per poll), which both #41 and #47 reporters confirm is stable.
    _LEGACY_FALLBACK_RESETS = 3

    async def _async_final_probe_before_reconnect(self) -> dict[str, Any] | None:
        """Perform one last direct power-flow read before tearing down session.

        Some relays occasionally miss one poll burst and immediately recover on
        the next request. A final probe reduces unnecessary reconnect churn.
        """
        self.stats_reconnect_probes += 1
        try:
            data = await self.hass.async_add_executor_job(self._read_power_flow)
            _LOGGER.debug("[E2E diag] final-probe %s", self._e2e_diag)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("E2E final probe before reconnect failed: %s", err)
            return None
        if data is None:
            return None
        invalid_channels = get_invalid_realtime_power_channels(data)
        if invalid_channels:
            _LOGGER.warning(
                "E2E final probe rejected by sanity filter (channels=%s, abs_max_w=%d)",
                ",".join(invalid_channels),
                REALTIME_POWER_ABS_MAX_W,
            )
            return None
        self.stats_reconnects_avoided += 1
        _LOGGER.info(
            "E2E final probe succeeded after empty reads; keeping session "
            "(reconnects avoided: %d)",
            self.stats_reconnects_avoided,
        )
        return data

    def _keep_last_or_fail(self) -> dict[str, Any] | None:
        """Return last-known data, or raise UpdateFailed if none ever arrived.

        Keeping the previous values visible is correct once the session has
        produced at least one good read — a transient gap must not blank the
        sensors. But when NO successful read has ever happened and the failures
        have passed the tolerance threshold, returning ``None`` silently marks
        the coordinator update as successful, so HA reports the integration as
        healthy while every entity is unavailable (#47, and the method's own
        docstring already promises to surface this). Raise instead so HA shows
        the real state and keeps retrying.
        """
        if (
            self.data is None
            and not self._successful_first_refresh
            and max(self._empty_reads, self._consecutive_read_errors)
            >= self._MAX_EMPTY_READS
        ):
            raise UpdateFailed(
                "No realtime E2E data received yet — the device's power-flow "
                "frames are not reaching Home Assistant"
            )
        return self.data

    def _register_stall_reset_and_maybe_fallback(self) -> None:
        """Count a full-reset escalation; latch legacy fallback if they persist.

        Both ``stream_stall_reset`` and ``poll_stall_reset`` call this after a
        full REST-client reset + session rebuild. When the persistent-session
        model has been fully reset ``_LEGACY_FALLBACK_RESETS`` times with no
        successful read in between, it cannot recover on this network. Latch
        into the beta9-style one-shot read model (fresh socket + handshake +
        single 0x30 read per poll), which both #41 and #47 reporters confirm is
        stable where the persistent/stream session yields no usable frames. The
        counter resets to 0 on any successful read.
        """
        if self._legacy_fallback_active:
            return
        self._stall_resets_without_success += 1
        if self._stall_resets_without_success >= self._LEGACY_FALLBACK_RESETS:
            self._legacy_fallback_active = True
            _LOGGER.warning(
                "E2E persistent session failed to recover after %d full resets "
                "with no successful read — switching to legacy compatibility "
                "reads (beta9 model: fresh handshake + single read per poll). "
                "This traverses networks where the persistent/stream session "
                "cannot; it stays active until the integration is reloaded "
                "(#41, #47).",
                self._stall_resets_without_success,
            )

    def _read_power_flow_legacy(self) -> dict | None:
        """beta9-style one-shot read: fresh socket + handshake + single 0x30.

        Runs in the executor. Uses the shared cached E2E credentials; on a
        ``None`` result it retries once with a forced fresh login (rotating
        home + device secrets), mirroring beta9's fresh-login-after-failure
        without hammering the REST API on every poll.
        """
        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")

        creds = client.get_e2e_credentials(home_id, device_id, model)
        data = _standalone_read_power_flow(
            creds, log=lambda msg: _LOGGER.debug("[E2E legacy] %s", msg)
        )
        if data is None:
            creds = client.get_e2e_credentials(
                home_id, device_id, model, force_refresh=True
            )
            data = _standalone_read_power_flow(
                creds, log=lambda msg: _LOGGER.debug("[E2E legacy] %s", msg)
            )
        self._e2e_diag = f"legacy result={'data' if data else 'None'}"
        return data

    async def _async_update_data(self) -> dict[str, Any] | None:
        """Fetch realtime power flow via the persistent E2E session.

        Single empty reads are tolerated (the previous value is kept in
        ``self.data``). After ``_MAX_EMPTY_READS`` consecutive failures the
        session is torn down and the coordinator raises UpdateFailed so HA
        surfaces the issue.
        """
        import time as _time
        # Finalise the PREVIOUS poll's outcome into the rolling window now that
        # it is settled (it succeeded iff stats_successful_polls advanced since
        # we last looked). One-poll lag is fine for the rolling rate.
        if self.stats_total_polls > 0:
            self._recent_poll_outcomes.append(
                self.stats_successful_polls > self._recent_window_prev_success
            )
        self._recent_window_prev_success = self.stats_successful_polls
        self.stats_total_polls += 1
        self._maybe_update_stall_snapshot()
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "EMALDO_DEBUG[realtime_poll_start] poll #%d first_successful_refresh=%s "
                "data_is_none=%s last_update_success=%s",
                self.stats_total_polls,
                self._successful_first_refresh,
                self.data is None,
                self.last_update_success,
            )

        try:
            data = await self.hass.async_add_executor_job(self._read_power_flow)
            _LOGGER.debug("[E2E diag] %s", self._e2e_diag)
        except EmaldoAuthError as err:
            # Token expired — force REST re-login and E2E reconnect.
            # This is self-healing (next poll re-logins automatically), so log at INFO.
            self._parent._reset_client()  # noqa: SLF001
            await self._close_session()
            self._empty_reads = 0
            self._consecutive_reconnects += 1
            self.stats_last_failure = _time.time()
            self._record_reconnect("auth_expired", self.stats_last_failure)
            _LOGGER.info("E2E auth expired, will re-login on next poll: %s", err)
            return self._keep_last_or_fail()  # keep last known values visible
        except Exception as err:
            await self._close_session()
            self._empty_reads = 0
            self._consecutive_read_errors += 1
            self.stats_last_failure = _time.time()
            self.stats_last_read_error = f"{type(err).__name__}: {err}"
            reconnect_reason = (
                "keepalive_session_expired_21204"
                if isinstance(err, EmaldoE2ESessionExpired)
                else "read_error"
            )
            self._record_reconnect(reconnect_reason, self.stats_last_failure)
            if self._consecutive_read_errors >= self._MAX_EMPTY_READS:
                _LOGGER.warning(
                    "E2E power flow read failed %d times consecutively: %s",
                    self._consecutive_read_errors, err,
                )
            else:
                _LOGGER.debug(
                    "E2E power flow read failed (attempt %d/%d): %s",
                    self._consecutive_read_errors, self._MAX_EMPTY_READS, err,
                )
            return self._keep_last_or_fail()  # keep last known values visible

        # Ensure keepalive task is running. Created as a config-entry
        # background task (not hass.async_create_task) so HA's bootstrap does
        # not wait for this long-running loop during the "Wrapping up" phase,
        # which would otherwise delay "Home Assistant has started".
        # In stream mode the persistent session's background receiver thread
        # owns the keepalive cadence, so the asyncio loop is not needed.
        if self._is_primary and not self._stream_mode and (
            self._keepalive_task is None or self._keepalive_task.done()
        ):
            self._keepalive_task = self._entry.async_create_background_task(
                self.hass, self._keepalive_loop(), name=f"{DOMAIN}_keepalive"
            )

        if data is None:
            self._empty_reads += 1
            self.stats_empty_reads += 1
            self.stats_last_failure = _time.time()
            if self._stream_mode:
                # The background stream thread owns all recovery (adaptive
                # resubscribe, 21204 reconnect, long-stall watchdog). Tearing
                # the session down here would force a fresh e2e_login +
                # handshake and re-incur the device's slow stream startup,
                # amplifying a brief frame gap into a 20-30 s outage. Keep the
                # last values visible and let the thread self-heal in place.
                #
                # Escalation (beta13g): the in-thread reconnect refreshes creds
                # via the shared client. If a cloud outage (api.emaldo.com is
                # flaky ~01:00-02:00) leaves the shared REST token dead, every
                # re-handshake keeps using stale creds and the stream wedges
                # indefinitely (long_stall storm, frames frozen, 0% until an HA
                # restart). After a prolonged stall, do the full recovery the
                # stream path otherwise never reaches: reset the REST client
                # (clean re-login) and rebuild the session from scratch.
                if self._empty_reads >= self._STREAM_STALL_RESET_POLLS:
                    _LOGGER.warning(
                        "E2E stream wedged: no fresh frame for %d consecutive "
                        "polls (~%ds) — in-place reconnect not recovering; "
                        "resetting REST client and rebuilding session "
                        "(stream_diag=%s)",
                        self._empty_reads,
                        self._empty_reads * REALTIME_SCAN_INTERVAL,
                        self._stream_diag,
                    )
                    self._parent._reset_client()  # noqa: SLF001 - clean re-login
                    self._needs_fresh_creds = True
                    await self._close_session()  # full rebuild on next poll
                    self._empty_reads = 0
                    self._consecutive_reconnects += 1
                    self._consecutive_stream_stall_resets += 1
                    self._record_reconnect("stream_stall_reset", _time.time())
                    self._register_stall_reset_and_maybe_fallback()
                    if (
                        self._consecutive_stream_stall_resets
                        >= self._STREAM_STALL_POLL_HINT_RESETS
                        and not self._stream_poll_mode_hint_logged
                    ):
                        self._stream_poll_mode_hint_logged = True
                        # Classify the stall from the stream diagnostics rather
                        # than assuming a NAT/firewall drop. drain_packets counts
                        # datagrams the receiver actually pulled off the socket;
                        # drain_unparsed counts those it could not decrypt/parse.
                        # If packets ARE arriving but cannot be decoded, this is
                        # a credential/decryption problem (poll mode will not
                        # help) — not the no-traffic NAT case (#41, #47).
                        _sd = self._stream_diag or {}
                        _pkts = int(_sd.get("drain_packets", 0) or 0)
                        _unparsed = int(_sd.get("drain_unparsed", 0) or 0)
                        if _pkts > 0 and _unparsed >= max(1, _pkts - 1):
                            _LOGGER.warning(
                                "E2E realtime stream has stalled and been "
                                "force-reset %d times without recovering. The "
                                "relay IS delivering packets (%d seen, %d "
                                "undecryptable) but none can be decrypted — this "
                                "is a credential/decryption failure, not a "
                                "network drop, so switching to poll mode may not "
                                "help. Please report this log on the issue "
                                "tracker (#41, #47).",
                                self._consecutive_stream_stall_resets,
                                _pkts,
                                _unparsed,
                            )
                        else:
                            _LOGGER.warning(
                                "E2E realtime stream has stalled and been force-reset %d "
                                "times without recovering — the device's push frames are "
                                "not reaching Home Assistant (typically a restrictive "
                                "NAT/firewall/CGNAT network). This cannot be fixed by "
                                "reconnecting. Turn OFF 'Realtime stream mode' under "
                                "Settings → Devices & services → Emaldo Battery → Configure "
                                "to use the poll model, which traverses NAT reliably (#41, "
                                "#47).",
                                self._consecutive_stream_stall_resets,
                            )
                    return self._keep_last_or_fail()
                _LOGGER.debug(
                    "E2E stream: no fresh frame (empty #%d) — keeping last "
                    "values, stream thread self-heals",
                    self._empty_reads,
                )
                return self._keep_last_or_fail()
            # Poll mode only past this point (stream mode returned above).
            # Classify the failure: did the relay stay silent (a true empty
            # read), or answer with something we could not decrypt/parse? The
            # latter is the stale-`chat_secret` signature and warrants a forced
            # re-login rather than an endless reuse of the cached credentials.
            _pf_diag = self.stats_powerflow_last_diag or {}
            received_but_unparsed = bool(
                _pf_diag.get("initial_timeout", 0) == 0
                and _pf_diag.get("initial_session_expired", 0) == 0
                and _pf_diag.get("drain_session_expired", 0) == 0
                and _pf_diag.get("drain_powerflow_hits", 0) == 0
                and (
                    _pf_diag.get("initial_nonmatching", 0)
                    or _pf_diag.get("drain_packets_seen", 0)
                )
            )
            if received_but_unparsed:
                self._undecryptable_streak += 1
                self.stats_undecryptable_polls += 1
                if self._undecryptable_streak == self._UNDECRYPTABLE_RESET_STREAK:
                    _LOGGER.warning(
                        "E2E power flow: relay responded but no frame was "
                        "decryptable for %d consecutive polls — credentials "
                        "likely stale; will force a fresh re-login "
                        "(powerflow_diag=%s)",
                        self._undecryptable_streak,
                        _pf_diag,
                    )
            else:
                self._undecryptable_streak = 0
            if self._empty_reads >= self._MAX_EMPTY_READS:
                keepalive_recently_ok = (
                    self._last_keepalive_success is not None
                    and (self.stats_last_failure - self._last_keepalive_success)
                    <= (KEEPALIVE_INTERVAL * 2.5)
                )
                if (
                    keepalive_recently_ok
                    and self._consecutive_read_errors == 0
                    and self._empty_reconnect_deferral_streak
                    < self._MAX_EMPTY_RECONNECT_DEFERRALS
                ):
                    self._empty_reconnect_deferral_streak += 1
                    self.stats_empty_reconnect_deferrals_healthy_keepalive += 1
                    _LOGGER.info(
                        "E2E empty reads reached reconnect threshold, but keepalive is healthy "
                        "(%d/%d deferrals) — deferring reconnect",
                        self._empty_reconnect_deferral_streak,
                        self._MAX_EMPTY_RECONNECT_DEFERRALS,
                    )
                    return self._keep_last_or_fail()
                if (probe_data := await self._async_final_probe_before_reconnect()) is not None:
                    data = probe_data
                    self._empty_reads = 0
                    self._empty_reconnect_deferral_streak = 0
                    self._undecryptable_streak = 0
                else:
                    self._consecutive_reconnects += 1
                    self._episode_drops += 1
                    self._empty_reconnect_deferral_streak = 0
                    # Escalation (poll-mode counterpart of stream_stall_reset):
                    # after repeated reconnect cycles that never recover, or a
                    # run of undecryptable responses, a plain session teardown
                    # keeps reusing the cached credentials / dead relay binding.
                    # Reset the REST client and force fresh credentials so the
                    # next rebuild re-registers from scratch (#41). Fire only on
                    # the threshold and then periodically to avoid hammering the
                    # cloud API every poll during a sustained outage.
                    full_reset = (
                        (
                            self._consecutive_reconnects
                            >= self._POLL_STALL_RESET_RECONNECTS
                            and self._consecutive_reconnects
                            % self._POLL_STALL_RESET_RECONNECTS
                            == 0
                        )
                        or self._undecryptable_streak
                        >= self._UNDECRYPTABLE_RESET_STREAK
                    )
                    if full_reset:
                        _LOGGER.warning(
                            "E2E poll stall: %d reconnect cycles without recovery "
                            "(undecryptable_streak=%d) — resetting REST client and "
                            "rebuilding session with fresh credentials "
                            "(powerflow_diag=%s)",
                            self._consecutive_reconnects,
                            self._undecryptable_streak,
                            self.stats_powerflow_last_diag,
                        )
                        self._parent._reset_client()  # noqa: SLF001 - clean re-login
                        self._needs_fresh_creds = True
                        self._undecryptable_streak = 0
                        self._record_reconnect("poll_stall_reset", _time.time())
                        self._register_stall_reset_and_maybe_fallback()
                        await self._close_session()
                        self._empty_reads = 0
                        return self._keep_last_or_fail()  # keep last known values visible
                    self._record_reconnect("empty_reads", _time.time())
                    if self._consecutive_reconnects <= self._WARN_RECONNECT_THRESHOLD:
                        # Session expiry is normal relay behaviour — always INFO unless persistent
                        _LOGGER.info(
                            "E2E power flow: %d consecutive empty reads, reconnecting "
                            "(episode drops: %d, reconnects: %d since start)",
                            self._empty_reads,
                            self._episode_drops,
                            self.stats_reconnects,
                        )
                    elif self._consecutive_reconnects == self._WARN_RECONNECT_THRESHOLD + 1:
                        _LOGGER.warning(
                            "E2E power flow: persistent connection failure after %d reconnect "
                            "cycles — suppressing further reconnect warnings (episode drops: %d). "
                            "Check device/relay connectivity.",
                            self._consecutive_reconnects - 1,
                            self._episode_drops,
                        )
                    else:
                        _LOGGER.debug(
                            "E2E power flow: empty reads, reconnecting "
                            "(episode drops: %d, reconnects: %d since start)",
                            self._episode_drops,
                            self.stats_reconnects,
                        )
                    await self._close_session()
                    self._empty_reads = 0
                    return self._keep_last_or_fail()  # keep last known values visible
                # fall through and process recovered probe_data as a normal success
            # Keep previous data visible to sensors
            _LOGGER.info(
                "E2E power flow empty read %d/%d, keeping previous values",
                self._empty_reads, self._MAX_EMPTY_READS,
            )
            return self._keep_last_or_fail()

        invalid_channels = get_invalid_realtime_power_channels(data)
        if invalid_channels:
            self._empty_reads += 1
            self.stats_last_failure = _time.time()
            _LOGGER.warning(
                "E2E power flow rejected by sanity filter (channels=%s, abs_max_w=%d) "
                "— keeping previous values",
                ",".join(invalid_channels),
                REALTIME_POWER_ABS_MAX_W,
            )
            if not self._stream_mode and self._empty_reads >= self._MAX_EMPTY_READS:
                self._consecutive_reconnects += 1
                self._record_reconnect("invalid_payload", _time.time())
                self._empty_reconnect_deferral_streak = 0
                await self._close_session()
                self._empty_reads = 0
            return self._keep_last_or_fail()

        self._empty_reads = 0
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _pf = {k: data.get(k) for k in ("battery_w", "solar_w", "grid_w", "soc") if k in data}
            _LOGGER.debug("[PowerFlow] %s", _pf)
        if self._consecutive_reconnects > 0:
            _LOGGER.info(
                "E2E power flow recovered after %d reconnect cycles (episode drops: %d)",
                self._consecutive_reconnects,
                self._episode_drops,
            )
        self._consecutive_reconnects = 0
        # A clean read ends the current failure episode: reset the per-episode
        # drop tally so warning messages report only the next outage's drops.
        # stats_empty_reads stays monotonic for the diagnostic sensor.
        self._episode_drops = 0
        if self._consecutive_read_errors >= self._MAX_EMPTY_READS:
            _LOGGER.info(
                "E2E power flow recovered after %d consecutive errors",
                self._consecutive_read_errors,
            )
        self._consecutive_read_errors = 0
        self._empty_reconnect_deferral_streak = 0
        self._undecryptable_streak = 0
        self._consecutive_stream_stall_resets = 0
        # A successful read means the current transport is working, so clear the
        # legacy-fallback escalation counter (Fix E). Note: once
        # ``_legacy_fallback_active`` latches it stays until reload — a working
        # legacy read must not silently flip back to the persistent model.
        self._stall_resets_without_success = 0
        self.stats_successful_polls += 1
        self.stats_last_success = _time.time()
        was_first = not self._successful_first_refresh
        self._successful_first_refresh = True
        if was_first:
            _LOGGER.info(
                "EMALDO_DEBUG[first_successful_refresh] realtime coordinator got its "
                "first valid E2E read — entities will now start writing state"
            )

        # Poll balancing state periodically using the same session. This avoids
        # opening a competing UDP socket from the slow coordinator. The cadence
        # is looser in stream mode to reduce contention with the background
        # stream receiver (see _BALANCING_POLL_INTERVAL* for the rationale).
        self._balancing_poll_counter += 1
        _balancing_interval = (
            self._BALANCING_POLL_INTERVAL_STREAM
            if self._stream_mode
            else self._BALANCING_POLL_INTERVAL
        )
        if self._balancing_poll_counter >= _balancing_interval:
            self._balancing_poll_counter = 0
            try:
                rf = await self.hass.async_add_executor_job(
                    self._session.read_regulate_frequency_state
                )
                # APK analysis (zd/j.java class y): device always responds
                # with the actual state (0=Idle, 1=OnHold, 2+=active).
                # None means the query itself failed (session/timeout), so
                # keep the last known value rather than falsely reporting idle.
                if rf is not None:
                    self._regulate_frequency = rf
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Regulate frequency state read failed: %s", err)
                # Keep the last known value so the sensor doesn't flicker to unknown
                # on a transient failure.

            # Poll virtual power plant (sell-back to grid) state alongside balancing (~60s).
            try:
                vpp = await self.hass.async_add_executor_job(
                    self._read_virtualpowerplant
                )
                if vpp is not None:
                    data["sell_back_to_grid_on"] = vpp["sell_back_to_grid_on"]
                elif self.data and "sell_back_to_grid_on" in self.data:
                    data["sell_back_to_grid_on"] = self.data["sell_back_to_grid_on"]
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Virtual power plant state read failed: %s", err)
                if self.data and "sell_back_to_grid_on" in self.data:
                    data["sell_back_to_grid_on"] = self.data["sell_back_to_grid_on"]

            # Poll sell-limit (selling protection) state alongside balancing (~60s).
            try:
                sl = await self.hass.async_add_executor_job(self._read_sell_limit)
                if sl is not None:
                    data["sell_limit_on"] = sl["selling_protection_on"]
                    data["sell_limit_threshold"] = sl["threshold_kwh"]
                elif self.data:
                    for _k in ("sell_limit_on", "sell_limit_threshold"):
                        if _k in self.data:
                            data[_k] = self.data[_k]
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Sell limit state read failed: %s", err)
                if self.data:
                    for _k in ("sell_limit_on", "sell_limit_threshold"):
                        if _k in self.data:
                            data[_k] = self.data[_k]

            # Poll manual selling state alongside balancing (~60s).
            # ``manual_selling_intended_target`` holds the user's pending target
            # set via the NumberEntity. Polling must never overwrite it with the
            # firmware-reported value before a selling session actually starts,
            # otherwise the requested amount is silently replaced (#42).
            _MS_KEYS = (
                "manual_selling_on",
                "manual_selling_target_kwh",
                "manual_selling_sold_kwh",
                "manual_selling_intended_target",
            )
            try:
                ms = await self.hass.async_add_executor_job(self._read_manual_selling)
                if ms is not None:
                    data["manual_selling_on"] = ms["enabled"]
                    data["manual_selling_target_kwh"] = ms["target_energy_kwh"]
                    data["manual_selling_sold_kwh"] = ms["sold_so_far_kwh"]
                    # Preserve the pending intended target until a session is
                    # active; once selling starts the firmware value is
                    # authoritative and the intent is dropped.
                    intended = (self.data or {}).get("manual_selling_intended_target")
                    if intended is not None and not ms["enabled"]:
                        data["manual_selling_intended_target"] = intended
                elif self.data:
                    for _k in _MS_KEYS:
                        if _k in self.data:
                            data[_k] = self.data[_k]
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Manual selling state read failed: %s", err)
                if self.data:
                    for _k in _MS_KEYS:
                        if _k in self.data:
                            data[_k] = self.data[_k]
        elif self.data and "sell_back_to_grid_on" in self.data:
            data["sell_back_to_grid_on"] = self.data["sell_back_to_grid_on"]
            for _k in ("sell_limit_on", "sell_limit_threshold"):
                if _k in self.data:
                    data[_k] = self.data[_k]
            for _k in (
                "manual_selling_on",
                "manual_selling_target_kwh",
                "manual_selling_sold_kwh",
                "manual_selling_intended_target",
            ):
                if _k in self.data:
                    data[_k] = self.data[_k]

        # Poll per-module battery info every 60 successful reads (~10 min).
        # Runs on a dedicated one-shot E2E session, NOT the persistent realtime
        # socket: the 0x06 scan probes up to 13 cabinet slots and would hold
        # the realtime session lock for tens of seconds, starving the keepalive
        # task and letting the relay drop the realtime session (#37).
        #
        # The scan is dispatched as a BACKGROUND task rather than awaited here,
        # because a cold-start scan can take ~50 s and would otherwise delay
        # the first power-flow reading from reaching sensors (the coordinator
        # cannot notify listeners until `_async_update_data` returns). The scan
        # updates `_battery_modules`/`_battery_module_slots` and notifies
        # listeners itself once it finishes.
        self._battery_modules_poll_counter += 1
        if self._battery_modules_poll_counter >= 60:
            self._battery_modules_poll_counter = 0
            # Don't start a new scan while one is already running.
            if self._battery_scan_task is None or self._battery_scan_task.done():
                # Background task (not hass.async_create_task) so a cold-start
                # scan never blocks HA bootstrap's "Wrapping up" phase.
                self._battery_scan_task = self._entry.async_create_background_task(
                    self.hass,
                    self._async_scan_battery_modules(),
                    name=f"{DOMAIN}_battery_scan",
                )

        data["battery_modules"] = self._battery_modules
        data["battery_module_slots"] = self._battery_module_slots

        return data

    async def _async_scan_battery_modules(self) -> None:
        """Run the 0x06 battery-module scan off the realtime poll path.

        Updates ``_battery_modules`` / ``_battery_module_slots`` and notifies
        listeners so the per-slot battery sensors pick up new data without
        blocking a realtime power-flow refresh.
        """
        try:
            cached_count = len(self._battery_modules)
            _LOGGER.debug(
                "Battery module info poll starting: device_id=%s model=%s cached_modules=%d",
                self.device_id,
                self.device_model,
                cached_count,
            )
            modules = await self.hass.async_add_executor_job(
                self._read_battery_info_standalone
            )
            returned_count = len(modules or [])
            returned_serials = [m.get("serial") for m in modules or []]
            _LOGGER.debug(
                "Battery module info poll returned %d modules: serials=%s",
                returned_count,
                returned_serials,
            )
            if modules:
                self._battery_modules = modules
                # Map each responding module to its physical scan slot so
                # sensors stay tied to cabinet positions, not to serials or
                # response order (#23).
                for m in modules:
                    slot = m.get("scan_index")
                    if slot is not None:
                        self._battery_module_slots[slot] = m
                # Propagate to the coordinator's current data dict so the
                # immediate listener notification below reflects the new
                # modules (otherwise per-slot sensors would lag one poll).
                if isinstance(self.data, dict):
                    self.data["battery_modules"] = self._battery_modules
                    self.data["battery_module_slots"] = self._battery_module_slots
            else:
                _LOGGER.debug(
                    "Battery module info poll returned no modules; retaining cached_modules=%d",
                    cached_count,
                )
            # Notify listeners so battery-module sensors reflect the new data.
            self.async_update_listeners()
        except EmaldoConnectionError as err:
            # Expected transient cloud/API failure path; keep cached modules.
            _LOGGER.debug(
                "Battery module info poll skipped due to transient connection error; "
                "retaining cached modules: %s",
                err,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Battery module info read failed: %s", err, exc_info=True)

    @property
    def regulate_frequency(self) -> dict | None:
        """Return the last known grid frequency regulation state, or None."""
        return self._regulate_frequency

    async def _close_session(self) -> None:
        """Close the current session (if any).

        For non-primary coordinators this is a no-op — they share the
        primary's session and must not close it.
        """
        if self._session is not None:
            if self._is_primary:
                _LOGGER.debug(
                    "Closing shared E2E session (primary device %s)",
                    self.device_id,
                )
                try:
                    await self.hass.async_add_executor_job(self._session.close)
                except Exception:  # noqa: BLE001
                    pass
                self._pop_shared_session()
            else:
                _LOGGER.debug(
                    "Not closing E2E session for secondary device %s "
                    "(shared with primary)",
                    self.device_id,
                )
            self._invalidate_session_ref()

    async def _keepalive_loop(self) -> None:
        """Periodically send alive+heartbeat to keep the relay session alive."""
        import time as _time
        fail_count = 0
        try:
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)
                if self._session is None or self._session.closed:
                    return
                try:
                    ok = await self.hass.async_add_executor_job(self._session.keepalive)
                except Exception as err:  # noqa: BLE001
                    _LOGGER.debug("Keepalive error: %s", err)
                    ok = False
                if ok:
                    fail_count = 0
                    self._last_keepalive_success = _time.time()
                else:
                    fail_count += 1
                    self.stats_keepalive_failures += 1
                    reason = "other"
                    if self._session is not None:
                        reason = self._session.last_keepalive_failure_reason or "other"
                    if reason == "session_expired_21204":
                        self.stats_keepalive_failures_session_expired += 1
                    elif reason == "closed":
                        self.stats_keepalive_failures_closed += 1
                    elif reason == "exception":
                        self.stats_keepalive_failures_exception += 1
                    elif reason == "response_timeout":
                        self.stats_keepalive_failures_response_timeout += 1
                    else:
                        self.stats_keepalive_failures_other += 1
                    _LOGGER.info(
                        "Keepalive fail #%d (total keepalive failures: %d, reason=%s)",
                        fail_count,
                        self.stats_keepalive_failures,
                        reason,
                    )
                    if fail_count >= 2:
                        # Two consecutive keepalive failures just mean the
                        # relay session has expired; closing and letting the
                        # next poll re-handshake is the designed recovery path.
                        _LOGGER.info(
                            "Keepalive failed twice, closing session for reconnect"
                        )
                        reconnect_reason = (
                            f"keepalive_{reason}"
                            if reason
                            in {
                                "session_expired_21204",
                                "closed",
                                "exception",
                                "response_timeout",
                            }
                            else "keepalive_other"
                        )
                        self._record_reconnect(reconnect_reason, _time.time())
                        await self._close_session()
                        return
        except asyncio.CancelledError:
            pass
