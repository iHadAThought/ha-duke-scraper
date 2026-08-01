# Contributing

Thanks for your interest in improving **Duke Energy Scraper**.

This project is unofficial and not affiliated with Duke Energy or Home Assistant.
Contributions should respect that scraping undocumented sites can break without notice.

## Ways to contribute

- Bug reports and feature ideas via [Issues](https://github.com/iHadAThought/ha-duke-scraper/issues)
- Pull requests for the Home Assistant integration (`custom_components/duke_scraper/`)
- Pull requests for the Playwright worker (`worker/` and `duke_scraper_worker/`)
- Documentation fixes in `README.md` / add-on `DOCS.md`

## Development setup

1. Fork and clone this repository.
2. For the integration: copy `custom_components/duke_scraper` into a Home Assistant `config/custom_components/` tree (or use HACS from your fork).
3. For the worker: build from `worker/` (Compose) or install the Supervisor add-on from your fork.
4. Keep `worker/worker.py` and `duke_scraper_worker/worker.py` in sync (`./scripts/sync-addon-worker.sh`).

## Pull requests

1. Create a branch from `main`.
2. Keep changes focused; avoid unrelated refactors.
3. Update docs when behavior or install steps change.
4. Ensure CI passes (hassfest + HACS validation).
5. Do not commit credentials, session dumps, MFA codes, or personal Duke account data.
6. Fill out the pull request template.

## Code of conduct

Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please do **not** open public issues for vulnerabilities. See [SECURITY.md](.github/SECURITY.md).
