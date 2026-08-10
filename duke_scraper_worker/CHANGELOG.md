# Changelog — Duke Energy Scraper Worker add-on

## 1.0.1

- Worker publishes `worker_url` on every start (IP or advertise host) so HA survives hassio IP drift
- Integration auto-discovers worker via file + hostnames + `/health` probe
- New Home Assistant–style brand icon and logo (Duke ↔ HA sync mark)

## 1.0.0

- Initial public release
- Supervisor add-on with pre-built GHCR images (`ghcr.io/ihadathought/{arch}-duke-scraper-worker`)
- Writes `/config/.duke_scraper/worker_url` for the HACS integration
