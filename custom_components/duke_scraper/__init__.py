"""Duke Energy Scraper integration setup."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import DukeScraperCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []

type DukeScraperConfigEntry = ConfigEntry[DukeScraperCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DukeScraperConfigEntry) -> bool:
    """Set up Duke Energy Scraper from a config entry.

    First refresh runs in a background task so a multi-month backfill cannot
    cancel config-entry setup (HA shows "Unknown error occurred").
    """
    coordinator = DukeScraperCoordinator(hass, entry)
    entry.runtime_data = coordinator

    async def _initial_refresh() -> None:
        try:
            await coordinator.async_refresh()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Duke scraper initial refresh failed")

    entry.async_create_background_task(
        hass, _initial_refresh(), "duke_scraper_initial_refresh"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: DukeScraperConfigEntry) -> bool:
    """Unload a config entry (statistics are intentionally kept)."""
    return True


async def async_reload_entry(hass: HomeAssistant, entry: DukeScraperConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)
