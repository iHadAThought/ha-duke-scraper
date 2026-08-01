"""Binary sensors for Duke Energy Scraper billing status."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DukeScraperCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DukeScraperCoordinator = entry.runtime_data
    async_add_entities([DukeBillPastDueBinarySensor(coordinator, entry)])


class DukeBillPastDueBinarySensor(
    CoordinatorEntity[DukeScraperCoordinator], BinarySensorEntity
):
    _attr_has_entity_name = True
    _attr_name = "Bill past due"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-circle"

    def __init__(
        self, coordinator: DukeScraperCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_bill_past_due"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Duke Energy",
            "model": "My Account scraper",
        }

    def _billing(self) -> dict:
        return self.coordinator.billing or self.coordinator.data or {}

    @property
    def available(self) -> bool:
        billing = self._billing()
        return super().available and (
            billing.get("past_due") is not None
            or billing.get("billing_status") in {"past_due", "ok", "pending"}
        )

    @property
    def is_on(self) -> bool | None:
        billing = self._billing()
        if billing.get("past_due") is not None:
            return bool(billing.get("past_due"))
        status = str(billing.get("billing_status") or "").lower()
        if status in {"past_due", "late", "delinquent"}:
            return True
        if status in {"ok", "current", "paid"}:
            return False
        return None
