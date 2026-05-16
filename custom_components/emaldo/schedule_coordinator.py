"""Schedule & override coordinator for Emaldo."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_call_later,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .emaldo_lib import EmaldoClient, EmaldoAuthError, EmaldoConnectionError
from .emaldo_lib.const import set_params

from .const import (
    DOMAIN,
    CONF_HOME_ID,
    CONF_APP_ID,
    CONF_APP_SECRET,
    CONF_APP_VERSION,
    DEFAULT_APP_ID,
    DEFAULT_APP_SECRET,
    DEFAULT_APP_VERSION,
    CONF_SCHEDULE_START_HOUR,
    CONF_SCHEDULE_START_MINUTE,
    CONF_SCHEDULE_INTERVAL,
    DEFAULT_SCHEDULE_START_HOUR,
    DEFAULT_SCHEDULE_START_MINUTE,
    DEFAULT_SCHEDULE_INTERVAL,
    EVENT_NEXT_DAY_SCHEDULE_READY,
)

_LOGGER = logging.getLogger(__name__)


class EmaldoScheduleCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinator that polls schedule and override data on a custom time pattern.

    Fetches on startup, at a configured time of day, and then at a repeat
    interval. Fires an event when tomorrow's schedule first appears.
    """

    config_entry: ConfigEntry

    # Exponential backoff: 60s, 120s, 240s, 480s, 960s, capped at 1800s
    _RETRY_BASE_SECONDS = 60
    _RETRY_MAX_SECONDS = 1800

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the schedule coordinator."""
        # We use a very long update_interval as a fallback safety net;
        # actual updates are driven by our custom time tracking.
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_schedule",
            update_interval=timedelta(hours=24),
        )
        self._entry = entry
        self._client: EmaldoClient | None = None
        self._device_id: str | None = None
        self._model: str | None = None
        self._device_name: str | None = None
        self._had_next_day = False
        self._e2e_retry_count = 0
        self._retry_count = 0  # exponential backoff counter
        self._unsub_time: CALLBACK_TYPE | None = None
        self._unsub_interval: CALLBACK_TYPE | None = None
        self._unsub_e2e_retry: CALLBACK_TYPE | None = None
        self._unsub_retry: CALLBACK_TYPE | None = None

    @property
    def home_id(self) -> str:
        return self._entry.data[CONF_HOME_ID]

    @property
    def device_id(self) -> str | None:
        return self._device_id

    @property
    def device_model(self) -> str | None:
        return self._model

    @property
    def device_name(self) -> str | None:
        return self._device_name

    # -- Client management (shared pattern with sensor coordinator) --

    def _ensure_client(self) -> EmaldoClient:
        data = self._entry.data
        app_id = data.get(CONF_APP_ID, DEFAULT_APP_ID)
        app_secret = data.get(CONF_APP_SECRET, DEFAULT_APP_SECRET)
        app_version = data.get(CONF_APP_VERSION, DEFAULT_APP_VERSION)
        set_params(app_id, app_secret, app_version)

        if self._client is None or not self._client.is_authenticated:
            self._client = EmaldoClient(app_version=app_version)
            self._client.login(data[CONF_EMAIL], data[CONF_PASSWORD])

        if self._device_id is None:
            did, model, name = self._client.find_device(self.home_id)
            self._device_id = did
            self._model = model
            self._device_name = name

        return self._client

    # -- Data fetching --

    def _fetch_schedule_data(self) -> dict[str, Any]:
        """Fetch schedule + overrides (runs in executor)."""
        client = self._ensure_client()
        hid, did, model = self.home_id, self._device_id, self._model

        schedule = client.get_schedule(hid, did, model)

        # Override reading via E2E can fail (UDP timeouts etc.) — don't
        # let that block the entire update.
        overrides = None
        try:
            overrides = client.get_overrides(hid, did, model)
        except Exception:
            _LOGGER.warning("Failed to read E2E overrides, skipping", exc_info=True)

        return {"schedule": schedule, "overrides": overrides}

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from Emaldo API.

        On failure, returns previously cached data (if any) so sensors stay
        available, and schedules an exponential-backoff retry. Only raises
        UpdateFailed when there is no prior data at all (first refresh).
        """
        try:
            result = await self.hass.async_add_executor_job(self._fetch_schedule_data)
        except EmaldoAuthError as err:
            self._client = None
            return self._handle_fetch_failure(f"Authentication failed: {err}", err)
        except EmaldoConnectionError as err:
            return self._handle_fetch_failure(f"Connection error: {err}", err)
        except Exception as err:
            return self._handle_fetch_failure(f"Error fetching schedule: {err}", err)

        # Success — reset backoff state
        if self._retry_count > 0:
            _LOGGER.info("Schedule fetch recovered after %d retries", self._retry_count)
        self._retry_count = 0
        self._cancel_retry()

        # Detect next-day schedule appearing
        schedule = result.get("schedule") or {}
        slots = schedule.get("hope_charge_discharges", [])
        has_next_day = len(slots) > 96
        if has_next_day and not self._had_next_day:
            _LOGGER.info("Next-day schedule detected, firing event")
            self.hass.bus.async_fire(
                EVENT_NEXT_DAY_SCHEDULE_READY,
                {"entry_id": self._entry.entry_id},
            )
        self._had_next_day = has_next_day

        # If E2E overrides failed, schedule a retry (up to 3 attempts)
        if result.get("overrides") is None and self._e2e_retry_count < 3:
            self._e2e_retry_count += 1
            delay = 60 * self._e2e_retry_count  # 60s, 120s, 180s
            _LOGGER.info(
                "E2E overrides unavailable, scheduling retry %d/3 in %ds",
                self._e2e_retry_count, delay,
            )
            self._cancel_e2e_retry()
            self._unsub_e2e_retry = async_call_later(
                self.hass, delay, self._e2e_retry_callback
            )
        elif result.get("overrides") is not None:
            self._e2e_retry_count = 0
            self._cancel_e2e_retry()

        return result

    # -- Exponential backoff retry for full schedule failures --

    def _handle_fetch_failure(self, message: str, err: Exception) -> dict[str, Any]:
        """Handle a fetch failure: return stale data if available, else raise."""
        self._schedule_retry()
        if self.data is not None:
            _LOGGER.warning(
                "%s — keeping previous data, retry %d in %ds",
                message, self._retry_count,
                min(self._RETRY_BASE_SECONDS * (2 ** (self._retry_count - 1)),
                    self._RETRY_MAX_SECONDS),
            )
            return self.data
        # No prior data — must raise so first_refresh fails correctly
        raise UpdateFailed(message) from err

    def _schedule_retry(self) -> None:
        """Schedule an exponential-backoff retry (1 min, 2 min, 4 min …)."""
        self._cancel_retry()
        self._retry_count += 1
        delay = min(
            self._RETRY_BASE_SECONDS * (2 ** (self._retry_count - 1)),
            self._RETRY_MAX_SECONDS,
        )
        _LOGGER.debug("Scheduling schedule retry %d in %ds", self._retry_count, delay)
        self._unsub_retry = async_call_later(
            self.hass, delay, self._retry_callback
        )

    @callback
    def _retry_callback(self, _now: datetime) -> None:
        """Fire a coordinator refresh after backoff delay."""
        _LOGGER.debug("Backoff retry %d firing", self._retry_count)
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _cancel_retry(self) -> None:
        """Cancel pending backoff retry."""
        if self._unsub_retry is not None:
            self._unsub_retry()
            self._unsub_retry = None

    # -- E2E retry --

    def _fetch_e2e_only(self) -> dict | None:
        """Try to fetch E2E overrides without touching REST schedule."""
        client = self._ensure_client()
        return client.get_overrides(
            self.home_id, self._device_id, self._model
        )

    @callback
    def _e2e_retry_callback(self, _now: datetime) -> None:
        """Retry E2E fetch only — never risks coordinator failure state."""

        async def _retry_e2e() -> None:
            try:
                overrides = await self.hass.async_add_executor_job(
                    self._fetch_e2e_only
                )
            except Exception:
                _LOGGER.debug(
                    "E2E retry %d failed", self._e2e_retry_count, exc_info=True
                )
                overrides = None

            if overrides is not None and self.data is not None:
                self.data["overrides"] = overrides
                self._e2e_retry_count = 0
                self._cancel_e2e_retry()
                self.async_set_updated_data(self.data)
                _LOGGER.info("E2E retry succeeded, overrides updated")
                return

            # Schedule next retry if still under limit
            if self._e2e_retry_count < 3:
                self._e2e_retry_count += 1
                delay = 60 * self._e2e_retry_count
                _LOGGER.info(
                    "E2E retry %d/3 failed, next attempt in %ds",
                    self._e2e_retry_count, delay,
                )
                self._cancel_e2e_retry()
                self._unsub_e2e_retry = async_call_later(
                    self.hass, delay, self._e2e_retry_callback
                )
            else:
                _LOGGER.warning("E2E retries exhausted, overrides unavailable")

        self.hass.async_create_task(_retry_e2e())

    @callback
    def _cancel_e2e_retry(self) -> None:
        """Cancel pending E2E retry."""
        if self._unsub_e2e_retry is not None:
            self._unsub_e2e_retry()
            self._unsub_e2e_retry = None

    # -- Time-based polling setup --

    @callback
    def async_setup_listeners(self) -> None:
        """Set up the time-of-day and interval listeners."""
        self._cancel_listeners()

        opts = self._entry.options
        start_hour = opts.get(CONF_SCHEDULE_START_HOUR, DEFAULT_SCHEDULE_START_HOUR)
        start_minute = opts.get(
            CONF_SCHEDULE_START_MINUTE, DEFAULT_SCHEDULE_START_MINUTE
        )
        interval_sec = opts.get(CONF_SCHEDULE_INTERVAL, DEFAULT_SCHEDULE_INTERVAL)

        @callback
        def _on_time_trigger(now: datetime) -> None:
            """Refresh when the configured start time is reached."""
            _LOGGER.debug("Schedule start-time trigger fired at %s", now)
            self.hass.async_create_task(self.async_request_refresh())

        @callback
        def _on_interval(now: datetime) -> None:
            """Refresh on the repeat interval."""
            _LOGGER.debug("Schedule interval trigger fired at %s", now)
            self.hass.async_create_task(self.async_request_refresh())

        self._unsub_time = async_track_time_change(
            self.hass, _on_time_trigger, hour=start_hour, minute=start_minute, second=0
        )
        self._unsub_interval = async_track_time_interval(
            self.hass, _on_interval, timedelta(seconds=interval_sec)
        )

    @callback
    def _cancel_listeners(self) -> None:
        """Cancel existing time listeners."""
        if self._unsub_time is not None:
            self._unsub_time()
            self._unsub_time = None
        if self._unsub_interval is not None:
            self._unsub_interval()
            self._unsub_interval = None

    @callback
    def async_shutdown(self) -> None:
        """Cancel listeners on shutdown."""
        self._cancel_listeners()
        self._cancel_e2e_retry()
        self._cancel_retry()
