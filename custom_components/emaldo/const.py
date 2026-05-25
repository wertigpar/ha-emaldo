"""Constants for the Emaldo integration."""

DOMAIN = "emaldo"

CONF_HOME_ID = "home_id"
CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_APP_VERSION = "app_version"

DEFAULT_APP_ID = "CXRqKjx2MzSAkdyucR9NDyPiiQR2vQcQ"
DEFAULT_APP_SECRET = "FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT"
DEFAULT_APP_VERSION = "2.8.4"

DEFAULT_SCAN_INTERVAL = 60  # seconds
REALTIME_SCAN_INTERVAL = 10  # seconds — fast E2E power flow polling
KEEPALIVE_INTERVAL = 7  # seconds — UDP session keepalive (relay TTL ~10s)

# Schedule polling configuration
CONF_SCHEDULE_START_HOUR = "schedule_start_hour"
CONF_SCHEDULE_START_MINUTE = "schedule_start_minute"
CONF_SCHEDULE_INTERVAL = "schedule_interval"

DEFAULT_SCHEDULE_START_HOUR = 14
DEFAULT_SCHEDULE_START_MINUTE = 0
DEFAULT_SCHEDULE_INTERVAL = 7200  # 2 hours in seconds

# Models that do not support the solar PV input
PV_UNSUPPORTED_MODELS: frozenset[str] = frozenset({
    "PS1-BAK10-HS10",  # Power Store
    "VB1-BAK5-HS10",   # Power Pulse
    "HP5000",          # Legacy
    "HP5001",          # Legacy
    "PSE1",            # Power Sense 1
    "PSE2",            # Power Sense 2
})

# Models that do not support the EV charger function
EV_UNSUPPORTED_MODELS: frozenset[str] = frozenset({
    "PS1-BAK10-HS10",  # Power Store
    "VB1-BAK5-HS10",   # Power Pulse
    "HP5000",          # Legacy
    "HP5001",          # Legacy
    "PSE1",            # Power Sense 1
    "PSE2",            # Power Sense 2
})

# Event names
EVENT_NEXT_DAY_SCHEDULE_READY = f"{DOMAIN}_next_day_schedule_ready"


