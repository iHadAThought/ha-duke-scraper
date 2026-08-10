"""Runtime worker URL discovery (file + hostnames + health probe).

Hassio Docker IPs drift when containers are recreated. Prefer a fresh
``worker_url`` file and DNS hostnames over a sticky IPv4 stored in the
config entry.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_WORKER_URL,
    DATA_DIR_NAME,
    DEFAULT_WORKER_URL,
    WORKER_HOST_FALLBACKS,
    WORKER_URL_FILE,
)

_LOGGER = logging.getLogger(__name__)

_IPV4_HOST = re.compile(
    r"^(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}"
    r"(?:25[0-5]|2[0-4]\d|[01]?\d?\d)$"
)

PROBE_TIMEOUT = aiohttp.ClientTimeout(total=5)


def normalize_worker_base(url: str) -> str:
    """Strip whitespace and trailing slash."""
    return (url or "").strip().rstrip("/")


def worker_url_host(url: str) -> str | None:
    """Return hostname from a worker URL, or None if unparseable."""
    base = normalize_worker_base(url)
    if not base:
        return None
    parsed = urlparse(base if "://" in base else f"http://{base}")
    return parsed.hostname


def is_ipv4_worker_url(url: str) -> bool:
    """True when the URL host is an IPv4 address (sticky hassio IP)."""
    host = worker_url_host(url)
    return bool(host and _IPV4_HOST.match(host))


def read_worker_url_file(hass: HomeAssistant) -> str | None:
    """Read ``/config/.duke_scraper/worker_url`` if present."""
    path = Path(hass.config.path(DATA_DIR_NAME)) / WORKER_URL_FILE
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip().rstrip("/")
    return text or None


def fallback_worker_urls() -> list[str]:
    """Known hassio / compose hostnames."""
    urls = [DEFAULT_WORKER_URL.rstrip("/")]
    for host in WORKER_HOST_FALLBACKS:
        urls.append(f"http://{host}:8765")
    # Deduplicate while preserving order
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def worker_url_candidates(
    hass: HomeAssistant, entry: ConfigEntry | None = None
) -> list[str]:
    """Ordered candidate bases for probing."""
    configured = ""
    if entry is not None:
        configured = normalize_worker_base(entry.data.get(CONF_WORKER_URL) or "")

    file_url = read_worker_url_file(hass)
    candidates: list[str] = []

    # 1) Explicit non-IP override from config entry
    if configured and not is_ipv4_worker_url(configured):
        candidates.append(configured)

    # 2) Fresh file from worker / deploy
    if file_url:
        candidates.append(file_url)

    # 3) Legacy sticky IPv4 from config entry
    if configured and is_ipv4_worker_url(configured):
        candidates.append(configured)

    # 4) Hostname fallbacks
    candidates.extend(fallback_worker_urls())

    seen: set[str] = set()
    out: list[str] = []
    for u in candidates:
        n = normalize_worker_base(u)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def should_persist_worker_url(hass: HomeAssistant, worker_url: str) -> str:
    """Return value to store in entry data.

    Blank means auto (file + DNS). Only persist explicit user overrides
    that are not the file contents and not a known fallback hostname.
    """
    chosen = normalize_worker_base(worker_url)
    if not chosen:
        return ""

    file_url = read_worker_url_file(hass)
    if file_url and chosen == normalize_worker_base(file_url):
        return ""

    if chosen in fallback_worker_urls():
        return ""

    # Sticky IPs should not be persisted as permanent truth
    if is_ipv4_worker_url(chosen):
        return ""

    return chosen


async def async_probe_worker(
    hass: HomeAssistant,
    base: str,
    *,
    require_playwright: bool = False,
    timeout: aiohttp.ClientTimeout | None = None,
) -> dict[str, Any]:
    """GET ``{base}/health``. Raise ``ValueError`` if unreachable/unhealthy."""
    session = async_get_clientsession(hass)
    url = f"{normalize_worker_base(base)}/health"
    try:
        async with session.get(url, timeout=timeout or PROBE_TIMEOUT) as resp:
            if resp.status != 200:
                raise ValueError(f"Worker health returned HTTP {resp.status}")
            health = await resp.json(content_type=None)
    except aiohttp.ClientError as err:
        raise ValueError(
            f"Cannot reach scraper worker at {normalize_worker_base(base)}. "
            "Is the duke_scraper_worker container running on the hassio network?"
        ) from err

    if not isinstance(health, dict):
        raise ValueError("Worker health returned non-JSON body")
    if health.get("ok") is False:
        raise ValueError(health.get("error") or "Worker health reported not ok")
    if require_playwright and not health.get("playwright_ready"):
        raise ValueError("Worker is up but Playwright/Chromium is not ready yet")
    return health


async def async_resolve_worker_url(
    hass: HomeAssistant,
    entry: ConfigEntry | None = None,
    *,
    require_playwright: bool = False,
) -> str:
    """Return the first healthy worker base URL.

    Raises ``ValueError`` listing tried candidates when none succeed.
    """
    candidates = await hass.async_add_executor_job(worker_url_candidates, hass, entry)
    errors: list[str] = []
    for base in candidates:
        try:
            await async_probe_worker(
                hass, base, require_playwright=require_playwright
            )
        except ValueError as err:
            errors.append(f"{base}: {err}")
            _LOGGER.debug("Worker candidate failed %s: %s", base, err)
            continue
        _LOGGER.info("Duke scraper worker resolved to %s", base)
        return base

    detail = "; ".join(errors) if errors else "no candidates"
    raise ValueError(
        "No reachable Duke scraper worker. "
        f"Tried: {', '.join(candidates) or '(none)'}. ({detail})"
    )


def entry_has_sticky_ipv4(entry: ConfigEntry) -> bool:
    """True when config entry stores an IPv4 worker_url."""
    configured = normalize_worker_base(entry.data.get(CONF_WORKER_URL) or "")
    return bool(configured and is_ipv4_worker_url(configured))
