"""Constants for Duke Energy Scraper."""

from __future__ import annotations

DOMAIN = "duke_scraper"

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
CONF_METER_SERIAL = "meter_serial"
CONF_WORKER_URL = "worker_url"
CONF_MFA_CODE = "mfa_code"
CONF_REQUEST_CODE = "request_code"

# Hassio DNS does not resolve manually-started containers; deploy writes
# /config/.duke_scraper/worker_url with the current hassio-network IP.
DEFAULT_WORKER_URL = "http://172.30.33.4:8765"
WORKER_URL_FILE = "worker_url"
DEFAULT_METER_SERIAL = "325385805"

# First-run backfill window (hourly for all available data in these years)
BACKFILL_START_YEAR = 2025
BACKFILL_END_YEAR = 2026

# Ongoing polls: ~6 hours with random jitter (see coordinator).
UPDATE_INTERVAL_HOURS = 6
UPDATE_JITTER_MINUTES = 45
LOOKBACK_DAYS = 7

DATA_DIR_NAME = ".duke_scraper"
STORAGE_STATE_NAME = "storage_state.json"
BACKFILL_DONE_KEY = "backfill_done"
WEB_MFA_OK_KEY = "web_mfa_ok"
NOTIFICATION_MFA_ID = "duke_scraper_mfa_required"
