#!/usr/bin/env bash
# Start the Duke scraper worker and publish worker_url for the HA integration.
set -euo pipefail

OPTIONS_PATH=/data/options.json
DATA_DIR=/config/.duke_scraper
mkdir -p "$DATA_DIR"

if [[ -f "$OPTIONS_PATH" ]]; then
  TZ_VALUE="$(python3 - <<'PY'
import json
from pathlib import Path
opts = json.loads(Path("/data/options.json").read_text(encoding="utf-8"))
print(opts.get("timezone") or "America/New_York")
PY
)"
  export TZ="$TZ_VALUE"
else
  export TZ="${TZ:-America/New_York}"
fi

# Resolve this add-on's DNS name on the hassio network (REPO_SLUG with _ → -).
WORKER_HOST=""
if [[ -n "${SUPERVISOR_TOKEN:-}" ]]; then
  WORKER_HOST="$(python3 - <<'PY'
import json, os, urllib.request
token = os.environ.get("SUPERVISOR_TOKEN", "")
req = urllib.request.Request(
    "http://supervisor/addons/self/info",
    headers={"Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp).get("data") or {}
except Exception as err:
    print(f"supervisor lookup failed: {err}", flush=True)
    raise SystemExit(0)
host = data.get("hostname") or data.get("slug") or ""
host = str(host).replace("_", "-")
print(host)
PY
)" || true
fi

if [[ -z "$WORKER_HOST" ]]; then
  WORKER_HOST="local-duke-scraper-worker"
fi

echo -n "http://${WORKER_HOST}:8765" > "${DATA_DIR}/worker_url"
echo "Wrote ${DATA_DIR}/worker_url -> $(cat "${DATA_DIR}/worker_url")"

export DUKE_SCRAPER_DATA="$DATA_DIR"
export DUKE_SCRAPER_HOST=0.0.0.0
export DUKE_SCRAPER_PORT=8765
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/ms-playwright}"

exec python3 /app/worker.py
