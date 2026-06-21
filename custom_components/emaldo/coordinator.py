"""DataUpdateCoordinators for Emaldo.

Two coordinators:
* :class:`EmaldoCoordinator` — slow REST + battery details (60s interval)
* :class:`EmaldoRealtimeCoordinator` — fast E2E power flow via a persistent
  UDP session (10s interval).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
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
                raise UpdateFailed(f"Connection error: {err}") from err
            except Exception as err:
                raise UpdateFailed(f"Error fetching Emaldo data: {err}") from err

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
        creds = client.e2e_login(self.home_id, self._device_id, self._model)

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
        creds = client.e2e_login(self.home_id, self._device_id, self._model)
        if mode in (1, 2, 3):
            return set_ev_charging_mode_smart(creds, mode)
        if mode in (EV_MODE_INSTANT_FULL, EV_MODE_INSTANT_FIXED):
            return set_ev_charging_mode_instant(creds, mode, fixed_kwh=fixed_kwh)
        raise ValueError(f"Unknown EV mode: {mode}")

    def _write_emergency_charge_on(
        self, start_unix: int | None = None, end_unix: int | None = None
    ) -> None:
        """Start emergency charge for the given time window via the REST API.

        If *start_unix* is None the device starts immediately (now).
        If *end_unix* is None the E2E library falls back to its own default
        (top-of-current-hour + 48 h).
        """
        import time as _time
        if start_unix is None:
            start_unix = int(_time.time())
        if end_unix is None:
            end_unix = start_unix + 3600  # 1 hour fallback
        for attempt in range(2):
            try:
                client = self._ensure_client()
                client.emergency_charge_window(
                    self.home_id, self._device_id, self._model,
                    start_unix, end_unix,
                )
                self._emergency_charge_active = True
                return
            except EmaldoAuthError:
                self._reset_client()
                if attempt == 1:
                    raise
            except Exception:
                if attempt == 1:
                    raise

    def _write_emergency_charge_off(self) -> None:
        """Cancel active emergency charge via the REST API."""
        for attempt in range(2):
            try:
                client = self._ensure_client()
                client.emergency_charge_off(
                    self.home_id, self._device_id, self._model
                )
                self._emergency_charge_active = False
                return
            except EmaldoAuthError:
                self._reset_client()
                if attempt == 1:
                    raise
            except Exception:
                if attempt == 1:
                    raise


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
    ) -> None:
        """Initialize the realtime coordinator.

        Args:
            hass: Home Assistant instance.
            entry: Config entry.
            parent: The slow :class:`EmaldoCoordinator` — used to share the
                authenticated REST client and device discovery.
        """
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_realtime",
            update_interval=timedelta(seconds=REALTIME_SCAN_INTERVAL),
        )
        self._entry = entry
        self._parent = parent
        self._session: PersistentE2ESession | None = None
        self._session_binding: tuple[str, str, str] | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._empty_reads: int = 0
        self._consecutive_read_errors: int = 0
        self._consecutive_reconnects: int = 0
        self._episode_drops: int = 0
        self._regulate_frequency: dict | None = None
        self._balancing_poll_counter: int = 5  # trigger full read on first successful poll
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
        self.stats_empty_reads: int = 0
        self.stats_reconnects: int = 0
        self.stats_keepalive_failures: int = 0
        self.stats_last_success: float | None = None
        self.stats_last_failure: float | None = None
        self.stats_last_reconnect: float | None = None
        # Diagnostic info collected by executor thread, logged from main thread
        self._e2e_diag: str = "(not yet called)"
        #: Set to ``True`` once the very first successful E2E read completes.
        #: Entities use this to suppress state writes during the initial
        #: handshake/reconnect phase, preventing a flash of "unavailable" over
        #: previously restored sensor values on HA restart.
        self._successful_first_refresh: bool = False

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
            await self.hass.async_add_executor_job(self._session.close)
            self._invalidate_session_ref()

    def _invalidate_session_ref(self) -> None:
        """Drop local references to the active E2E session and binding."""
        self._session = None
        self._session_binding = None

    def _ensure_session(self) -> PersistentE2ESession:
        """Create and connect the persistent E2E session if needed.

        Session scope is device-local: one UDP session per
        ``(home_id, device_id, model)`` binding.
        """
        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")

        binding = (home_id, device_id, model)
        if self._session is not None and not self._session.closed:
            if self._session_binding == binding:
                return self._session
            previous_binding = self._session_binding
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass
            self._invalidate_session_ref()
            _LOGGER.info(
                "Recreating E2E session due to device binding change: %s -> %s",
                previous_binding,
                binding,
            )

        creds = client.e2e_login(home_id, device_id, model)
        _LOGGER.debug("E2E session host: %s", creds.get("host", "(default)"))
        self._session = PersistentE2ESession(
            creds,
            log=lambda msg: _LOGGER.debug("[E2E] %s", msg),
        )
        self._session.connect()
        self._session_binding = binding
        return self._session

    def _read_power_flow(self) -> dict | None:
        """Synchronous helper that runs in the executor."""
        session = self._ensure_session()
        # Collect diagnostic info — will be logged from main thread in _async_update_data
        self._e2e_diag = f"host={session._creds.get('host','?')} has_log={session._log is not None} closed={session.closed}"
        data = session.read_power_flow()
        self._e2e_diag += f" result={'data' if data else 'None'}"
        if data is None and session.closed:
            # Session died mid-read — force recreation on next call
            self._invalidate_session_ref()
        return data

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
        creds = client.e2e_login(home_id, device_id, model)
        return _standalone_read_battery_info(
            creds,
            log=lambda msg: _LOGGER.debug("[E2E battery] %s", msg),
        )

    #: Tolerate this many consecutive empty reads before surfacing unavailable.
    _MAX_EMPTY_READS = 3
    #: After this many consecutive reconnect cycles with no recovery, switch from
    #: WARNING to DEBUG to avoid flooding the log during persistent failures.
    _WARN_RECONNECT_THRESHOLD = 3

    async def _async_update_data(self) -> dict[str, Any] | None:
        """Fetch realtime power flow via the persistent E2E session.

        Single empty reads are tolerated (the previous value is kept in
        ``self.data``). After ``_MAX_EMPTY_READS`` consecutive failures the
        session is torn down and the coordinator raises UpdateFailed so HA
        surfaces the issue.
        """
        import time as _time
        self.stats_total_polls += 1
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
            _LOGGER.info("E2E auth expired, will re-login on next poll: %s", err)
            return self.data  # keep last known values visible
        except Exception as err:
            await self._close_session()
            self._empty_reads = 0
            self._consecutive_read_errors += 1
            self.stats_last_failure = _time.time()
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
            return self.data  # keep last known values visible

        # Ensure keepalive task is running
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = self.hass.async_create_task(
                self._keepalive_loop(), name=f"{DOMAIN}_keepalive"
            )

        if data is None:
            self._empty_reads += 1
            self.stats_empty_reads += 1
            self.stats_last_failure = _time.time()
            if self._empty_reads >= self._MAX_EMPTY_READS:
                self.stats_reconnects += 1
                self._consecutive_reconnects += 1
                self._episode_drops += 1
                self.stats_last_reconnect = _time.time()
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
                return self.data  # keep last known values visible
            # Keep previous data visible to sensors
            _LOGGER.info(
                "E2E power flow empty read %d/%d, keeping previous values",
                self._empty_reads, self._MAX_EMPTY_READS,
            )
            return self.data

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
            if self._empty_reads >= self._MAX_EMPTY_READS:
                self.stats_reconnects += 1
                self._consecutive_reconnects += 1
                self.stats_last_reconnect = _time.time()
                await self._close_session()
                self._empty_reads = 0
            return self.data

        self._empty_reads = 0
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
        self.stats_successful_polls += 1
        self.stats_last_success = _time.time()
        was_first = not self._successful_first_refresh
        self._successful_first_refresh = True
        if was_first:
            _LOGGER.info(
                "EMALDO_DEBUG[first_successful_refresh] realtime coordinator got its "
                "first valid E2E read — entities will now start writing state"
            )

        # Poll balancing state every 6th successful read (~60s) using the same session.
        # This avoids opening a competing UDP socket from the slow coordinator.
        self._balancing_poll_counter += 1
        if self._balancing_poll_counter >= 6:
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
            _MS_KEYS = ("manual_selling_on", "manual_selling_target_kwh", "manual_selling_sold_kwh")
            try:
                ms = await self.hass.async_add_executor_job(self._read_manual_selling)
                if ms is not None:
                    data["manual_selling_on"] = ms["enabled"]
                    data["manual_selling_target_kwh"] = ms["target_energy_kwh"]
                    data["manual_selling_sold_kwh"] = ms["sold_so_far_kwh"]
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
            for _k in ("manual_selling_on", "manual_selling_target_kwh", "manual_selling_sold_kwh"):
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
                self._battery_scan_task = self.hass.async_create_task(
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
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Battery module info read failed: %s", err, exc_info=True)

    @property
    def regulate_frequency(self) -> dict | None:
        """Return the last known grid frequency regulation state, or None."""
        return self._regulate_frequency

    async def _close_session(self) -> None:
        """Close the current session (if any)."""
        if self._session is not None:
            try:
                await self.hass.async_add_executor_job(self._session.close)
            except Exception:  # noqa: BLE001
                pass
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
                else:
                    fail_count += 1
                    self.stats_keepalive_failures += 1
                    _LOGGER.info(
                        "Keepalive fail #%d (total keepalive failures: %d)",
                        fail_count, self.stats_keepalive_failures,
                    )
                    if fail_count >= 2:
                        # Two consecutive keepalive failures just mean the
                        # relay session has expired; closing and letting the
                        # next poll re-handshake is the designed recovery path.
                        _LOGGER.info(
                            "Keepalive failed twice, closing session for reconnect"
                        )
                        await self._close_session()
                        return
        except asyncio.CancelledError:
            pass
