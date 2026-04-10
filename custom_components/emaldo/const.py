"""Constants for the Emaldo integration."""

DOMAIN = "emaldo"

CONF_HOME_ID = "home_id"
CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_APP_VERSION = "app_version"

DEFAULT_SCAN_INTERVAL = 60  # seconds

# Schedule polling configuration
CONF_SCHEDULE_START_HOUR = "schedule_start_hour"
CONF_SCHEDULE_START_MINUTE = "schedule_start_minute"
CONF_SCHEDULE_INTERVAL = "schedule_interval"

DEFAULT_SCHEDULE_START_HOUR = 14
DEFAULT_SCHEDULE_START_MINUTE = 0
DEFAULT_SCHEDULE_INTERVAL = 7200  # 2 hours in seconds

# Event names
EVENT_NEXT_DAY_SCHEDULE_READY = f"{DOMAIN}_next_day_schedule_ready"


