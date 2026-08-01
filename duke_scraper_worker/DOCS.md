# Duke Energy Scraper Worker (Home Assistant add-on)

Playwright Chromium worker used by the **Duke Energy Scraper** integration
(HACS / `custom_components/duke_scraper`).

## What it does

- Listens on TCP **8765** (`/health`, `/export`, `/mfa/*`, `/passkey/enroll`)
- Stores session data under `/config/.duke_scraper`
- Writes `worker_url` so the integration can find this add-on on the hassio network

## Install

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**
2. Add: `https://github.com/iHadAThought/ha-duke-scraper`
3. Install **Duke Energy Scraper Worker**, start it, enable **Start on boot**
4. Install the integration via [HACS](https://my.home-assistant.io/redirect/hacs_repository/?owner=iHadAThought&repository=ha-duke-scraper&category=integration)
5. Restart Home Assistant, then add the integration (Worker URL can be left blank if `worker_url` was written)

## Options

| Option | Default | Meaning |
|---|---|---|
| `timezone` | `America/New_York` | IANA TZ for Duke export timestamps |

## Notes

- Supported architectures: **amd64**, **aarch64** (Playwright image)
- Installs from GHCR (`ghcr.io/ihadathought/{arch}-duke-scraper-worker`); first pull can take several minutes
- HACS does **not** install this add-on; both pieces are required on HAOS
