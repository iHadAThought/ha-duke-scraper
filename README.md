# Duke Energy Scraper (Home Assistant)

Custom integration that logs into Duke Energy My Account with Playwright, exports usage, and writes it into the Energy dashboard **Grid consumption** statistic.

Replaces the old HACS `duke_energy` OAuth integration (and the Chromium reauth helper on the M2 Mac).

## How to add your password

Credentials are **not** stored in git or `secrets.yaml`.

1. Open Home Assistant → **Settings → Devices & services → Add integration**
2. Search for **Duke Energy Scraper**
3. Enter Duke My Account **email** and **password** (optional meter serial + worker URL/IP)
4. After Submit, the **MFA** step appears: a code field plus **Request code** (sends the Duke email OTP)
5. Enter the email code (prefer “Remember this device” — handled automatically by the worker)

HA stores credentials in `.storage/core.config_entries`. Use **Reconfigure** if the password changes, or **Configure** / reauth when MFA expires.

## Login method

The worker uses two auth paths:

1. **Auth0 mobile PKCE** → CMA API tokens for **hourly** backfill  
2. **My Account web session** → **Download My Data** Green Button XML for **15-minute** incremental polls

Web session is cached in `/config/.duke_scraper/web_storage_state.json`. When cookies expire, the worker first tries a **silent refresh**:

1. **Worker passkey** (if `/config/.duke_scraper/webauthn_credential.json` exists) via Auth0 **Continue with Passkey**
2. Otherwise **email/password** (often succeeds without MFA on this tenant)
3. Only if Duke still demands MFA → HA persistent notification + reauth (Request code → OTP)

### Worker passkey (optional)

Duke’s login screen supports passkeys. The worker can own a **virtual** WebAuthn credential (CDP authenticator in Chromium)—not your phone’s iCloud passkey.

- After MFA (or via `POST /passkey/enroll`), the worker captures a passkey **if Auth0 shows an enrollment screen**.
- If you already have an iOS/phone passkey, Duke often **skips** offering another; enrollment may require removing the phone passkey under **My Account → Passkeys**, then retrying enroll (you can add the phone passkey back afterward if Duke allows multiple).
- Health: `passkey_enrolled: true` when the credential file is present.

Password silent refresh alone usually avoids the ~30-day email OTP loop even without a worker passkey.

## Architecture

| Piece | Role |
|---|---|
| `custom_components/duke_scraper/` | HA integration + config flow + statistics writer |
| `duke_scraper_worker` Docker container | Playwright Chromium on the `hassio` network |
| `/config/.duke_scraper/` | Tokens, web storage state, downloads, `worker_url` |
| Statistic ID | `duke_scraper:electric_325385805_energy_consumption` |

## First run / ongoing

- **First successful poll:** exports **hourly** usage (CMA API) for all available data in calendar **2025 and 2026**, then marks `backfill_done`.
- **Later polls (~every 6 hours, jitter ±45 min):** clicks **Download My Data**, parses Green Button ESPI XML (**15-minute** intervals), imports the last **7 days**.

## Worker lifecycle

On HAOS (VM 105):

```bash
cd /mnt/data/supervisor/homeassistant/duke_scraper_worker
docker build -t duke_scraper_worker:local .
docker rm -f duke_scraper_worker
docker run -d --name duke_scraper_worker --restart unless-stopped \
  --network hassio --network-alias duke_scraper_worker \
  -v /mnt/data/supervisor/homeassistant/.duke_scraper:/data \
  -e DUKE_SCRAPER_DATA=/data -e TZ=America/New_York \
  duke_scraper_worker:local

IP=$(docker inspect duke_scraper_worker --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}')
echo -n "http://$IP:8765" > /mnt/data/supervisor/homeassistant/.duke_scraper/worker_url
```

Health: `curl http://$IP:8765/health` → `playwright_ready: true`, `web_state: true` after first web login.

Source in this repo: [`ha-addons/duke_scraper/`](ha-addons/duke_scraper/).

## Troubleshooting

### Worker has no id_token

Re-add the integration or clear `/config/.duke_scraper/tokens.json` and re-validate login.

### MFA required for web download

When silent refresh cannot complete (Duke demands MFA), HA shows a persistent notification and opens reauth.

1. Open the notification / **Duke Energy Scraper → Reauthenticate** (or **Configure**)
2. Check **Request code** and submit (or wait for auto-send on first setup)
3. Enter the MFA code from email and submit

Worker endpoints: `POST /mfa/start`, `POST /mfa/complete`, `GET /mfa/status`, `POST /passkey/enroll`. Session file: `web_storage_state.json`. Passkey file: `webauthn_credential.json`.

Health: `curl …/health` → `playwright_ready`, `web_state`, `passkey_enrolled`.

### Export / 15-minute empty

Confirm `Download My Data` works in the browser on `/my-account/usage`. Check worker logs: `docker logs duke_scraper_worker`.
