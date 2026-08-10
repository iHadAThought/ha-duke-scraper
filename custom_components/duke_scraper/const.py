"""Constants for Duke Energy Scraper."""

from __future__ import annotations

DOMAIN = "duke_scraper"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_METER_SERIAL = "meter_serial"
CONF_WORKER_URL = "worker_url"
CONF_MFA_CODE = "mfa_code"
CONF_REQUEST_CODE = "request_code"

# Preferences (stored in entry.options)
CONF_USE_PASSKEY = "use_passkey"
CONF_BACKFILL_DAYS = "backfill_days"
CONF_INTERVAL = "interval"
CONF_UPDATE_MINUTES = "update_minutes"

# Hassio DNS often fails for manually-started containers; the worker writes
# /config/.duke_scraper/worker_url on start. Runtime discovery probes the file
# plus these hostnames — sticky IPv4 config-entry values are not trusted alone.
DEFAULT_WORKER_URL = "http://local-duke-scraper-worker:8765"
WORKER_HOST_FALLBACKS: tuple[str, ...] = (
    "local-duke-scraper-worker",
    "duke_scraper_worker",
)
WORKER_URL_FILE = "worker_url"
DEFAULT_METER_SERIAL = "325385805"

# First-run backfill year caps when backfill_days == "max"
BACKFILL_START_YEAR = 2025
BACKFILL_END_YEAR = 2026

# Defaults / legacy fallbacks
DEFAULT_USE_PASSKEY = True
DEFAULT_BACKFILL_DAYS = "max"
DEFAULT_INTERVAL = "fifteen_minute"
DEFAULT_UPDATE_MINUTES = 120
LOOKBACK_DAYS = 7

BACKFILL_DAY_CHOICES: dict[str, str] = {
    "7": "Last 7 days",
    "30": "Last 30 days",
    "90": "Last 90 days",
    "365": "Last 1 year",
    "max": "Max available",
}

INTERVAL_CHOICES: dict[str, str] = {
    "fifteen_minute": "15 minutes",
    "hourly": "1 hour",
    "daily": "1 day",
}

UPDATE_MINUTE_CHOICES: dict[int, str] = {
    30: "Every 30 minutes",
    60: "Every 1 hour",
    120: "Every 2 hours",
    360: "Every 6 hours",
    720: "Every 12 hours",
    1440: "Every 1 day",
    10080: "Every 7 days",
    20160: "Every 14 days",
    43200: "Every 30 days",
}

DATA_DIR_NAME = ".duke_scraper"
STORAGE_STATE_NAME = "storage_state.json"
BACKFILL_DONE_KEY = "backfill_done"
WEB_MFA_OK_KEY = "web_mfa_ok"
NOTIFICATION_MFA_ID = "duke_scraper_mfa_required"


def default_options() -> dict:
    """Default entry.options for new and migrated installs."""
    return {
        CONF_USE_PASSKEY: DEFAULT_USE_PASSKEY,
        CONF_BACKFILL_DAYS: DEFAULT_BACKFILL_DAYS,
        CONF_INTERVAL: DEFAULT_INTERVAL,
        CONF_UPDATE_MINUTES: DEFAULT_UPDATE_MINUTES,
    }


def option(entry, key: str, default=None):
    """Read a preference from entry.options with fallback to defaults."""
    opts = entry.options or {}
    if key in opts:
        return opts[key]
    defaults = default_options()
    if default is not None:
        return defaults.get(key, default)
    return defaults.get(key)
