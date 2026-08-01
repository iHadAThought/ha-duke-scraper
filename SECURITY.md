# Security policy

## Supported versions

| Version | Supported |
| --- | --- |
| Latest release on `main` / GitHub Releases | Yes |
| Older tags | No — please upgrade |

## Reporting a vulnerability

Please **do not** file a public GitHub issue for security problems (credential leaks, auth bypass, remote code execution, etc.).

Prefer one of:

1. [GitHub private vulnerability reporting](https://github.com/iHadAThought/ha-duke-scraper/security/advisories/new) (recommended)
2. Contact the maintainer [@iHadAThought](https://github.com/iHadAThought) via GitHub

Please include:

- Affected version / commit
- Impact (e.g. credential exposure, session theft)
- Steps to reproduce (without sharing real passwords or live session tokens)

You should receive an acknowledgment within a few days. We will coordinate a fix and disclosure timeline.

## Scope notes

This project logs into Duke Energy My Account using undocumented web/API surfaces and stores session/passkey material under `/config/.duke_scraper`. Treat that directory as sensitive. Reports about Duke Energy’s own website security belong with Duke Energy, not this repository.
