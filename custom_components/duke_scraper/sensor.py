"""Billing sensors for Duke Energy Scraper."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CURRENCY_DOLLAR
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
    async_add_entities(
        [
            DukeBillingRateSensor(coordinator, entry),
            DukeBillingCurrentBillSensor(coordinator, entry),
            DukeBillingEstimatedBillSensor(coordinator, entry),
            DukeBillingDueDateSensor(coordinator, entry),
            DukeBillingStatusSensor(coordinator, entry),
        ]
    )


class _DukeBillingSensor(CoordinatorEntity[DukeScraperCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DukeScraperCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Duke Energy",
            "model": "My Account scraper",
        }

    @property
    def available(self) -> bool:
        billing = self.coordinator.billing or self.coordinator.data or {}
        return super().available and billing.get(self._key) is not None

    def _billing(self) -> dict[str, Any]:
        return self.coordinator.billing or self.coordinator.data or {}


class DukeBillingRateSensor(_DukeBillingSensor):
    _attr_name = "Energy rate"
    _attr_native_unit_of_measurement = f"{CURRENCY_DOLLAR}/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:currency-usd"

    def __init__(self, coordinator: DukeScraperCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "energy_rate_usd_per_kwh")

    @property
    def native_value(self) -> float | None:
        val = self._billing().get(self._key)
        return float(val) if val is not None else None


class DukeBillingCurrentBillSensor(_DukeBillingSensor):
    _attr_name = "Current bill"
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:receipt-text"

    def __init__(self, coordinator: DukeScraperCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "current_bill_usd")

    @property
    def native_value(self) -> float | None:
        val = self._billing().get(self._key)
        return float(val) if val is not None else None


class DukeBillingEstimatedBillSensor(_DukeBillingSensor):
    _attr_name = "Estimated bill"
    _attr_native_unit_of_measurement = CURRENCY_DOLLAR
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL
    _attr_icon = "mdi:receipt-text-outline"

    def __init__(self, coordinator: DukeScraperCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "estimated_bill_usd")

    @property
    def native_value(self) -> float | None:
        val = self._billing().get(self._key)
        return float(val) if val is not None else None


class DukeBillingDueDateSensor(_DukeBillingSensor):
    _attr_name = "Bill due date"
    _attr_device_class = SensorDeviceClass.DATE
    _attr_icon = "mdi:calendar-clock"

    def __init__(self, coordinator: DukeScraperCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "bill_due_date")

    @property
    def native_value(self):
        val = self._billing().get(self._key)
        if not val:
            return None
        from datetime import date

        if isinstance(val, date):
            return val
        try:
            return date.fromisoformat(str(val)[:10])
        except ValueError:
            return None


class DukeBillingStatusSensor(_DukeBillingSensor):
    _attr_name = "Billing status"
    _attr_icon = "mdi:file-document-alert"

    def __init__(self, coordinator: DukeScraperCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "billing_status")

    @property
    def available(self) -> bool:
        billing = self._billing()
        return super(CoordinatorEntity, self).available and bool(
            billing.get(self._key) or billing.get("ok") is not None
        )

    @property
    def native_value(self) -> str | None:
        billing = self._billing()
        return billing.get(self._key) or (
            "unknown" if billing else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        billing = self._billing()
        attrs: dict[str, Any] = {}
        for key in (
            "billing_message",
            "fetched_at",
            "period_start",
            "period_end",
            "last_bill_date",
            "past_due",
            "raw_keys",
        ):
            if key in billing and billing[key] is not None:
                attrs[key] = billing[key]
        return attrs
