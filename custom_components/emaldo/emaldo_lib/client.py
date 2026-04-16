"""Emaldo API client.

Provides a high-level Python interface to the Emaldo battery system API.
All methods return plain dicts/lists and raise exceptions on errors.

Usage::

    from emaldo import EmaldoClient

    client = EmaldoClient()
    client.login("user@example.com", "password123")
    homes = client.list_homes()
    devices = client.list_devices(homes[0]["home_id"])
    battery = client.get_battery(home_id, device_id, model)
"""

import json
import time
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .const import (
    API_HOST,
    APP_SHORT,
    DEFAULT_E2E_HOST,
    DEFAULT_E2E_PORT,
    DEFAULT_MARKER_HIGH,
    DEFAULT_MARKER_LOW,
    DP_ENDPOINTS,
    DP_HOST,
    SLOT_NO_OVERRIDE,
    get_app_id,
    get_default_app_version,
)
from .crypto import decrypt_response, encrypt_field, make_gmtime
from .exceptions import (
    EmaldoAPIError,
    EmaldoAuthError,
    EmaldoConnectionError,
    EmaldoE2EError,
)
from . import e2e as _e2e


def _short_error(exc: Exception) -> str:
    """Extract a short, human-readable message from a requests exception."""
    # Walk the cause chain to find the innermost message
    cause = exc
    while cause.__cause__:
        cause = cause.__cause__
    msg = str(cause)
    # Extract quoted message like SSLEOFError(8, 'EOF occurred ...')
    if "'" in msg:
        parts = msg.split("'")
        if len(parts) >= 2 and len(parts[1]) > 5:
            return parts[1][:200]
    # For things like "too many 502 error responses", use as-is
    return msg[:200]


class EmaldoClient:
    """Client for the Emaldo battery system API.

    The client maintains session state (token, user info) in memory.
    Use :meth:`export_session` / :meth:`import_session` to persist
    across restarts.

    Args:
        session: Optional previously-exported session dict.
        app_version: App version string to report. Defaults to the
            latest known version. The server may reject requests
            from outdated versions.
    """

    def __init__(
        self,
        session: dict | None = None,
        *,
        app_version: str = None,
    ):
        self._session: dict = session or {}
        self._app_version = app_version if app_version is not None else get_default_app_version()
        self._http = requests.Session()
        # Retry transient connection / SSL errors automatically.
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["POST", "GET"],
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._http.mount("https://", adapter)
        self._http.mount("http://", adapter)
        # Use the same header set as the official app (okhttp/4.9.0).
        self._http.headers.clear()
        self._http.headers.update({
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "okhttp/4.9.0",
            "Accept-Encoding": "gzip",
        })

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def export_session(self) -> dict:
        """Return the current session state as a serialisable dict."""
        return dict(self._session)

    def import_session(self, session: dict) -> None:
        """Restore a previously exported session."""
        self._session = dict(session)

    @property
    def is_authenticated(self) -> bool:
        """Whether we have a valid token."""
        return bool(self._session.get("token"))

    # ------------------------------------------------------------------
    # Low-level API
    # ------------------------------------------------------------------

    def _get_base_url(self, path: str) -> tuple[str, str]:
        """Return ``(base_url, host)`` for a given API path."""
        for prefix in DP_ENDPOINTS:
            if path.startswith(prefix):
                return f"https://{DP_HOST}", DP_HOST
        return f"https://{API_HOST}", API_HOST

    def api_request(
        self,
        path: str,
        json_data: dict | None = None,
        *,
        need_token: bool = True,
    ) -> dict:
        """Make an encrypted API request and return the decrypted result.

        Args:
            path: API endpoint path (e.g. ``/home/list-homes/``).
            json_data: Optional body dict to encrypt.
            need_token: Include the session token (default *True*).

        Returns:
            Full response dict with decrypted ``Result`` field.

        Raises:
            EmaldoAuthError: Session expired or not logged in.
            EmaldoAPIError: API returned a non-success status.
        """
        base, host = self._get_base_url(path)
        url = f"{base}{path}{get_app_id()}"

        form_data: dict[str, str] = {}

        if json_data is not None:
            json_data["gmtime"] = make_gmtime()
            json_str = json.dumps(json_data, separators=(",", ":"))
            form_data["json"] = encrypt_field(json_str)

        if need_token:
            token = self._session.get("token", "")
            if not token:
                raise EmaldoAuthError("Not logged in. Call login() first.")
            token_with_ts = f"{token}_{make_gmtime()}"
            form_data["token"] = encrypt_field(token_with_ts)

        form_data["gm"] = "1"

        headers = {"X-Online-Host": host}
        try:
            resp = self._http.post(url, data=form_data, headers=headers, timeout=30)
        except requests.exceptions.ConnectionError as exc:
            raise EmaldoConnectionError(
                f"Connection failed: {host}{path} — {_short_error(exc)}"
            ) from exc
        except requests.exceptions.Timeout as exc:
            raise EmaldoConnectionError(
                f"Request timed out: {host}{path}"
            ) from exc
        except requests.exceptions.RetryError as exc:
            raise EmaldoConnectionError(
                f"Request failed after retries: {host}{path} — {_short_error(exc)}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise EmaldoConnectionError(
                f"Request failed: {host}{path} — {_short_error(exc)}"
            ) from exc

        if resp.status_code >= 500:
            raise EmaldoConnectionError(
                f"Server error {resp.status_code}: {host}{path}"
            )
        resp.raise_for_status()

        resp_json = resp.json()
        status = resp_json.get("Status", 0)

        if status == -12:
            raise EmaldoAuthError("Session expired. Call login() again.")

        if status != 1:
            error_msg = resp_json.get("ErrorMessage", "Unknown error")
            raise EmaldoAPIError(
                f"API error (status={status}): {error_msg}",
                status=status,
                response=resp_json,
            )

        # Decrypt the Result field
        result_hex = resp_json.get("Result", "")
        if result_hex and isinstance(result_hex, str):
            try:
                decrypted = decrypt_response(result_hex)
                resp_json["Result"] = json.loads(decrypted)
            except Exception as exc:
                resp_json["Result"] = f"[Decryption failed: {exc}]"

        return resp_json

    # ------------------------------------------------------------------
    # Version check
    # ------------------------------------------------------------------

    def check_version(self) -> dict:
        """Check if the current app version is up to date.

        Calls ``/domain/getappversionstate/`` to query the server for
        the latest required version.

        Returns:
            A dict with keys:

            - ``version`` (str): The latest version string (e.g. ``"2.8.3"``).
            - ``must`` (int): ``1`` if the update is mandatory, ``0`` if optional.
            - ``url`` (str): Download URL for the update.
            - ``up_to_date`` (bool): Whether the current ``app_version`` meets
              the requirement.
        """
        result = self.api_request(
            "/domain/getappversionstate/",
            json_data={"short": APP_SHORT},
            need_token=False,
        )
        data = result.get("Result", {})
        # The server wraps version info as a JSON string inside "version"
        version_info: dict = {}
        if isinstance(data, dict):
            raw = data.get("version", "{}")
            if isinstance(raw, str):
                version_info = json.loads(raw)
            elif isinstance(raw, dict):
                version_info = raw

        # Compare versions
        server_version = version_info.get("version", "0.0.0")
        up_to_date = self._compare_versions(self._app_version, server_version)

        return {
            "version": server_version,
            "must": version_info.get("must", 0),
            "url": version_info.get("url", ""),
            "up_to_date": up_to_date,
        }

    @staticmethod
    def _compare_versions(current: str, required: str) -> bool:
        """Return True if *current* >= *required* (simple tuple comparison)."""
        def _parse(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split(".") if x.isdigit())
        return _parse(current) >= _parse(required)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def login(self, identifier: str, password: str, *, use_phone: bool = False) -> dict:
        """Log in with email (or phone) and password.

        Args:
            identifier: Email address or phone number.
            password: Account password.
            use_phone: Treat *identifier* as phone number instead of email.

        Returns:
            Session data dict (user_id, token, etc.).

        Raises:
            EmaldoAuthError: Login failed.
        """
        json_data: dict[str, Any] = {"password": password}
        if use_phone:
            json_data["phone"] = identifier
        else:
            json_data["email"] = identifier

        result = self.api_request("/user/login/", json_data=json_data, need_token=False)
        session_data = result.get("Result", {})
        if not isinstance(session_data, dict) or "token" not in session_data:
            raise EmaldoAuthError(f"Login failed: {result}")

        self._session = {
            "token": session_data["token"],
            "user_id": session_data.get("user_id", ""),
            "email": identifier if not use_phone else "",
            "phone": identifier if use_phone else "",
            "login_time": time.time(),
        }
        for key in ("nickname", "avatar", "uid"):
            if key in session_data:
                self._session[key] = session_data[key]

        return session_data

    # ------------------------------------------------------------------
    # Homes & devices
    # ------------------------------------------------------------------

    def list_homes(self) -> list[dict]:
        """Return a list of homes associated with the account."""
        result = self.api_request("/home/list-homes/")
        data = result.get("Result", {})
        if isinstance(data, dict):
            return data.get("list_homes", [])
        return []

    def list_devices(self, home_id: str) -> list[dict]:
        """Return battery devices in a home.

        Args:
            home_id: Home identifier.
        """
        json_data = {
            "home_id": home_id,
            "models": [],
            "page_size": 30,
            "addtime": 1,
            "order": "asc",
        }
        result = self.api_request("/bmt/list-bmt/", json_data=json_data)
        data = result.get("Result", {})
        if isinstance(data, dict):
            return data.get("bmts", [])
        return []

    def search_device(self, home_id: str, device_id: str, model: str) -> dict:
        """Search for a specific device and return detailed info."""
        json_data = {
            "home_id": home_id,
            "ids": [{"id": device_id, "model": model}],
        }
        result = self.api_request("/bmt/search-bmt/", json_data=json_data)
        return result.get("Result", {})

    def find_home(self) -> tuple[str, str]:
        """Auto-discover the first home that contains devices.

        Returns:
            ``(home_id, home_name)``

        Raises:
            EmaldoAPIError: No homes found.
        """
        homes = self.list_homes()
        if not homes:
            raise EmaldoAPIError("No homes found.")
        for h in homes:
            hid = h["home_id"]
            devices = self.list_devices(hid)
            if devices:
                return hid, h.get("home_name", hid)
        # Fall back to first home
        return homes[0]["home_id"], homes[0].get("home_name", homes[0]["home_id"])

    def find_device(self, home_id: str) -> tuple[str, str, str]:
        """Auto-discover the first battery device in a home.

        Returns:
            ``(device_id, model, name)``

        Raises:
            EmaldoAPIError: No devices found.
        """
        devices = self.list_devices(home_id)
        if not devices:
            raise EmaldoAPIError("No battery devices found.")
        d = devices[0]
        return d["id"], d["model"], d.get("name", d["id"])

    # ------------------------------------------------------------------
    # Battery data
    # ------------------------------------------------------------------

    def get_battery(self, home_id: str, device_id: str, model: str) -> dict:
        """Get battery overview (SoC, capacity, sensor, dual power).

        Returns a dict with keys: ``sensor``, ``power_level``, ``battery``, ``dual_power``.
        """
        base = {"home_id": home_id, "id": device_id, "model": model}

        r_sensor = self.api_request("/bmt/stats/b-sensor/", json_data=dict(base))
        r_level = self.api_request(
            "/bmt/stats/battery/power-level/day/",
            json_data={**base, "offset": 0},
        )
        r_bat = self.api_request(
            "/bmt/stats/battery-v2/day/",
            json_data={**base, "offset": 0},
        )
        r_dual = self.api_request(
            "/bmt/is-dual-power-open/",
            json_data={"home_id": home_id, "bmt_id": device_id},
        )

        return {
            "sensor": r_sensor.get("Result") or {},
            "power_level": r_level.get("Result") or {},
            "battery": r_bat.get("Result") or {},
            "dual_power": r_dual.get("Result") or {},
        }

    def get_usage(
        self, home_id: str, device_id: str, model: str, offset: int = 0
    ) -> dict:
        """Get comprehensive daily usage data.

        Args:
            offset: Day offset. 0 = today, negative = past days
                (-1 = yesterday, -2 = day before, etc.).

        Returns a dict with keys: ``usage``, ``battery``, ``solar``, ``grid``, ``power_level``.
        """
        base = {"home_id": home_id, "id": device_id, "model": model, "offset": offset}

        r_usage = self.api_request("/bmt/stats/load/usage-v2/day/", json_data=dict(base))
        r_bat = self.api_request("/bmt/stats/battery-v2/day/", json_data=dict(base))
        r_solar = self.api_request("/bmt/stats/mppt-v2/day/", json_data=dict(base))
        r_grid = self.api_request(
            "/bmt/stats/grid/day/",
            json_data={**base, "get_real": True, "query_interval": 5},
        )
        r_level = self.api_request(
            "/bmt/stats/battery/power-level/day/", json_data=dict(base)
        )

        return {
            "usage": r_usage.get("Result") or {},
            "battery": r_bat.get("Result") or {},
            "solar": r_solar.get("Result") or {},
            "grid": r_grid.get("Result") or {},
            "power_level": r_level.get("Result") or {},
        }

    def get_revenue(
        self, home_id: str, device_id: str, model: str, offset: int = 0
    ) -> dict:
        """Get daily revenue data.

        Args:
            offset: Day offset. 0 = today, negative = past days
                (-1 = yesterday, -2 = day before, etc.).
        """
        json_data = {
            "home_id": home_id,
            "id": device_id,
            "model": model,
            "offset": offset,
        }
        result = self.api_request("/bmt/stats/revenue-v2/day/", json_data=json_data)
        return result.get("Result") or {}

    def get_fcr(self, home_id: str) -> dict:
        """Get FCR predicted revenue summary."""
        result = self.api_request(
            "/home/get-home-fcr-predict-revenue-summary/",
            json_data={"home_id": home_id},
        )
        return result.get("Result") or {}

    def get_fcr_daily(self, home_id: str) -> dict:
        """Get FCR predicted revenue by day."""
        result = self.api_request(
            "/home/get-home-fcr-predict-revenue-daily/",
            json_data={"home_id": home_id},
        )
        return result.get("Result") or {}

    def get_schedule(self, home_id: str, device_id: str, model: str) -> dict:
        """Get the current charge/discharge schedule.

        Returns a dict with keys including ``hope_charge_discharges`` (list of
        96 or 192 slot values), ``market_prices``, ``forecast_solars``,
        ``smart``, ``emergency``, ``start_time``, ``timezone``, ``gap``.
        """
        json_data = {"home_id": home_id, "id": device_id, "model": model}
        result = self.api_request(
            "/bmt/stats/get-charging-discharging-plans-v2-minute/",
            json_data=json_data,
        )
        return result.get("Result") or {}

    def get_power(self, home_id: str, device_id: str, model: str) -> dict:
        """Get current realtime power readings.

        Returns a dict with keys: ``usage``, ``battery``, ``grid``, ``dual_power``.
        """
        base = {"home_id": home_id, "id": device_id, "model": model, "offset": 0}

        r_usage = self.api_request("/bmt/stats/load/usage-v2/day/", json_data=dict(base))
        r_bat = self.api_request("/bmt/stats/battery-v2/day/", json_data=dict(base))
        r_grid = self.api_request(
            "/bmt/stats/grid/day/",
            json_data={**base, "get_real": True, "query_interval": 5},
        )
        r_dual = self.api_request(
            "/bmt/is-dual-power-open/",
            json_data={"home_id": home_id, "bmt_id": device_id},
        )

        return {
            "usage": r_usage.get("Result") or {},
            "battery": r_bat.get("Result") or {},
            "grid": r_grid.get("Result") or {},
            "dual_power": r_dual.get("Result") or {},
        }

    def get_solar(
        self, home_id: str, device_id: str, model: str, offset: int = 0
    ) -> dict:
        """Get solar/MPPT generation data (5-min intervals, 288 points/day).

        Args:
            offset: Day offset. 0 = today, negative = past days
                (-1 = yesterday, -2 = day before, etc.).
                At least 30 days of history available.
        """
        json_data = {
            "home_id": home_id,
            "id": device_id,
            "model": model,
            "offset": offset,
        }
        result = self.api_request("/bmt/stats/mppt-v2/day/", json_data=json_data)
        return result.get("Result") or {}

    def get_grid(
        self, home_id: str, device_id: str, model: str, offset: int = 0
    ) -> dict:
        """Get grid import/export data (5-min intervals).

        Args:
            offset: Day offset. 0 = today, negative = past days
                (-1 = yesterday, -2 = day before, etc.).
        """
        json_data = {
            "home_id": home_id,
            "id": device_id,
            "model": model,
            "offset": offset,
            "get_real": True,
            "query_interval": 5,
        }
        result = self.api_request("/bmt/stats/grid/day/", json_data=json_data)
        return result.get("Result") or {}

    def get_region(self, home_id: str, device_id: str, model: str) -> dict:
        """Get device region/country info."""
        json_data = {"home_id": home_id, "id": device_id, "model": model}
        result = self.api_request("/bmt/get-region/", json_data=json_data)
        return result.get("Result") or {}

    def get_contract(self, home_id: str) -> dict:
        """Get balance contract info."""
        result = self.api_request(
            "/bmt/get-family-balance-contract-info/",
            json_data={"home_id": home_id},
        )
        return result.get("Result") or {}

    def get_features(self, home_id: str, device_id: str, model: str) -> dict:
        """Get device feature flags."""
        json_data = {"home_id": home_id, "id": device_id, "model": model}
        result = self.api_request("/bmt/get-feature/", json_data=json_data)
        return result.get("Result") or {}

    def get_price_thresholds(self, home_id: str, device_id: str, model: str) -> dict:
        """Get default price percent thresholds."""
        json_data = {"home_id": home_id, "id": device_id, "model": model}
        result = self.api_request("/bmt/get-default-price-percent/", json_data=json_data)
        return result.get("Result") or {}

    def get_strategy(self, home_id: str, device_id: str, model: str) -> dict:
        """Get composite AI strategy info (FCR + schedule + revenue + thresholds).

        Returns a dict with keys: ``fcr_summary``, ``fcr_daily``, ``schedule``,
        ``price_thresholds``, ``revenue``.
        """
        return {
            "fcr_summary": self.get_fcr(home_id),
            "fcr_daily": self.get_fcr_daily(home_id),
            "schedule": self.get_schedule(home_id, device_id, model),
            "price_thresholds": self.get_price_thresholds(home_id, device_id, model),
            "revenue": self.get_revenue(home_id, device_id, model),
        }

    # ------------------------------------------------------------------
    # E2E override protocol
    # ------------------------------------------------------------------

    def e2e_login(self, home_id: str, device_id: str, model: str) -> dict:
        """Perform E2E login to obtain UDP session credentials.

        Calls three API endpoints (home e2e-login, device e2e-user-login,
        search-bmt) and returns a credentials dict for E2E operations.

        Raises:
            EmaldoAuthError: Auth issue during E2E login.
            EmaldoE2EError: Missing fields in API response.
        """
        # Step 1: Home E2E login
        home_result = self.api_request(
            "/home/e2e-login/", json_data={"home_id": home_id}
        )
        home_data = home_result.get("Result", {})
        if not isinstance(home_data, dict) or "end_id" not in home_data:
            raise EmaldoE2EError(f"Home e2e-login failed: {home_result}")

        # Step 2: Device E2E login
        dev_result = self.api_request(
            "/bmt/e2e-user-login/",
            json_data={
                "home_id": home_id,
                "models": [model],
                "page_size": 0,
                "ids": [{"id": device_id, "model": model}],
                "addtime": 0,
            },
        )
        dev_data = dev_result.get("Result", {})
        if not isinstance(dev_data, dict) or "e2es" not in dev_data:
            raise EmaldoE2EError(f"Device e2e-user-login failed: {dev_result}")
        device_e2e = dev_data["e2es"][0]

        # Step 3: Get battery end_id
        search_result = self.api_request(
            "/bmt/search-bmt/",
            json_data={
                "home_id": home_id,
                "ids": [{"id": device_id, "model": model}],
            },
        )
        search_data = search_result.get("Result", {})
        bmts = search_data.get("bmts", []) if isinstance(search_data, dict) else []
        if not bmts or "end_id" not in bmts[0]:
            raise EmaldoE2EError(f"search-bmt missing end_id: {search_result}")
        battery = bmts[0]

        return {
            "sender_end_id": device_e2e["end_id"],
            "sender_group_id": device_e2e["group_id"],
            "chat_secret": device_e2e["chat_secret"],
            "sender_end_secret": device_e2e.get("end_secret", ""),
            "recipient_end_id": battery["end_id"],
            "recipient_group_id": battery["group_id"],
            "home_end_id": home_data["end_id"],
            "home_group_id": home_data["group_id"],
            "home_end_secret": home_data.get("end_secret", ""),
            "home_chat_secret": home_data.get("chat_secret", ""),
            "host": device_e2e.get("host", f"{DEFAULT_E2E_HOST}:{DEFAULT_E2E_PORT}"),
        }

    def get_overrides(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        log: Callable[..., None] | None = None,
    ) -> dict | None:
        """Read current E2E override state.

        Returns a dict with ``slots`` (96 ints), ``high_marker``,
        and ``low_marker``; or *None* if reading fails.
        """
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.read_overrides(creds, log=log)

    def get_battery_info(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        log: Callable[..., None] | None = None,
    ) -> list[dict]:
        """Read battery cell info via E2E (type 0x06).

        Returns a list of dicts, one per battery cell.
        """
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.read_battery_info(creds, log=log)

    def get_power_flow(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        log: Callable[..., None] | None = None,
    ) -> dict | None:
        """Read realtime power flow via E2E (type 0x30)."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.read_power_flow(creds, log=log)

    def set_override(
        self,
        home_id: str,
        device_id: str,
        model: str,
        slot_values: bytes,
        *,
        high_marker: int = DEFAULT_MARKER_HIGH,
        low_marker: int = DEFAULT_MARKER_LOW,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Send override values to the device.

        The device override function is day-scoped: only 96 slots (today)
        are meaningful.  Tomorrow's schedule must be pushed fresh after
        midnight.

        Args:
            home_id: Home identifier.
            device_id: Device identifier.
            model: Device model string.
            slot_values: 96 bytes of override values.
            high_marker: High battery marker percentage.
            low_marker: Low battery marker percentage.
            log: Optional log callback ``log(message: str)``.

        Returns:
            *True* if the server acknowledged the override.
        """
        if len(slot_values) not in (96, 192):
            raise ValueError(f"Expected 96 or 192 slot bytes, got {len(slot_values)}")
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.send_override(
            creds, slot_values,
            high_marker=high_marker, low_marker=low_marker, log=log,
        )

    def reset_overrides(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        high_marker: int = DEFAULT_MARKER_HIGH,
        low_marker: int = DEFAULT_MARKER_LOW,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Clear all overrides (all slots → follow base schedule)."""
        slot_values = bytes([SLOT_NO_OVERRIDE] * 96)
        return self.set_override(
            home_id, device_id, model, slot_values,
            high_marker=high_marker, low_marker=low_marker, log=log,
        )

    # ── Sell (discharge-to-grid) ──────────────────────────────────────

    def send_sell(
        self,
        home_id: str,
        device_id: str,
        model: str,
        duration_seconds: int,
        *,
        label: str = "Sell",
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Send a sell (discharge-to-grid) command.

        Args:
            duration_seconds: How long the sell window lasts.
            label: Verbose log label for the E2E command.
            log: Optional log callback.

        Returns:
            *True* if acknowledged.
        """
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.send_sell(creds, duration_seconds, label=label, log=log)

    def cancel_sell(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        label: str = "Cancel sell",
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Cancel an active sell command."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.cancel_sell(creds, label=label, log=log)

    # Emergency charge uses the same E2E type 0x01 command as sell.
    # The battery decides whether to charge or discharge based on its
    # current state; the protocol payload is identical.

    def emergency_charge_on(
        self,
        home_id: str,
        device_id: str,
        model: str,
        duration_seconds: int,
        *,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Enable emergency charge for *duration_seconds*."""
        return self.send_sell(
            home_id, device_id, model, duration_seconds,
            label="Emergency charge", log=log,
        )

    def emergency_charge_off(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Disable emergency charge."""
        return self.cancel_sell(
            home_id, device_id, model,
            label="Cancel emergency charge", log=log,
        )

    # ── Peak shaving ─────────────────────────────────────────────────

    def get_peak_shaving(
        self,
        home_id: str,
        device_id: str,
        model: str,
        *,
        log: Callable[..., None] | None = None,
    ) -> dict:
        """Read peak shaving config and schedule via E2E.

        Returns a dict with ``config`` and ``schedule`` sub-dicts.
        """
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.read_peak_shaving(creds, log=log)

    def toggle_peak_shaving(
        self,
        home_id: str,
        device_id: str,
        model: str,
        enabled: bool,
        *,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Toggle peak shaving on or off."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.toggle_peak_shaving(creds, enabled, log=log)

    def set_peak_shaving_points(
        self,
        home_id: str,
        device_id: str,
        model: str,
        peak_reserve_pct: int,
        ups_reserve_pct: int,
        *,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Set peak shaving reserve percentages."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.set_peak_shaving_points(
            creds, peak_reserve_pct, ups_reserve_pct, log=log,
        )

    def set_peak_shaving_schedule(
        self,
        home_id: str,
        device_id: str,
        model: str,
        schedule_id: int,
        start_seconds: int,
        end_seconds: int,
        repeat_days: int,
        min_peak_power_w: int,
        *,
        all_day: bool = False,
        trailing: bytes = b"",
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Add or modify a peak shaving schedule."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.set_peak_shaving_schedule(
            creds, schedule_id, start_seconds, end_seconds,
            repeat_days, min_peak_power_w,
            all_day=all_day, trailing=trailing, log=log,
        )

    def set_peak_shaving_redundancy(
        self,
        home_id: str,
        device_id: str,
        model: str,
        redundancy: int,
        *,
        log: Callable[..., None] | None = None,
    ) -> bool:
        """Set peak shaving redundancy value."""
        creds = self.e2e_login(home_id, device_id, model)
        return _e2e.set_peak_shaving_redundancy(creds, redundancy, log=log)
