"""Constants for the Emaldo integration."""

DOMAIN = "emaldo"

CONF_HOME_ID = "home_id"
CONF_DEVICE_ID = "device_id"
CONF_DEVICE_MODEL = "device_model"
CONF_DEVICE_NAME = "device_name"
CONF_APP_ID = "app_id"
CONF_APP_SECRET = "app_secret"
CONF_APP_VERSION = "app_version"

DEFAULT_APP_ID = "CXRqKjx2MzSAkdyucR9NDyPiiQR2vQcQ"
DEFAULT_APP_SECRET = "FpF4Uqiio9k8p9VUSX36UZxy9wLs7ybT"
DEFAULT_APP_VERSION = "2.8.6"

DEFAULT_SCAN_INTERVAL = 60  # seconds
# Fast E2E power-flow polling. Must stay below the relay's power-flow session
# TTL so each read re-arms the next one and lands inside the live window.
# Diagnostics show the session goes cold ~(poll_interval - 0.4)s after the last
# read/handshake and that keepalive packets do NOT refresh it — only 0x30 reads
# do. Polling at 5s keeps reads chained inside the ~7-8s observed TTL.
REALTIME_SCAN_INTERVAL = 5  # seconds — fast E2E power flow polling
KEEPALIVE_INTERVAL = 7  # seconds — UDP session keepalive (relay TTL ~10s)

# -- Subscribe-and-stream realtime mode (beta13d prototype) -----------------
# Packet captures of the official app prove that power-flow (0x30) is a PUSH
# stream: the app subscribes once, the device pushes a burst of power-flow
# frames, and the app re-subscribes the whole bundle ~every 15 s. The legacy
# poll model (send 0x30, read one frame, sleep 5 s) races the relay's
# power-flow session TTL and produces the 21204 "session expired" storm that
# caps realtime success ~81%.
#
# When True, the persistent session runs a background receiver thread that
# subscribes once, continuously drains the pushed power-flow stream, and
# re-subscribes + keepalives on the app's cadence. The coordinator poll then
# just reads the freshest cached frame instead of issuing a fresh 0x30 read.
# Set to False to fall back to the legacy poll model.
#
# This is the *default*; it can be overridden per config entry via the options
# flow (``CONF_REALTIME_STREAM_MODE``). Some networks (restrictive NAT/firewall)
# drop the device-initiated push datagrams the stream model relies on, starving
# realtime data even though the official app works; those users can select the
# poll model, which is request/response and traverses NAT reliably (#41).
REALTIME_STREAM_MODE = True

#: Options-flow key to override the realtime mode per config entry. When present
#: its bool value wins over the ``REALTIME_STREAM_MODE`` module default.
CONF_REALTIME_STREAM_MODE = "realtime_stream_mode"
# How often the stream receiver re-subscribes the 0x30 power-flow stream to
# keep the device pushing. Testing showed the relay returns 21204 (session
# expired) when 0x30 subscribes are spaced tighter than ~10 s, so the periodic
# cadence stays calm (12 s — close to the official app's ~15 s) to avoid that
# storm. The receiver subscribes ONLY on this periodic schedule; chasing a
# dropped frame with an early (adaptive) resubscribe lands inside the relay's
# spacing wall, gets 21204, and triggers a reconnect storm — so we do not.
RESUBSCRIBE_INTERVAL = 12  # seconds (periodic, calm)
# Retained for the start_stream() signature/back-compat; adaptive resubscribe
# is disabled (see RESUBSCRIBE_INTERVAL comment).
STREAM_FRAME_GAP_RESUBSCRIBE = 7  # seconds (unused — adaptive disabled)
# Hard floor on subscribe spacing — must stay above the relay's ~10 s 21204
# spacing wall so a post-reconnect subscribe can never violate it.
STREAM_MIN_RESUBSCRIBE_GAP = 11  # seconds
# A cached power-flow frame older than this is treated as stale (None). Set
# wider than 2x the resubscribe interval so a single dropped subscribe-response
# (next frame ~24 s later) is BRIDGED and still counts as fresh — this is what
# breaks the ~87 % ceiling without resubscribing faster. A genuine outage falls
# through to the long-stall watchdog below.
STREAM_STALE_AFTER = 28  # seconds
# Long-stall watchdog: if the stream delivers no frame for this long (frames
# stopped, or none ever arrived after a fresh handshake), the receiver thread
# rebuilds the session in place. This is the ONLY stream teardown path — the
# coordinator no longer reconnects on staleness in stream mode, so a brief
# frame gap is bridged by the wider stale window without the fresh-handshake
# startup penalty that used to amplify it into a 20-30 s outage.
STREAM_LONG_STALL_RECONNECT = 45  # seconds

# Stream wedged full-reset escalation (beta13g): in stream mode the background
# thread's in-place reconnect is normally the only recovery. If a cloud-side
# outage (api.emaldo.com is flaky around 01:00-02:00) leaves the shared REST
# token dead, every in-thread credential refresh + re-handshake keeps failing
# and the stream can wedge indefinitely (long_stall storm, frames frozen, 0%
# success until an HA restart). After no fresh frame for this many seconds the
# coordinator escalates to a full REST-client reset + session rebuild, which
# forces a clean re-login and genuinely fresh credentials. Kept well above the
# 45 s long-stall watchdog so a healthy in-place self-heal never triggers it.
# (beta13i: lowered 180 -> 120 s to recover a dead-token wedge sooner.)
STREAM_STALL_FULL_RESET_SECONDS = 120

# Cold-start first-frame wait (beta13h): when a fresh stream session is started
# the device needs a moment to complete the handshake + subscribe and push its
# first frame. Rather than returning an immediate empty read (which leaves the
# realtime/E2E sensors on the restored value or "unknown" until the next poll),
# the poll that starts the stream blocks up to this many seconds for the first
# frame to arrive. It runs on the executor thread (never the event loop) and the
# first refresh is a background task, so HA startup is unaffected; it exits early
# the instant a frame is cached and falls through to the normal empty-read path
# if the device stays silent.
STREAM_FIRST_FRAME_WAIT = 12.0

# Rolling success-rate window (beta13e): number of most recent realtime polls
# used to compute a "recent" success rate alongside the cumulative lifetime
# rate. The cumulative rate only ever falls and is reset by a restart, so it
# hides recovery; this rolling rate reflects current health. 240 polls at the
# 5 s realtime cadence ≈ a 20-minute window.
REALTIME_SUCCESS_WINDOW = 240

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
    "HP5001",          # Legacy
    "PSE1",            # Power Sense 1
    "PSE2",            # Power Sense 2
})

# Models that do not support the EV charger function
EV_UNSUPPORTED_MODELS: frozenset[str] = frozenset({
    "PS1-BAK10-HS10",  # Power Store
    "VB1-BAK5-HS10",   # Power Pulse
    "HP5001",          # Legacy
    "PSE1",            # Power Sense 1
    "PSE2",            # Power Sense 2
})

# Event names
EVENT_NEXT_DAY_SCHEDULE_READY = f"{DOMAIN}_next_day_schedule_ready"


