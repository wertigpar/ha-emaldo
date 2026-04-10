"""Constants for the Emaldo client (bundled for HA integration).

APP_ID, APP_SECRET, and DEFAULT_APP_VERSION are hardcoded defaults.
Use set_params() to override them from the HA config entry.
"""

# ----- App identity (from APK 2.8.3) -----
_DEFAULT_APP_ID = "CXRqKjx2MzSAkdyucR9NDyPiiQR2vQcQ"
_DEFAULT_APP_SECRET = "FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT"
_DEFAULT_APP_VERSION = "2.8.3"

_override_params: dict | None = None


def set_params(app_id: str, app_secret: str, app_version: str) -> None:
    """Override app parameters programmatically (e.g. from Home Assistant config)."""
    global _override_params
    _override_params = {
        "app_id": app_id,
        "app_secret": app_secret,
        "app_version": app_version,
    }


def get_app_id() -> str:
    if _override_params is not None:
        return _override_params["app_id"]
    return _DEFAULT_APP_ID


def get_app_secret() -> bytes:
    if _override_params is not None:
        return _override_params["app_secret"].encode()
    return _DEFAULT_APP_SECRET.encode()


def get_default_app_version() -> str:
    if _override_params is not None:
        return _override_params["app_version"]
    return _DEFAULT_APP_VERSION


# API endpoints
API_HOST = "api.emaldo.com"
DP_HOST = "dp.emaldo.com"  # data plane (stats/analytics)

# Default E2E server (actual host is returned by e2e-user-login API)
DEFAULT_E2E_HOST = "e2e2.emaldo.com"
DEFAULT_E2E_PORT = 1050

# Endpoints routed via the data plane host
DP_ENDPOINTS = [
    "/bmt/stats/",
    "/home/get-home-fcr-predict-revenue-summary/",
    "/home/get-home-fcr-predict-revenue-daily/",
]

# App version check
APP_SHORT = "emaldo_android"  # Platform identifier for version check API

# E2E override slot values
SLOT_NO_OVERRIDE = 0x80  # Follow base schedule (128)
SLOT_IDLE = 0x00         # No charge/discharge
SLOT_CHARGE_DEFAULT = 0x48  # Charge at 72%

# Default battery markers (percentage)
DEFAULT_MARKER_HIGH = 72
DEFAULT_MARKER_LOW = 20
MIN_MARKER_GAP = 15  # Minimum gap between high and low markers

# Currency sub-unit labels by timezone
_TZ_CURRENCY_SUBUNIT = {
    "Europe/Stockholm": "öre",
    "Europe/Oslo": "øre",
    "Europe/Copenhagen": "øre",
}

def price_unit_for_timezone(tz_name: str) -> str:
    """Return the electricity price sub-unit label for a timezone."""
    subunit = _TZ_CURRENCY_SUBUNIT.get(tz_name, "c")
    return f"{subunit}/kWh"


def encode_override_action(action: str, low: int, high: int) -> int:
    """Encode a named override action to a slot byte value.

    Args:
        action: One of ``charge-low``, ``charge-high``, ``charge-100``,
            ``idle``, ``discharge-low``, ``discharge-high``, ``none``.
        low: Current low marker percentage.
        high: Current high marker percentage.

    Returns:
        Slot byte value 0-255.
    """
    action = action.lower().strip()
    _map = {
        "charge-low": low,
        "cl": low,
        "charge-high": high,
        "ch": high,
        "charge-100": 100,
        "c100": 100,
        "cf": 100,
        "idle": SLOT_IDLE,
        "i": SLOT_IDLE,
        "discharge-low": (256 - low) & 0xFF,
        "dl": (256 - low) & 0xFF,
        "discharge-high": (256 - high) & 0xFF,
        "dh": (256 - high) & 0xFF,
        "none": SLOT_NO_OVERRIDE,
        "clear": SLOT_NO_OVERRIDE,
        "x": SLOT_NO_OVERRIDE,
    }
    if action in _map:
        return _map[action]
    raise ValueError(f"Unknown override action: {action!r}")


def decode_slot_action(value: int, low: int, high: int) -> str:
    """Decode a slot byte value to a human-readable action string."""
    if value == SLOT_NO_OVERRIDE:
        return "none"
    if value == SLOT_IDLE:
        return "idle"
    if 1 <= value <= 100:
        if value == low:
            return f"charge-low ({low}%)"
        elif value == high:
            return f"charge-high ({high}%)"
        elif value == 100:
            return "charge-100"
        else:
            return f"charge ({value}%)"
    if value > 128:
        threshold = 256 - value
        if threshold == low:
            return f"discharge-low ({low}%)"
        elif threshold == high:
            return f"discharge-high ({high}%)"
        else:
            return f"discharge ({threshold}%)"
    return f"unknown (0x{value:02x})"
