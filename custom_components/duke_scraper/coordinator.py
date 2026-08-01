"""Coordinator: scrape Duke hourly usage and insert external statistics."""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta
from typing import Any, cast

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from pathlib import Path

from .const import (
    BACKFILL_DONE_KEY,
    BACKFILL_END_YEAR,
    BACKFILL_START_YEAR,
    CONF_EMAIL,
    CONF_METER_SERIAL,
    CONF_PASSWORD,
    CONF_WORKER_URL,
    DATA_DIR_NAME,
    DEFAULT_METER_SERIAL,
    DEFAULT_WORKER_URL,
    DOMAIN,
    LOOKBACK_DAYS,
    NOTIFICATION_MFA_ID,
    UPDATE_INTERVAL_HOURS,
    UPDATE_JITTER_MINUTES,
    WEB_MFA_OK_KEY,
    WORKER_URL_FILE,
)

_LOGGER = logging.getLogger(__name__)


def _random_update_interval() -> timedelta:
    """Return ~6h with ±jitter so polls aren't on a fixed clock."""
    base = UPDATE_INTERVAL_HOURS * 60
    jitter = UPDATE_JITTER_MINUTES
    return timedelta(minutes=random.randint(base - jitter, base + jitter))


class DukeScraperCoordinator(DataUpdateCoordinator[None]):
    """Poll the Playwright worker and write energy statistics."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Duke Energy Scraper",
            update_interval=_random_update_interval(),
        )
        self.entry = entry
        self._statistic_ids: set[str] = set()
        self._worker_url_cache: str | None = None

        @callback
        def _dummy_listener() -> None:
            pass

        # Keep the coordinator polling even with no entity listeners.
        self.async_add_listener(_dummy_listener)

    @property
    def meter_serial(self) -> str:
        return (
            self.entry.data.get(CONF_METER_SERIAL) or DEFAULT_METER_SERIAL
        ).strip()

    @property
    def statistic_id(self) -> str:
        return f"{DOMAIN}:electric_{self.meter_serial}_energy_consumption"

    def _resolve_worker_url(self) -> str:
        configured = (self.entry.data.get(CONF_WORKER_URL) or "").strip()
        if configured:
            return configured.rstrip("/")
        url_file = Path(self.hass.config.path(DATA_DIR_NAME)) / WORKER_URL_FILE
        if url_file.is_file():
            return url_file.read_text(encoding="utf-8").strip().rstrip("/")
        return DEFAULT_WORKER_URL.rstrip("/")

    async def _async_worker_url(self) -> str:
        if self._worker_url_cache:
            return self._worker_url_cache
        url = await self.hass.async_add_executor_job(self._resolve_worker_url)
        self._worker_url_cache = url
        return url

    async def _async_update_data(self) -> None:
        """Fetch usage and insert statistics."""
        try:
            try:
                usage = await self._async_fetch_usage()
            except Exception as err:
                raise UpdateFailed(str(err)) from err

            if not usage:
                _LOGGER.debug("No usage rows returned")
                return

            await self._async_insert_statistics(usage)

            if not self.entry.data.get(BACKFILL_DONE_KEY):
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={**self.entry.data, BACKFILL_DONE_KEY: True},
                )
                _LOGGER.info("Duke scraper first-run backfill marked complete")
        finally:
            # Reschedule next poll ~6h from now with fresh random jitter.
            self.update_interval = _random_update_interval()
            _LOGGER.info("Next Duke scrape scheduled in %s", self.update_interval)

    async def _async_fetch_usage(self) -> dict[datetime, float]:
        """Call worker /export for the appropriate date range."""
        tz = await dt_util.async_get_time_zone("America/New_York")
        now = dt_util.now(tz)
        end = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)

        if not self.entry.data.get(BACKFILL_DONE_KEY):
            start = datetime(BACKFILL_START_YEAR, 1, 1, tzinfo=tz)
            end_cap = datetime(BACKFILL_END_YEAR, 12, 31, tzinfo=tz)
            end = min(end, end_cap)
            mode = "backfill"
            interval = "hourly"
        else:
            start = end - timedelta(days=LOOKBACK_DAYS)
            mode = "incremental"
            # Post-backfill: Green Button XML with true 15-minute intervals
            interval = "fifteen_minute"

        _LOGGER.info(
            "Duke scraper %s (%s) fetch %s → %s via %s",
            mode,
            interval,
            start.date(),
            end.date(),
            await self._async_worker_url(),
        )

        session = async_get_clientsession(self.hass)
        payload = {
            "email": self.entry.data[CONF_EMAIL],
            "password": self.entry.data[CONF_PASSWORD],
            "meter_serial": self.meter_serial,
            "start": start.date().isoformat(),
            "end": end.date().isoformat(),
            "interval": interval,
        }
        timeout = aiohttp.ClientTimeout(
            total=60 * 45 if mode == "backfill" else 60 * 15
        )
        async with session.post(
            f"{await self._async_worker_url()}/export", json=payload, timeout=timeout
        ) as resp:
            body = await resp.json(content_type=None)
            if resp.status == 401 or body.get("error_code") == "mfa_required":
                await self._async_handle_mfa_required(
                    body.get("error") or "Duke web MFA required"
                )
                raise UpdateFailed(body.get("error") or "MFA required")
            if resp.status != 200 or not body.get("ok"):
                err = body.get("error") or f"Worker HTTP {resp.status}"
                if "mfa" in err.lower() or "web session" in err.lower():
                    await self._async_handle_mfa_required(err)
                raise UpdateFailed(err)

        rows: dict[datetime, float] = {}
        fifteen = interval == "fifteen_minute"
        for item in body.get("hours") or []:
            # item: {"start": "ISO8601", "kwh": float}
            stamp = dt_util.parse_datetime(item["start"])
            if stamp is None:
                continue
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=tz)
            else:
                stamp = stamp.astimezone(tz)
            if fifteen:
                minute = (stamp.minute // 15) * 15
                stamp = stamp.replace(minute=minute, second=0, microsecond=0)
            else:
                stamp = stamp.replace(minute=0, second=0, microsecond=0)
            kwh = float(item["kwh"])
            if kwh < 0:
                continue
            rows[stamp] = kwh
        _LOGGER.info(
            "Duke scraper received %s %s points",
            len(rows),
            "fifteen-minute" if fifteen else "hourly",
        )
        return rows

    async def _async_handle_mfa_required(self, detail: str) -> None:
        """Notify user and open reauth so they can request/enter a new MFA code."""
        _LOGGER.warning("Duke scraper MFA required: %s", detail)
        already_flagged = self.entry.data.get(WEB_MFA_OK_KEY) is False
        if not already_flagged:
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, WEB_MFA_OK_KEY: False},
            )
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": NOTIFICATION_MFA_ID,
                    "title": "Duke Energy MFA required",
                    "message": (
                        "The Duke Energy web session expired (usually after ~30 days). "
                        "Open **Settings → Devices & services → Duke Energy Scraper → "
                        "Configure** (or Reauthenticate) to request a new email code "
                        "and resume 15-minute usage downloads.\n\n"
                        f"Detail: {detail}"
                    ),
                },
                blocking=False,
            )
            self.entry.async_start_reauth(self.hass)

    async def _async_insert_statistics(self, usage: dict[datetime, float]) -> None:
        """Write sum-increasing external statistics for Energy dashboard."""
        consumption_statistic_id = self.statistic_id
        self._statistic_ids.add(consumption_statistic_id)

        last_stat = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics,
            self.hass,
            1,
            consumption_statistic_id,
            True,  # noqa: FBT003
            set(),
        )

        if not last_stat:
            consumption_sum = 0.0
            last_stats_time = None
        else:
            # Use 5-minute period so 15-minute starts are visible when continuing sums.
            period = "5minute"
            first = min(usage.keys())
            if first.minute == 0 and all(k.minute == 0 for k in list(usage)[:24]):
                period = "hour"
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                min(usage.keys()),
                None,
                {consumption_statistic_id},
                period,
                None,
                {"sum"},
            )
            if consumption_statistic_id in stats and stats[consumption_statistic_id]:
                consumption_sum = cast(
                    "float", stats[consumption_statistic_id][0]["sum"]
                )
                last_stats_time = stats[consumption_statistic_id][0]["start"]
            else:
                # Overlap window empty — continue from last absolute sum
                consumption_sum = cast(
                    "float", last_stat[consumption_statistic_id][0]["sum"]
                )
                last_stats_time = last_stat[consumption_statistic_id][0]["start"]

        consumption_statistics: list[StatisticData] = []
        for start in sorted(usage.keys()):
            if last_stats_time is not None and start.timestamp() <= last_stats_time:
                continue
            consumption_sum += usage[start]
            consumption_statistics.append(
                StatisticData(start=start, state=usage[start], sum=consumption_sum)
            )

        if not consumption_statistics:
            _LOGGER.debug("No new statistic rows to insert")
            return

        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Duke Energy Electric {self.meter_serial} Consumption",
            source=DOMAIN,
            statistic_id=consumption_statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        _LOGGER.info(
            "Inserting %s statistics into %s",
            len(consumption_statistics),
            consumption_statistic_id,
        )
        async_add_external_statistics(self.hass, metadata, consumption_statistics)
