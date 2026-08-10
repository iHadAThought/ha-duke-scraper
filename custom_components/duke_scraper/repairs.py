"""Repairs flows for Duke Energy Scraper."""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN, ISSUE_MFA_REQUIRED, NOTIFICATION_MFA_ID


def clear_mfa_alerts(hass: HomeAssistant) -> None:
    """Remove the MFA repair issue and dismiss the matching notification."""
    ir.async_delete_issue(hass, DOMAIN, ISSUE_MFA_REQUIRED)
    hass.async_create_task(
        hass.services.async_call(
            "persistent_notification",
            "dismiss",
            {"notification_id": NOTIFICATION_MFA_ID},
        )
    )


class MfaRequiredRepairFlow(RepairsFlow):
    """Open the config-entry reauth flow so the user can complete MFA."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Handle the first step of a fix flow."""
        return await self.async_step_start_reauth()

    async def async_step_start_reauth(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Confirm, then start reauthentication for the config entry."""
        if user_input is not None:
            entry_id = (self.data or {}).get("entry_id")
            if isinstance(entry_id, str):
                entry = self.hass.config_entries.async_get_entry(entry_id)
                if entry is not None:
                    entry.async_start_reauth(self.hass)
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="start_reauth",
            data_schema=vol.Schema({}),
        )


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Create a repair flow for a registered issue."""
    if issue_id == ISSUE_MFA_REQUIRED:
        return MfaRequiredRepairFlow()
    return ConfirmRepairFlow()
