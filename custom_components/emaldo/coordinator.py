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
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .emaldo_lib import (
    EmaldoClient,
    EmaldoAuthError,
    EmaldoConnectionError,
    PersistentE2ESession,
)
from .emaldo_lib.const import set_params
from .emaldo_lib.e2e import (
    build_subscription_packet,
    decrypt_response,
    generate_nonce,
    _run_session,
    parse_ev_charging_info,
    _EV_TYPE_GET_STATE,
)

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_SCAN_INTERVAL,
    REALTIME_SCAN_INTERVAL,
    KEEPALIVE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)


class EmaldoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Slow coordinator for REST battery/power data (60s)."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self._entry = entry
        self._client: EmaldoClient | None = None
        self._device_id: str | None = None
        self._model: str | None = None
        self._device_name: str | None = None

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

    def _ensure_client(self) -> EmaldoClient:
        """Create and authenticate the client if needed."""
        data = self._entry.data

        # Always set app params before any client operation
        set_params(data[CONF_APP_ID], data[CONF_APP_SECRET], data[CONF_APP_VERSION])

        if self._client is None or not self._client.is_authenticated:
            self._client = EmaldoClient(app_version=data[CONF_APP_VERSION])
            self._client.login(data[CONF_EMAIL], data[CONF_PASSWORD])

        if self._device_id is None:
            did, model, name = self._client.find_device(self.home_id)
            self._device_id = did
            self._model = model
            self._device_name = name

        return self._client

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch battery + power data from the REST API."""
        for attempt in range(2):
            try:
                client = await self.hass.async_add_executor_job(self._ensure_client)
                battery = await self.hass.async_add_executor_job(
                    client.get_battery, self.home_id, self._device_id, self._model
                )
                power = await self.hass.async_add_executor_job(
                    client.get_power, self.home_id, self._device_id, self._model
                )
                break
            except EmaldoAuthError:
                self._client = None
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
        ev = None
        try:
            ev = await self.hass.async_add_executor_job(
                self._read_ev_state
            )
        except Exception as err:
            _LOGGER.debug("EV state fetch failed: %s", err)

        return {
            "battery": battery,
            "power": power,
            "solar": solar,
            "ev": ev,
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


class EmaldoRealtimeCoordinator(DataUpdateCoordinator[dict[str, Any] | None]):
    """Fast coordinator for E2E real-time power flow (10s).

    Uses :class:`PersistentE2ESession` to keep a UDP socket open across polls,
    reducing latency from ~500ms to ~85ms per read. A background task sends
    keepalive messages every 15 seconds to prevent the relay server from
    dropping the session.
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
        self._keepalive_task: asyncio.Task | None = None
        self._empty_reads: int = 0
        self._regulate_frequency: dict | None = None
        self._balancing_poll_counter: int = 0
        # -- Stats for diagnostic sensor --
        self.stats_total_polls: int = 0
        self.stats_successful_polls: int = 0
        self.stats_empty_reads: int = 0
        self.stats_reconnects: int = 0
        self.stats_keepalive_failures: int = 0
        self.stats_last_success: float | None = None
        self.stats_last_failure: float | None = None
        self.stats_last_reconnect: float | None = None

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
        """Cancel keepalive and close the UDP session."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except (asyncio.CancelledError, Exception):
                pass
            self._keepalive_task = None
        if self._session is not None:
            await self.hass.async_add_executor_job(self._session.close)
            self._session = None

    def _ensure_session(self) -> PersistentE2ESession:
        """Create and connect the persistent E2E session if needed."""
        if self._session is not None and not self._session.closed:
            return self._session

        client = self._parent._ensure_client()  # noqa: SLF001 - intended
        home_id = self._parent.home_id
        device_id = self._parent._device_id  # noqa: SLF001
        model = self._parent._model  # noqa: SLF001
        if device_id is None or model is None:
            raise UpdateFailed("Device not yet discovered")

        creds = client.e2e_login(home_id, device_id, model)
        self._session = PersistentE2ESession(creds)
        self._session.connect()
        return self._session

    def _read_power_flow(self) -> dict | None:
        """Synchronous helper that runs in the executor."""
        session = self._ensure_session()
        data = session.read_power_flow()
        if data is None and session.closed:
            # Session died mid-read — force recreation on next call
            self._session = None
        return data

    #: Tolerate this many consecutive empty reads before surfacing unavailable.
    _MAX_EMPTY_READS = 3

    async def _async_update_data(self) -> dict[str, Any] | None:
        """Fetch realtime power flow via the persistent E2E session.

        Single empty reads are tolerated (the previous value is kept in
        ``self.data``). After ``_MAX_EMPTY_READS`` consecutive failures the
        session is torn down and the coordinator raises UpdateFailed so HA
        surfaces the issue.
        """
        import time as _time
        self.stats_total_polls += 1

        try:
            data = await self.hass.async_add_executor_job(self._read_power_flow)
        except EmaldoAuthError as err:
            # Token expired — force REST re-login and E2E reconnect
            self._parent._client = None  # noqa: SLF001
            await self._close_session()
            self._empty_reads = 0
            self.stats_last_failure = _time.time()
            _LOGGER.warning("E2E auth failed, will re-login on next poll: %s", err)
            return self.data  # keep last known values visible
        except Exception as err:
            await self._close_session()
            self._empty_reads = 0
            self.stats_last_failure = _time.time()
            _LOGGER.warning("E2E power flow read failed: %s", err)
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
                self.stats_last_reconnect = _time.time()
                _LOGGER.warning(
                    "E2E power flow: %d consecutive empty reads, reconnecting "
                    "(total drops: %d, reconnects: %d since start)",
                    self._empty_reads,
                    self.stats_empty_reads,
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

        self._empty_reads = 0
        self.stats_successful_polls += 1
        self.stats_last_success = _time.time()

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

        return data

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
            self._session = None

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
                        _LOGGER.warning(
                            "Keepalive failed twice, closing session for reconnect"
                        )
                        await self._close_session()
                        return
        except asyncio.CancelledError:
            pass
