# Duke Energy Scraper (Home Assistant)

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=iHadAThought&repository=ha-duke-scraper&category=integration)

Custom Home Assistant integration that logs into **Duke Energy My Account**, exports meter usage, and writes it into the Energy dashboard **Grid consumption** statistic.

This repository includes:

1. **`custom_components/duke_scraper/`** — Home Assistant integration (install via HACS or manually)
2. **`duke_scraper_worker/`** — Home Assistant **Supervisor add-on** (Playwright worker)
3. **`worker/`** — Same worker for Docker Compose / manual `docker run` (non-HAOS)

On **Home Assistant OS**, install the integration with HACS **and** the worker add-on from this repo’s add-on store. HACS cannot start Docker containers by itself.

> **Not affiliated with Duke Energy.** Uses undocumented My Account / mobile CMA API surfaces. Use at your own risk; Duke may change auth or endpoints at any time.

## Features

- Config flow: email/password → scraping preferences → email MFA
- Optional **worker-owned passkey** (virtual WebAuthn) to reduce recurring MFA
- First-run history depth (7 days → max available)
- Ongoing resolution: **15-minute**, **hourly**, or **daily**
- Poll schedule: **30 minutes** minimum, default **2 hours**, up to **30 days**
- Usage exports are **kWh only** (Green Button + CMA graph do not include \$)

## Architecture

| Piece | Role |
|---|---|
| `custom_components/duke_scraper/` | HA config flow, coordinator, Energy statistics |
| **Duke Energy Scraper Worker** add-on | Playwright on port **8765** (`/health`, `/export`, `/mfa/*`) |
| Data directory (`/config/.duke_scraper`) | Tokens, web session, passkey, downloads, `worker_url` |

Statistic ID:

`duke_scraper:electric_<METER_SERIAL>_energy_consumption`

## Requirements

- Home Assistant **2024.8+**
- **HAOS / Supervised:** Supervisor add-on (below) **or** a manual Docker worker
- **Container / Core:** Docker Compose / `docker run` for the worker
- Network path from Home Assistant → worker (`http://<worker>:8765`)
- Duke Energy My Account email/password and access to email MFA codes

---

## Install the integration

### Option A — HACS (recommended)

1. Install [HACS](https://www.hacs.xyz/) if needed.
2. Use the [HACS my-link](https://my.home-assistant.io/redirect/hacs_repository/?owner=iHadAThought&repository=ha-duke-scraper&category=integration), **or** **HACS → Integrations → ⋮ → Custom repositories**
3. Repository: `https://github.com/iHadAThought/ha-duke-scraper` · Category: **Integration**
4. Download **Duke Energy Scraper**
5. **Restart Home Assistant**

### Option B — Manual

```bash
# From a machine with access to your HA config folder
cp -a custom_components/duke_scraper /path/to/homeassistant/config/custom_components/
```

On HAOS the config path is usually `/config` (or `/mnt/data/supervisor/homeassistant` on the host).

Restart Home Assistant after copying.

---

## Install the worker

Do this **before** (or right after) adding the integration.

### Home Assistant OS — Supervisor add-on (recommended)

1. **Settings → Add-ons → Add-on store → ⋮ → Repositories**
2. Add: `https://github.com/iHadAThought/ha-duke-scraper`
3. Find **Duke Energy Scraper Worker** → **Install** → **Start** (enable Start on boot)
4. Wait until the add-on log shows Playwright ready / listening on `8765` (image pulls from GHCR; first pull is large)
5. The add-on writes `/config/.duke_scraper/worker_url` automatically — leave **Worker URL** blank in the integration, or paste the URL from that file

Pre-built images: `ghcr.io/ihadathought/{amd64|aarch64}-duke-scraper-worker` ([v1.4.1 release](https://github.com/iHadAThought/ha-duke-scraper/releases/tag/v1.4.1)).

**One-time (required for HAOS pulls):** make both GHCR packages **Public**:

- [amd64-duke-scraper-worker](https://github.com/users/iHadAThought/packages/container/package/amd64-duke-scraper-worker) → Package settings → Change visibility → Public  
- [aarch64-duke-scraper-worker](https://github.com/users/iHadAThought/packages/container/package/aarch64-duke-scraper-worker) → same

Supported architectures: **amd64**, **aarch64**.

### Home Assistant OS — manual Docker (alternative)

You need a shell on the HA host (Advanced SSH / Terminal add-on with protection mode off, or console access).

```bash
# Clone or copy this repo somewhere on the HA host, then:
cd /path/to/ha-duke-scraper

docker build -t duke_scraper_worker:local ./worker

docker rm -f duke_scraper_worker 2>/dev/null || true

# Persist worker data next to HA config
mkdir -p /config/.duke_scraper

docker run -d \
  --name duke_scraper_worker \
  --restart unless-stopped \
  --network hassio \
  --network-alias duke_scraper_worker \
  -v /config/.duke_scraper:/data \
  -e DUKE_SCRAPER_DATA=/data \
  -e TZ=America/New_York \
  duke_scraper_worker:local

# Discover the container IP on the hassio network and save it for HA
IP=$(docker inspect duke_scraper_worker \
  --format '{{(index .NetworkSettings.Networks "hassio").IPAddress}}')
echo -n "http://${IP}:8765" > /config/.duke_scraper/worker_url
echo "worker_url=$(cat /config/.duke_scraper/worker_url)"
curl -s "http://${IP}:8765/health"
```

Expected health JSON includes `"playwright_ready": true`.

**Notes for HAOS**

- Attach to the **`hassio`** network so Core can reach the container. Hassio DNS often does **not** resolve manually started containers, so saving `worker_url` (or pasting the URL in the integration) is important.
- After a container recreate, the IP may change — re-run the `docker inspect` + `worker_url` lines (or update the Worker URL in the integration).
- Time zone: set `TZ` to your Duke billing timezone (often `America/New_York`).

### Home Assistant Container / Docker Compose

If HA already runs in Compose, add the worker alongside it on a shared network.

Example `docker-compose.yml` snippet:

```yaml
services:
  homeassistant:
    # ... your existing HA service ...
    networks: [ha]

  duke_scraper_worker:
    build: ./worker
    image: duke_scraper_worker:local
    container_name: duke_scraper_worker
    restart: unless-stopped
    environment:
      DUKE_SCRAPER_DATA: /data
      DUKE_SCRAPER_HOST: "0.0.0.0"
      DUKE_SCRAPER_PORT: "8765"
      TZ: America/New_York
    volumes:
      # Use the same host path you mount as HA /config, plus .duke_scraper
      - ./ha-config/.duke_scraper:/data
    networks: [ha]
    # Optional: publish for debugging from the LAN
    # ports:
    #   - "8765:8765"

networks:
  ha:
    name: ha
```

Then:

```bash
docker compose build duke_scraper_worker
docker compose up -d duke_scraper_worker
curl -s http://duke_scraper_worker:8765/health   # from another container on `ha`
```

In the integration, set **Worker URL** to:

`http://duke_scraper_worker:8765`

(or the published host port, e.g. `http://192.168.x.x:8765`).

### Home Assistant Supervised / generic Linux Docker

Same as Container: build `./worker`, run with a volume for data, put HA and the worker on one Docker network, point the integration at `http://<name-or-ip>:8765`.

### Quick health check

```bash
curl -s http://127.0.0.1:8765/health
# or from HAOS: curl -s "$(cat /config/.duke_scraper/worker_url)/health"
```

Useful flags: `playwright_ready`, `web_state`, `passkey_enrolled`, `mfa_pending`.

---

## Setup in Home Assistant

1. Confirm the worker is healthy (above).
2. **Settings → Devices & services → Add integration → Duke Energy Scraper**
3. Enter Duke **email** / **password**
4. Optional: meter serial, **Worker URL** (defaults to `/config/.duke_scraper/worker_url` if present)
5. Preferences:
   - Use worker passkey
   - First export history depth
   - Ongoing data resolution
   - Poll interval (min 30 minutes, default 2 hours)
6. Complete **MFA** (Request code → enter the email OTP)

Later: **Configure** → Preferences or Credentials / MFA.

### Energy dashboard

**Settings → Dashboards → Energy → Add consumption** → pick the Duke external statistic as **Grid consumption**.

Usage is kWh-only. Set a fixed \$/kWh (or another price entity) in the Energy dashboard if you want cost estimates.

---

## Existing Duke passkey (phone / iCloud)

Duke often **skips** offering a second passkey if one already exists. The worker needs its own virtual passkey (or leave passkey disabled and rely on password + occasional MFA).

### 1. Prefer the website UI

1. Sign in at [Duke Energy My Account](https://www.duke-energy.com/my-account)
2. **Settings → Profile → Passkeys** (wording may vary)
3. Look for **Remove** / **Revoke** and remove the existing passkey
4. In HA, Configure / MFA with **Use worker passkey** enabled so the worker can click **Create Passkey**
5. Optionally add your phone passkey again if Duke allows multiple

### 2. If Remove is missing (advanced — proceed at your own risk)

When only one passkey exists, Duke’s UI may hide Remove. Some accounts can revoke via browser DevTools against My Account APIs.

**Warnings**

- Undocumented APIs; may change without notice
- Revoking your only passkey can break passkey sign-in until you enroll again
- You are responsible for anything you run in the browser console on duke-energy.com

**High-level steps**

1. Sign in, open DevTools → Network, reload Passkeys settings
2. Note session headers such as `cdxp-session` and `authorization: MyAccount`
3. Call the list/revoke endpoints that page uses (often under a `cdxp` / CIAM `revoke-passkey` path)
4. Confirm removal in the UI, enroll the **worker** passkey via HA MFA, then re-add a phone passkey if desired

If you prefer not to do that, leave **Use worker passkey** off and complete MFA when HA notifies you (~30-day web session).

---

## Troubleshooting

### Worker unreachable

- `docker ps` shows `duke_scraper_worker` running
- `curl "$(cat /config/.duke_scraper/worker_url)/health"` (or your Worker URL) returns JSON
- After recreate, refresh `worker_url` / integration Worker URL (IP may change on `hassio`)

### MFA required

HA shows a persistent notification when the web session expires. **Configure / Reauthenticate** → Request code → OTP.

### Empty 15-minute export

Confirm **Download My Data** works in a browser on My Account → Usage. Check `docker logs duke_scraper_worker`.

### Passkey not enrolled

See **Existing Duke passkey**. Health should show `passkey_enrolled: true` after Create Passkey (`webauthn_credential.json` in the data dir).

### Logs

```bash
docker logs -f duke_scraper_worker
```

---

## License

[MIT](LICENSE) — © 2026 Brendan Mahoney.

Not an official Duke Energy or Home Assistant product. Provided as-is.
