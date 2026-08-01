"""Duke Energy Scraper integration setup."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import default_options
from .coordinator import DukeScraperCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = []

type DukeScraperConfigEntry = ConfigEntry[DukeScraperCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: DukeScraperConfigEntry) -> bool:
    """Set up Duke Energy Scraper from a config entry.

    First refresh runs in a background task so a multi-month backfill cannot
    cancel config-entry setup (HA shows "Unknown error occurred").
    """
    if not entry.options:
        hass.config_entries.async_update_entry(entry, options=default_options())

    coordinator = DukeScraperCoordinator(hass, entry)
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

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
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(hass: HomeAssistant, entry: DukeScraperConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate config entry to current version (fill default options)."""
    _LOGGER.info("Migrating duke_scraper entry from v%s", entry.version)
    if entry.version < 2:
        hass.config_entries.async_update_entry(
            entry,
            options={**default_options(), **(entry.options or {})},
            version=2,
        )
    return True
