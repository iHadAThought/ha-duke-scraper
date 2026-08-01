#!/usr/bin/env bash
# Copy canonical worker/worker.py into the Supervisor add-on build context.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cp "$ROOT/worker/worker.py" "$ROOT/duke_scraper_worker/worker.py"
echo "Synced worker.py → duke_scraper_worker/"
