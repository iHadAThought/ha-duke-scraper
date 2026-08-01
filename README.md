# Duke Energy Scraper (Home Assistant)

Custom Home Assistant integration that logs into **Duke Energy My Account**, exports meter usage, and writes it into the Energy dashboard **Grid consumption** statistic.

This repository also includes a companion **Playwright worker** (Docker). HACS installs only the custom component — you must run the worker separately.

> **Not affiliated with Duke Energy.** Scrapes the customer My Account / mobile API surfaces. Use at your own risk; Duke may change their site or terms at any time.

## Features

- Config flow: email/password → scraping preferences → email MFA
- Optional **worker-owned passkey** (virtual WebAuthn) to reduce recurring MFA
- First-run history depth (7 days → max available)
- Ongoing resolution: **15-minute**, **hourly**, or **daily**
- Poll schedule: **30 minutes** minimum, default **2 hours**, up to **30 days**
- Best-effort daily **billing snapshot** (\$/kWh, current/estimated bill, due date, past-due / status) when Duke exposes those fields
- Usage exports are **kWh only** (Green Button + CMA graph do not include \$ amounts)

## Architecture

| Piece | Role |
|---|---|
| `custom_components/duke_scraper/` | HA integration, config flow, statistics, billing sensors |
| `worker/` | Playwright Chromium HTTP worker (`/export`, `/mfa/*`, `/billing`, `/health`) |
| Persistent data dir | Tokens, web storage state, optional passkey, billing cache, downloads |

Statistic ID pattern:

`duke_scraper:electric_<METER_SERIAL>_energy_consumption`

## Requirements

- Home Assistant (2024.8+ recommended)
- Docker (or equivalent) to run the worker, reachable from HA (Hassio/HAOS: attach to the `hassio` network)
- Duke Energy My Account credentials + email MFA access

## Install (HACS custom repository)

1. Install [HACS](https://hacs.xyz/) if you have not already.
2. **HACS → Integrations → ⋮ → Custom repositories**
3. Add this repository URL (category: **Integration**)
4. Download **Duke Energy Scraper**
5. Restart Home Assistant

### Manual install

Copy `custom_components/duke_scraper/` into your HA `config/custom_components/` folder and restart.

## Worker deploy

From this repo (example on HAOS):

```bash
# Build & run (adjust paths for your install)
docker build -t duke_scraper_worker:local ./worker
docker rm -f duke_scraper_worker 2>/dev/null || true
docker run -d --name duke_scraper_worker --restart unless-stopped \
  --network hassio --network-alias duke_scraper_worker \
  -v /config/.duke_scraper:/data \
  -e DUKE_SCRAPER_DATA=/data -e TZ=America/New_York \
  duke_scraper_worker:local

IP=$(docker inspect duke_scraper_worker --format '{{(index .NetworkSettings.Networks "hassio").IPAddress}}')
mkdir -p /config/.duke_scraper
echo -n "http://$IP:8765" > /config/.duke_scraper/worker_url
curl -s "http://$IP:8765/health"
```

A `deploy.sh` helper is included for Proxmox/HAOS guest-agent deployments.

Health should report `playwright_ready: true`. After MFA, `web_state: true`. With a worker passkey, `passkey_enrolled: true`.

## Setup in Home Assistant

1. **Settings → Devices & services → Add integration → Duke Energy Scraper**
2. Enter Duke **email** / **password** (optional meter serial + worker URL)
3. Choose preferences:
   - Use worker passkey
   - First export history depth
   - Ongoing data resolution
   - Poll interval (min 30 minutes, default 2 hours)
   - Fetch billing daily
4. Complete **MFA** (Request code → enter email OTP)

Options later: **Configure** → Preferences or Credentials / MFA.

### Energy dashboard

Add the external statistic as **Grid consumption**. Usage is kWh-only. If the rate sensor is populated, you can use **“Use an entity with current price”**; otherwise set a fixed \$/kWh in Energy settings.

## Existing Duke passkey (phone / iCloud)

Duke often **skips** offering a second passkey if one already exists. The worker needs its own virtual passkey (or you can leave passkey disabled and rely on password + occasional MFA).

### 1. Prefer the website UI

1. Sign in at [Duke Energy My Account](https://www.duke-energy.com/my-account)
2. Open **Settings → Profile → Passkeys** (wording may vary)
3. Look for a **Remove** / **Revoke** button on your existing passkey and remove it
4. Re-run MFA / Configure in HA with **Use worker passkey** enabled so the worker can click **Create Passkey**
5. Optionally add your phone passkey again afterward if Duke allows multiple

### 2. If Remove is missing (advanced — proceed at your own risk)

When only one passkey exists, Duke’s UI may hide Remove. Some accounts can revoke via browser DevTools against Duke’s My Account APIs.

**Warnings**

- This uses undocumented customer-site APIs and may break or change without notice
- Revoking your only passkey can lock you out of passkey sign-in until you enroll again
- You are responsible for anything you run in the browser console on duke-energy.com

**High-level steps (only if you understand the risk):**

1. Sign in to My Account in Chrome/Safari, open DevTools → Network, reload the Passkeys settings page
2. Find an authenticated request and note headers such as `cdxp-session` and `authorization: MyAccount` (exact names vary)
3. From the Console, call the passkey list / revoke endpoints Duke uses for that page (commonly under a `cdxp` / CIAM path such as `revoke-passkey`), using those same session headers
4. Confirm the passkey is gone in the UI, then enroll the **worker** passkey via HA MFA before re-adding a phone passkey

If you are not comfortable with that, leave **Use worker passkey** off and complete MFA when HA notifies you (~30-day web session).

## Billing sensors (best-effort)

When **Fetch billing daily** is enabled, the worker probes My Account billing pages/APIs **at most once per calendar day** and may create:

| Entity | Meaning |
|---|---|
| `sensor.*_energy_rate` | \$/kWh if exposed or derivable |
| `sensor.*_current_bill` | Amount due / current bill |
| `sensor.*_estimated_bill` | Estimated / projected bill |
| `sensor.*_bill_due_date` | Due date |
| `sensor.*_billing_status` | `ok` / `past_due` / `pending` / `unknown` (+ message attributes) |
| `binary_sensor.*_bill_past_due` | Problem flag when late/past-due is detected |

If Duke does not expose these fields for your account, sensors stay unavailable — the integration will **not** invent values.

## HACS status

This integration is intended for distribution via **HACS** (custom repository first; default store later).

HACS docs:

- [Publish / general requirements](https://www.hacs.xyz/docs/publish/start/)
- [Include as a default repository](https://www.hacs.xyz/docs/publish/include/)

### Checklist toward HACS default listing

- [x] Public GitHub repository with README, description, topics, issues enabled
- [x] `hacs.json` in repo root
- [x] Valid `custom_components/duke_scraper/manifest.json` (`domain`, `documentation`, `issue_tracker`, `codeowners`, `name`, `version`)
- [x] `brand/icon.png`
- [x] GitHub Actions: HACS Action + hassfest (`.github/workflows/validate.yaml`)
- [ ] Publish a GitHub **Release** matching `manifest.json` `version` (e.g. `v1.2.0`)
- [ ] HACS Action passes with **no ignores**
- [ ] Open PR to [`hacs/default`](https://github.com/hacs/default) `integration` file (owner/major contributor; reviews can take months)

**Important:** HACS only ships `custom_components/`. Document the worker Docker requirement clearly for users (and reviewers).

## Troubleshooting

### Worker unreachable

Confirm the container is running and `worker_url` / integration worker URL points at `http://<ip>:8765`. Check `curl …/health`.

### MFA required

HA shows a persistent notification when the web session expires. Open **Configure / Reauthenticate**, request a code, submit the OTP.

### Empty 15-minute export

Confirm **Download My Data** works in a browser on My Account → Usage. Check `docker logs duke_scraper_worker`.

### Passkey not enrolled

See **Existing Duke passkey** above. Health: `passkey_enrolled` should become `true` after a successful Create Passkey ceremony (`webauthn_credential.json` in the data dir).

## License / disclaimer

Provided as-is for personal Home Assistant use. Not an official Duke Energy or Home Assistant product.
