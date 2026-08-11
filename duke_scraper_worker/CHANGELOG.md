# Changelog — Duke Energy Scraper Worker add-on

## 1.0.3

- Fix MFA / login hang on Auth0 identifier page: ignore honeypot
  `input[type=password]` and wait for a visible password field (or retry Continue)

## 1.0.2

- GHCR images moved to repo-linked paths so GitHub Actions can publish with `GITHUB_TOKEN`:
  `ghcr.io/ihadathought/ha-duke-scraper/{amd64|aarch64}-duke-scraper-worker`
- Fixes `permission_denied: write_package` on the old user-root package names

## 1.0.1

- Worker publishes `worker_url` on every start (IP or advertise host) so HA survives hassio IP drift
- Integration auto-discovers worker via file + hostnames + `/health` probe
- New Home Assistant–style brand icon and logo (Duke ↔ HA sync mark)

## 1.0.0

- Initial public release
- Supervisor add-on with pre-built GHCR images (`ghcr.io/ihadathought/{arch}-duke-scraper-worker`)
- Writes `/config/.duke_scraper/worker_url` for the HACS integration
