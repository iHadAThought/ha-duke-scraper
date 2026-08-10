"""Config flow for Duke Energy Scraper (credentials → preferences → MFA)."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    BACKFILL_DAY_CHOICES,
    CONF_BACKFILL_DAYS,
    CONF_EMAIL,
    CONF_INTERVAL,
    CONF_METER_SERIAL,
    CONF_MFA_CODE,
    CONF_REQUEST_CODE,
    CONF_UPDATE_MINUTES,
    CONF_USE_PASSKEY,
    CONF_WORKER_URL,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_INTERVAL,
    DEFAULT_METER_SERIAL,
    DEFAULT_UPDATE_MINUTES,
    DEFAULT_USE_PASSKEY,
    DOMAIN,
    INTERVAL_CHOICES,
    UPDATE_MINUTE_CHOICES,
    WEB_MFA_OK_KEY,
    default_options,
    option,
)
from .repairs import clear_mfa_alerts
from .worker_url import (
    async_probe_worker,
    normalize_worker_base,
    read_worker_url_file,
    should_persist_worker_url,
)

_LOGGER = logging.getLogger(__name__)


def _default_worker_url(hass: HomeAssistant | None = None) -> str:
    """Sync helper — display default for forms (file or blank for auto)."""
    if hass is not None:
        file_url = read_worker_url_file(hass)
        if file_url:
            return file_url
    return ""


async def _async_default_worker_url(hass: HomeAssistant) -> str:
    return await hass.async_add_executor_job(_default_worker_url, hass)


async def _worker_post(
    hass: HomeAssistant,
    worker_url: str,
    path: str,
    payload: dict[str, Any],
    timeout: int = 120,
) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    base = normalize_worker_base(worker_url)
    async with session.post(
        f"{base}{path}",
        json=payload,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        body = await resp.json(content_type=None)
        if resp.status >= 400 or not body.get("ok", True):
            raise ValueError(body.get("error") or f"Worker HTTP {resp.status}")
        return body


async def _validate_api_login(
    hass: HomeAssistant, worker_url: str, email: str, password: str
) -> dict[str, Any]:
    """Hit worker health + Auth0/CMA token validate (not web MFA)."""
    base = normalize_worker_base(worker_url)
    await async_probe_worker(
        hass,
        base,
        require_playwright=True,
        timeout=aiohttp.ClientTimeout(total=15),
    )
    return await _worker_post(
        hass,
        worker_url,
        "/validate",
        {"email": email, "password": password},
        timeout=120,
    )


async def _ensure_worker_reachable(hass: HomeAssistant, worker_url: str) -> None:
    """Fast check used when updating password (skip slow Auth0 validate)."""
    await async_probe_worker(
        hass,
        worker_url,
        require_playwright=True,
        timeout=aiohttp.ClientTimeout(total=10),
    )


async def _mfa_start(
    hass: HomeAssistant,
    worker_url: str,
    email: str,
    password: str,
    *,
    use_passkey: bool = True,
) -> dict[str, Any]:
    return await _worker_post(
        hass,
        worker_url,
        "/mfa/start",
        {"email": email, "password": password, "use_passkey": use_passkey},
        timeout=120,
    )


async def _mfa_complete(
    hass: HomeAssistant,
    worker_url: str,
    code: str,
    *,
    use_passkey: bool = True,
) -> dict[str, Any]:
    return await _worker_post(
        hass,
        worker_url,
        "/mfa/complete",
        {"mfa_code": code, "use_passkey": use_passkey},
        timeout=90,
    )


def _preferences_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = {**default_options(), **(defaults or {})}
    return vol.Schema(
        {
            vol.Required(
                CONF_USE_PASSKEY, default=bool(d[CONF_USE_PASSKEY])
            ): bool,
            vol.Required(
                CONF_BACKFILL_DAYS, default=str(d[CONF_BACKFILL_DAYS])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": k, "label": v}
                        for k, v in BACKFILL_DAY_CHOICES.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_INTERVAL, default=str(d[CONF_INTERVAL])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": k, "label": v} for k, v in INTERVAL_CHOICES.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_UPDATE_MINUTES, default=int(d[CONF_UPDATE_MINUTES])
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": str(k), "label": v}
                        for k, v in UPDATE_MINUTE_CHOICES.items()
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _normalize_preferences(user_input: dict[str, Any]) -> dict[str, Any]:
    minutes = user_input.get(CONF_UPDATE_MINUTES, DEFAULT_UPDATE_MINUTES)
    if isinstance(minutes, str):
        minutes = int(minutes)
    backfill = str(user_input.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS))
    interval = str(user_input.get(CONF_INTERVAL, DEFAULT_INTERVAL))
    if minutes not in UPDATE_MINUTE_CHOICES:
        minutes = DEFAULT_UPDATE_MINUTES
    if minutes < 30:
        minutes = 30
    if backfill not in BACKFILL_DAY_CHOICES:
        backfill = DEFAULT_BACKFILL_DAYS
    if interval not in INTERVAL_CHOICES:
        interval = DEFAULT_INTERVAL
    return {
        CONF_USE_PASSKEY: bool(
            user_input.get(CONF_USE_PASSKEY, DEFAULT_USE_PASSKEY)
        ),
        CONF_BACKFILL_DAYS: backfill,
        CONF_INTERVAL: interval,
        CONF_UPDATE_MINUTES: int(minutes),
    }


class DukeScraperConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Duke Energy Scraper."""

    VERSION = 2

    def __init__(self) -> None:
        self._email: str = ""
        self._password: str = ""
        self._meter: str = DEFAULT_METER_SERIAL
        self._worker: str = ""
        self._mfa_hint: str = ""
        self._options: dict[str, Any] = default_options()
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Credentials step."""
        errors: dict[str, str] = {}
        default_worker = await _async_default_worker_url(self.hass)

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            worker = normalize_worker_base(
                user_input.get(CONF_WORKER_URL) or default_worker
            )
            if not worker:
                errors["base"] = "cannot_connect"
                self.context["last_error"] = (
                    "No worker URL available. Start the Duke scraper worker "
                    "(writes /config/.duke_scraper/worker_url) or enter a URL."
                )
            else:
                try:
                    await _validate_api_login(
                        self.hass, worker, email, user_input[CONF_PASSWORD]
                    )
                except ValueError as err:
                    _LOGGER.warning("Duke scraper validation failed: %s", err)
                    errors["base"] = "cannot_connect"
                    self.context["last_error"] = str(err)
                else:
                    self._email = email
                    self._password = user_input[CONF_PASSWORD]
                    self._meter = (
                        user_input.get(CONF_METER_SERIAL) or DEFAULT_METER_SERIAL
                    ).strip()
                    self._worker = worker
                    return await self.async_step_preferences()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_EMAIL): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_METER_SERIAL, default=DEFAULT_METER_SERIAL): str,
                    vol.Optional(CONF_WORKER_URL, default=default_worker): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "hint": self.context.get("last_error") or "",
            },
        )

    async def async_step_preferences(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Scraping preferences (before MFA so passkey opt-in is known)."""
        if user_input is not None:
            self._options = _normalize_preferences(user_input)
            return await self.async_step_mfa()

        return self.async_show_form(
            step_id="preferences",
            data_schema=_preferences_schema(self._options),
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """MFA: request email code and/or submit it."""
        errors: dict[str, str] = {}
        use_passkey = bool(self._options.get(CONF_USE_PASSKEY, DEFAULT_USE_PASSKEY))

        if user_input is not None:
            request_code = bool(user_input.get(CONF_REQUEST_CODE))
            code = (user_input.get(CONF_MFA_CODE) or "").strip()

            if request_code or not code:
                try:
                    result = await _mfa_start(
                        self.hass,
                        self._worker,
                        self._email,
                        self._password,
                        use_passkey=use_passkey,
                    )
                except ValueError as err:
                    _LOGGER.warning("MFA request failed: %s", err)
                    errors["base"] = "mfa_request_failed"
                    self._mfa_hint = str(err)
                else:
                    if result.get("status") == "already_authenticated":
                        return await self._async_finish(web_mfa_ok=True)
                    self._mfa_hint = (
                        "Code sent to your Duke Energy email. "
                        "Enter it below (check spam). Code expires in a few minutes."
                    )
                    return self.async_show_form(
                        step_id="mfa",
                        data_schema=self._mfa_schema(),
                        errors=errors,
                        description_placeholders={"hint": self._mfa_hint},
                    )
            else:
                try:
                    await _mfa_complete(
                        self.hass,
                        self._worker,
                        code,
                        use_passkey=use_passkey,
                    )
                except ValueError as err:
                    _LOGGER.warning("MFA verify failed: %s", err)
                    errors["base"] = "invalid_mfa_code"
                    self._mfa_hint = str(err)
                else:
                    return await self._async_finish(web_mfa_ok=True)

        if user_input is None and not self._mfa_hint:
            try:
                result = await _mfa_start(
                    self.hass,
                    self._worker,
                    self._email,
                    self._password,
                    use_passkey=use_passkey,
                )
                if result.get("status") == "already_authenticated":
                    return await self._async_finish(web_mfa_ok=True)
                self._mfa_hint = (
                    "A verification code was emailed to your Duke Energy account. "
                    "Enter it below, or check Request code to send another."
                )
            except ValueError as err:
                self._mfa_hint = (
                    f"Could not auto-send code ({err}). "
                    "Check Request code and submit to try again."
                )

        return self.async_show_form(
            step_id="mfa",
            data_schema=self._mfa_schema(),
            errors=errors,
            description_placeholders={"hint": self._mfa_hint or ""},
        )

    def _mfa_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Optional(CONF_MFA_CODE): str,
                vol.Optional(CONF_REQUEST_CODE, default=False): bool,
            }
        )

    async def _async_finish(self, *, web_mfa_ok: bool) -> FlowResult:
        data = {
            CONF_EMAIL: self._email,
            CONF_PASSWORD: self._password,
            CONF_METER_SERIAL: self._meter,
            CONF_WORKER_URL: should_persist_worker_url(self.hass, self._worker),
            WEB_MFA_OK_KEY: web_mfa_ok,
        }
        options = {**default_options(), **self._options}
        if web_mfa_ok:
            clear_mfa_alerts(self.hass)
        if self._reauth_entry:
            return self.async_update_reload_and_abort(
                self._reauth_entry,
                data_updates=data,
                options=options,
            )
        return self.async_create_entry(
            title=f"Duke Energy ({self._email})",
            data=data,
            options=options,
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Reauth when web MFA session expires (~30 days)."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        assert entry is not None
        self._reauth_entry = entry
        self._email = entry.data[CONF_EMAIL]
        self._password = entry.data[CONF_PASSWORD]
        self._meter = entry.data.get(CONF_METER_SERIAL) or DEFAULT_METER_SERIAL
        self._worker = normalize_worker_base(
            entry.data.get(CONF_WORKER_URL) or await _async_default_worker_url(self.hass)
        )
        self._options = {**default_options(), **(entry.options or {})}
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Confirm password (update if needed), then MFA."""
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            worker = normalize_worker_base(
                user_input.get(CONF_WORKER_URL) or self._worker
            )
            try:
                await _ensure_worker_reachable(self.hass, worker)
            except ValueError as err:
                errors["base"] = "cannot_connect"
                self.context["last_error"] = str(err)
            else:
                self._password = password
                self._worker = worker
                self._mfa_hint = (
                    "Password saved. Check Request code, submit, then enter the "
                    "email MFA code."
                )
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_WORKER_URL, default=self._worker): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "email": self._email,
                "hint": self.context.get("last_error") or "",
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Reconfigure credentials, then preferences, then MFA."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        default_worker = normalize_worker_base(
            entry.data.get(CONF_WORKER_URL)
            or await _async_default_worker_url(self.hass)
        )
        self._options = {**default_options(), **(entry.options or {})}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            worker = normalize_worker_base(
                user_input.get(CONF_WORKER_URL) or default_worker
            )
            try:
                await _validate_api_login(
                    self.hass, worker, email, user_input[CONF_PASSWORD]
                )
            except ValueError as err:
                errors["base"] = "cannot_connect"
                self.context["last_error"] = str(err)
            else:
                self._reauth_entry = entry
                self._email = email
                self._password = user_input[CONF_PASSWORD]
                self._meter = (
                    user_input.get(CONF_METER_SERIAL)
                    or entry.data.get(CONF_METER_SERIAL)
                    or DEFAULT_METER_SERIAL
                ).strip()
                self._worker = worker
                self._mfa_hint = ""
                return await self.async_step_preferences()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_EMAIL, default=entry.data.get(CONF_EMAIL, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(
                        CONF_METER_SERIAL,
                        default=entry.data.get(CONF_METER_SERIAL, DEFAULT_METER_SERIAL),
                    ): str,
                    vol.Optional(CONF_WORKER_URL, default=default_worker): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "hint": self.context.get("last_error") or "",
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return DukeScraperOptionsFlow()


class DukeScraperOptionsFlow(config_entries.OptionsFlow):
    """Options: preferences and/or credentials + MFA."""

    def __init__(self) -> None:
        self._password: str | None = None
        self._worker: str | None = None
        self._mfa_hint = (
            "Update your password if needed, then request/enter an MFA code to "
            "refresh the web session."
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Choose preferences or credentials."""
        if user_input is not None:
            if user_input.get("next") == "credentials":
                return await self.async_step_credentials()
            return await self.async_step_preferences()

        return self.async_show_menu(
            step_id="init",
            menu_options=["preferences", "credentials"],
        )

    async def async_step_preferences(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.config_entry
        current = {**default_options(), **(entry.options or {})}
        if user_input is not None:
            options = _normalize_preferences(user_input)
            return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="preferences",
            data_schema=_preferences_schema(current),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        default_worker = normalize_worker_base(
            entry.data.get(CONF_WORKER_URL)
            or await _async_default_worker_url(self.hass)
        )

        if user_input is not None:
            password = (user_input.get(CONF_PASSWORD) or "").strip()
            if not password:
                password = entry.data[CONF_PASSWORD]
            worker = normalize_worker_base(
                user_input.get(CONF_WORKER_URL) or default_worker
            )
            try:
                await _ensure_worker_reachable(self.hass, worker)
            except ValueError as err:
                errors["base"] = "cannot_connect"
                self._mfa_hint = str(err)
            else:
                self._password = password
                self._worker = worker
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_PASSWORD: password,
                        CONF_WORKER_URL: should_persist_worker_url(self.hass, worker),
                    },
                )
                self._mfa_hint = (
                    "Password saved. Check Request code, submit, then enter the "
                    "email MFA code — or check Skip MFA if you only updated the password."
                )
                return await self.async_step_mfa()

        return self.async_show_form(
            step_id="credentials",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_PASSWORD): str,
                    vol.Optional(CONF_WORKER_URL, default=default_worker): str,
                }
            ),
            errors=errors,
            description_placeholders={
                "email": entry.data.get(CONF_EMAIL, ""),
                "hint": self._mfa_hint,
            },
        )

    async def async_step_mfa(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        email = entry.data[CONF_EMAIL]
        password = self._password or entry.data[CONF_PASSWORD]
        worker = normalize_worker_base(
            self._worker
            or entry.data.get(CONF_WORKER_URL)
            or await _async_default_worker_url(self.hass)
        )
        use_passkey = bool(option(entry, CONF_USE_PASSKEY, DEFAULT_USE_PASSKEY))

        if user_input is not None:
            request_code = bool(user_input.get(CONF_REQUEST_CODE))
            code = (user_input.get(CONF_MFA_CODE) or "").strip()
            skip = bool(user_input.get("skip_mfa"))
            if skip:
                return self.async_create_entry(
                    title="", data={**default_options(), **(entry.options or {})}
                )
            if request_code or not code:
                try:
                    result = await _mfa_start(
                        self.hass,
                        worker,
                        email,
                        password,
                        use_passkey=use_passkey,
                    )
                except ValueError as err:
                    errors["base"] = "mfa_request_failed"
                    self._mfa_hint = str(err)
                else:
                    if result.get("status") == "already_authenticated":
                        self.hass.config_entries.async_update_entry(
                            entry,
                            data={**entry.data, WEB_MFA_OK_KEY: True},
                        )
                        clear_mfa_alerts(self.hass)
                        return self.async_create_entry(
                            title="",
                            data={**default_options(), **(entry.options or {})},
                        )
                    self._mfa_hint = (
                        "Code sent. Enter it below, or request another code."
                    )
            else:
                try:
                    await _mfa_complete(
                        self.hass, worker, code, use_passkey=use_passkey
                    )
                except ValueError as err:
                    errors["base"] = "invalid_mfa_code"
                    self._mfa_hint = str(err)
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, WEB_MFA_OK_KEY: True},
                    )
                    clear_mfa_alerts(self.hass)
                    return self.async_create_entry(
                        title="",
                        data={**default_options(), **(entry.options or {})},
                    )

        return self.async_show_form(
            step_id="mfa",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_MFA_CODE): str,
                    vol.Optional(CONF_REQUEST_CODE, default=False): bool,
                    vol.Optional("skip_mfa", default=False): bool,
                }
            ),
            errors=errors,
            description_placeholders={"hint": self._mfa_hint},
        )
