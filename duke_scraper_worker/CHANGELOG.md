# Changelog — Duke Energy Scraper Worker add-on

## 1.4.1

- Publish pre-built images to GHCR (`ghcr.io/ihadathought/{arch}-duke-scraper-worker`)
- Supervisor installs by pulling the image instead of building Playwright on-device

## 1.4.0

- Initial Supervisor add-on (`duke_scraper_worker`)
- Writes `/config/.duke_scraper/worker_url` for the HACS integration
