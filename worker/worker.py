#!/usr/bin/env python3
"""Duke Energy Playwright scrape worker.

Logs in via Auth0 Universal Login (mobile PKCE client — same as aiodukeenergy),
exchanges the auth code for tokens, then pulls hourly usage from the CMA API.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
LOG = logging.getLogger("duke_scraper_worker")

DATA_DIR = Path(os.environ.get("DUKE_SCRAPER_DATA", "/data"))
TOKENS_PATH = DATA_DIR / "tokens.json"
WEB_STATE_PATH = DATA_DIR / "web_storage_state.json"
WEBAUTHN_PATH = DATA_DIR / "webauthn_credential.json"
DOWNLOADS_DIR = DATA_DIR / "downloads"
HOST = os.environ.get("DUKE_SCRAPER_HOST", "0.0.0.0")
PORT = int(os.environ.get("DUKE_SCRAPER_PORT", "8765"))
TZ = ZoneInfo("America/New_York")

# Web My Account (dashboard Auth0 client — used for Green Button XML download)
WEB_USAGE_URL = "https://www.duke-energy.com/my-account/usage"
WEB_DASHBOARD_URL = "https://www.duke-energy.com/my-account/dashboard"

API_BASE = "https://api-v2.cma.duke-energy.app"
AUTH_TOKEN_URL = f"{API_BASE}/login/auth-token"
AUTH0_DOMAIN = "login.duke-energy.com"
AUTHORIZE_URL = f"https://{AUTH0_DOMAIN}/authorize"
TOKEN_URL = f"https://{AUTH0_DOMAIN}/oauth/token"
CLIENT_ID = "PitoKqxMh8thrFF8rRlYGrAs3LbSD2dj"
REDIRECT_URI = "https://login.duke-energy.com/ios/com.duke-energy.app/callback"
# CMA API Basic credentials (iOS app — same as aiodukeenergy)
DE_CLIENT_ID = "HO2JKfv2dVuXhLHhleDr1s6fgVlPduGxVBO6GaS3dDjE7Kp8"
DE_CLIENT_SECRET = (
    "g4236o8ROFMD4JuVI4tsgLY7NiIEGXQgzzCnH9RiRrvFC6IN4KFg3A6dBmGIIuW6"
)
DE_BASIC = base64.b64encode(f"{DE_CLIENT_ID}:{DE_CLIENT_SECRET}".encode()).decode()
AUTH0_CLIENT = base64.b64encode(
    json.dumps(
        {
            "env": {"iOS": "26.2", "swift": "6.x"},
            "version": "2.13.0",
            "name": "Auth0.swift",
        }
    ).encode()
).decode()
DATE_FMT = "%m/%d/%Y"

_PLAYWRIGHT_READY = False


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)


def _publish_worker_url() -> None:
    """Write http://host:port to DATA_DIR/worker_url for HA auto-discovery.

    Prefer DUKE_SCRAPER_ADVERTISE_HOST, then Supervisor add-on hostname, then
    the first non-loopback IPv4 (hassio bridge IP for lab containers).
    """
    port = PORT
    host = (os.environ.get("DUKE_SCRAPER_ADVERTISE_HOST") or "").strip()

    if not host and os.environ.get("SUPERVISOR_TOKEN"):
        try:
            req = urllib.request.Request(
                "http://supervisor/addons/self/info",
                headers={
                    "Authorization": f"Bearer {os.environ['SUPERVISOR_TOKEN']}"
                },
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp).get("data") or {}
            raw = str(data.get("hostname") or data.get("slug") or "")
            host = raw.replace("_", "-")
        except Exception as err:
            LOG.warning("Supervisor hostname lookup failed: %s", err)

    if not host:
        try:
            import socket

            # UDP connect does not send packets; reveals the egress interface IP.
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("1.1.1.1", 80))
                host = sock.getsockname()[0]
            finally:
                sock.close()
        except Exception as err:
            LOG.warning("Could not detect container IP: %s", err)

    if not host or host.startswith("127."):
        host = "local-duke-scraper-worker"

    url = f"http://{host}:{port}"
    path = DATA_DIR / "worker_url"
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(url, encoding="utf-8")
        tmp.replace(path)
        LOG.info("Wrote worker_url -> %s", url)
    except Exception as err:
        LOG.warning("Failed to write worker_url: %s", err)


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge


def _authorize_url(code_challenge: str) -> str:
    params = {
        "client_id": CLIENT_ID,
        "scope": "openid profile email offline_access",
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "state": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "nonce": base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "auth0Client": AUTH0_CLIENT,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def _token_request(payload: dict[str, Any]) -> dict[str, Any]:
    headers = {
        "accept-language": "en_US",
        "auth0-client": AUTH0_CLIENT,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": "Duke%20Energy/1241 CFNetwork/3860.300.31 Darwin/25.2.0",
    }
    req = urllib.request.Request(
        TOKEN_URL,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        body = err.read().decode(errors="replace")[:500]
        raise RuntimeError(f"Token request failed {err.code}: {body}") from err


def _exchange_code(code: str, code_verifier: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code_verifier": code_verifier,
            "code": code,
            "redirect_uri": REDIRECT_URI,
        }
    )


def _refresh_tokens(refresh_token: str) -> dict[str, Any]:
    return _token_request(
        {
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
    )


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    pad = "=" * (-len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(parts[1] + pad))
    except Exception:
        return {}


def _token_expired(token: str, skew_seconds: int = 120) -> bool:
    payload = _jwt_payload(token)
    exp = payload.get("exp")
    if not exp:
        return True
    return datetime.fromtimestamp(exp, tz=timezone.utc) <= (
        datetime.now(timezone.utc) + timedelta(seconds=skew_seconds)
    )


def _save_tokens(tokens: dict[str, Any], email: str) -> None:
    payload = {
        "email": email,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        **tokens,
    }
    TOKENS_PATH.write_text(json.dumps(payload))
    LOG.info("Saved tokens to %s", TOKENS_PATH)


def _load_tokens(email: str) -> dict[str, Any] | None:
    if not TOKENS_PATH.exists():
        return None
    try:
        data = json.loads(TOKENS_PATH.read_text())
    except Exception:
        return None
    if data.get("email") and data["email"].lower() != email.lower():
        return None
    return data


def _extract_code(url: str) -> str | None:
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        codes = qs.get("code") or []
        return codes[0] if codes else None
    except Exception:
        return None


def _playwright_login(email: str, password: str) -> dict[str, Any]:
    """Auth0 Universal Login → authorization code → token exchange."""
    verifier, challenge = _pkce_pair()
    auth_url = _authorize_url(challenge)
    captured: dict[str, str | None] = {"url": None}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-dev-shm-usage",
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        def _maybe_capture(url: str) -> None:
            if "code=" in url and "callback" in url:
                captured["url"] = url
                LOG.info("Captured callback URL")

        page.on("framenavigated", lambda fr: _maybe_capture(fr.url))
        page.on("request", lambda req: _maybe_capture(req.url))

        LOG.info("Opening Auth0 authorize URL")
        page.goto(auth_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        # Cookie banner if present
        for sel in ("#onetrust-accept-btn-handler", "button:has-text('Accept')"):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=500):
                    loc.click(timeout=2000)
            except Exception:
                pass

        def _click_primary() -> None:
            """Click Auth0's visible primary button (skip ulp-hidden-form-submit-button)."""
            candidates = [
                "button[data-action-button-primary]:visible",
                "button._button-login-id:visible",
                "button[name='action'][value='default']:visible",
                "button:has-text('Continue'):visible",
                "button:has-text('Next'):visible",
                "button:has-text('Sign In'):visible",
                "button:has-text('Log In'):visible",
                "button[type='submit']:visible",
            ]
            for sel in candidates:
                try:
                    loc = page.locator(sel).first
                    if not loc.count():
                        continue
                    # Skip Auth0's aria-hidden decoy submit
                    hidden = loc.get_attribute("aria-hidden")
                    cls = loc.get_attribute("class") or ""
                    if hidden == "true" or "ulp-hidden-form-submit" in cls:
                        continue
                    if loc.is_visible(timeout=500):
                        loc.click(timeout=8000, no_wait_after=False)
                        return
                except Exception:
                    continue
            # Fallback: submit the focused field with Enter
            page.keyboard.press("Enter")

        # Step 1: email / username
        user = page.locator(
            "input#username, input[name='username'], input[type='email']"
        ).first
        user.wait_for(state="visible", timeout=20000)
        user.click(timeout=5000)
        user.fill(email, timeout=10000)
        LOG.info("Filled username")
        _click_primary()
        page.wait_for_timeout(2000)

        # Step 2: password
        pwd = page.locator("input[type='password']").first
        try:
            pwd.wait_for(state="visible", timeout=20000)
        except Exception as err:
            # CAPTCHA / passkey / error screen
            body = ""
            try:
                body = page.locator("body").inner_text(timeout=2000)[:400]
            except Exception:
                pass
            raise RuntimeError(
                f"Password field not shown (CAPTCHA/MFA?). url={page.url} body≈{body!r}"
            ) from err

        pwd.click(timeout=5000)
        pwd.fill(password, timeout=10000)
        LOG.info("Filled password")
        _click_primary()

        # Wait for redirect with code
        deadline = datetime.now(timezone.utc) + timedelta(seconds=90)
        while datetime.now(timezone.utc) < deadline and not captured["url"]:
            page.wait_for_timeout(500)
            _maybe_capture(page.url)
            # Auth0 error page?
            if "error=" in page.url or "access_denied" in page.url.lower():
                raise RuntimeError(f"Auth0 error redirect: {page.url}")

        browser.close()

    if not captured["url"]:
        raise RuntimeError(
            "Login finished but no authorization code redirect was captured. "
            "Duke may be showing CAPTCHA or MFA."
        )

    code = _extract_code(captured["url"])
    if not code:
        raise RuntimeError(f"Callback missing code: {captured['url']}")

    LOG.info("Exchanging authorization code")
    tokens = _exchange_code(code, verifier)
    if not tokens.get("id_token"):
        raise RuntimeError(f"Token response missing id_token: keys={list(tokens)}")
    _save_tokens(tokens, email)
    return tokens


def _get_tokens(email: str, password: str) -> dict[str, Any]:
    saved = _load_tokens(email)
    if saved and saved.get("id_token") and not _token_expired(saved["id_token"]):
        LOG.info("Using cached id_token")
        return saved
    if saved and saved.get("refresh_token"):
        try:
            LOG.info("Refreshing id_token")
            tokens = _refresh_tokens(saved["refresh_token"])
            # Auth0 may omit refresh_token on refresh — keep old one
            if not tokens.get("refresh_token"):
                tokens["refresh_token"] = saved["refresh_token"]
            # Force re-exchange of CMA API token after Auth0 refresh
            tokens.pop("de_access_token", None)
            tokens.pop("de_expires_at", None)
            _save_tokens(tokens, email)
            return tokens
        except Exception as err:
            LOG.warning("Refresh failed (%s); doing interactive login", err)
    LOG.info("Interactive Auth0 login")
    return _playwright_login(email, password)


def _de_token_expired(tokens: dict[str, Any], skew_seconds: int = 60) -> bool:
    de = tokens.get("de_access_token")
    exp = tokens.get("de_expires_at")
    if not de or not exp:
        return True
    try:
        expiry = datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
    except Exception:
        return True
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= (expiry - timedelta(seconds=skew_seconds))


def _exchange_duke_api_token(id_token: str) -> dict[str, Any]:
    """Exchange Auth0 id_token for CMA API access_token (required for account APIs)."""
    headers = {
        "Authorization": f"Basic {DE_BASIC}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
        "platform": "iOS",
        "User-Agent": "Duke%20Energy/1241 CFNetwork/3860.300.31 Darwin/25.2.0",
    }
    req = urllib.request.Request(
        AUTH_TOKEN_URL,
        data=json.dumps({"idToken": id_token}).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        text = err.read().decode(errors="replace")[:500]
        raise RuntimeError(
            f"Duke API token exchange failed {err.code}: {text}"
        ) from err
    if not data.get("access_token"):
        raise RuntimeError(f"Duke API token response missing access_token: {data}")
    return data


def _ensure_duke_api_token(tokens: dict[str, Any], email: str) -> dict[str, Any]:
    """Ensure tokens dict has a valid CMA de_access_token; persist when refreshed."""
    if not _de_token_expired(tokens):
        return tokens
    LOG.info("Exchanging Auth0 id_token for Duke CMA API token")
    api_auth = _exchange_duke_api_token(tokens["id_token"])
    expires_in = int(api_auth.get("expires_in") or 1800)
    issued_at = api_auth.get("issued_at")
    if issued_at:
        try:
            start = datetime.fromtimestamp(int(issued_at), tz=timezone.utc)
        except Exception:
            start = datetime.now(timezone.utc)
    else:
        start = datetime.now(timezone.utc)
    tokens["de_access_token"] = api_auth["access_token"]
    tokens["de_expires_at"] = (start + timedelta(seconds=expires_in)).isoformat()
    if api_auth.get("internalUserID"):
        tokens["de_internal_user_id"] = api_auth["internalUserID"]
    if api_auth.get("loginEmailAddress"):
        tokens["de_email"] = api_auth["loginEmailAddress"]
    _save_tokens(tokens, email)
    return tokens


def _api_json(
    method: str,
    url: str,
    access_token: str,
    params: dict | None = None,
    body: dict | None = None,
) -> dict[str, Any]:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = None
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "platform": "iOS",
        "User-Agent": "Duke%20Energy/1241 CFNetwork/3860.300.31 Darwin/25.2.0",
    }
    if body is not None:
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as err:
        text = err.read().decode(errors="replace")[:500]
        raise RuntimeError(f"API {method} {url} -> {err.code}: {text}") from err


def _get_meters(
    access_token: str, email: str, internal_user_id: str
) -> list[dict[str, Any]]:
    account_list = _api_json(
        "GET",
        f"{API_BASE}/account-list",
        access_token,
        params={
            "email": email,
            "internalUserID": internal_user_id,
            "fetchFreshData": "true",
        },
    )
    meters: list[dict[str, Any]] = []
    related = account_list.get("relatedBpNumber")
    for account in account_list.get("accounts") or []:
        details = _api_json(
            "GET",
            f"{API_BASE}/account-details-v2",
            access_token,
            params={
                "email": email,
                "srcSysCd": account["srcSysCd"],
                "srcAcctId": account["srcAcctId"],
                "primaryBpNumber": account["primaryBpNumber"],
                "relatedBpNumber": related,
            },
        )
        # Prefer details address for zip
        acct = {k: v for k, v in account.items() if k != "details"}
        if isinstance(details, dict):
            if details.get("serviceAddressParsed"):
                acct["serviceAddressParsed"] = details["serviceAddressParsed"]
        for meter in details.get("meterInfo") or []:
            meters.append({**meter, "account": acct})
    return meters


def _hourly_for_range(
    access_token: str,
    meter: dict[str, Any],
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    account = meter["account"]
    by_start: dict[str, float] = {}
    cursor = start

    def _mdy(s: str) -> str:
        return datetime.strptime(s, "%Y-%m-%d").strftime(DATE_FMT)

    sap = account.get("serviceAddressParsed") or {}
    zip_code = (sap.get("zipCode") if isinstance(sap, dict) else None) or account.get(
        "zipCode"
    ) or "00000"

    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=13), end)
        start_dt = datetime(cursor.year, cursor.month, cursor.day, tzinfo=TZ)
        now = datetime.now(TZ)
        date_iso = now.replace(
            year=start_dt.year, month=start_dt.month, day=start_dt.day
        ).isoformat(timespec="milliseconds")

        body = {
            "srcSysCd": account["srcSysCd"],
            "srcAcctId": account["srcAcctId"],
            "srcAcctId2": account.get("srcAcctId2") or "",
            "meterSerialNumber": meter["serialNum"],
            "serviceType": meter["serviceType"],
            "intervalFrequency": "HOURLY",
            "periodType": "DAY",
            "date": date_iso,
            "agrmtStartDt": _mdy(meter["agreementActiveDate"]),
            "agrmtEndDt": _mdy(meter["agreementEndDate"]),
            "meterCertDt": _mdy(meter["meterCertificationDate"]),
            "startDate": cursor.strftime(DATE_FMT),
            "endDate": chunk_end.strftime(DATE_FMT),
            "zipCode": zip_code,
        }
        LOG.info("API hourly %s %s → %s", meter.get("serialNum"), cursor, chunk_end)
        result = _api_json(
            "POST", f"{API_BASE}/account/usage/graph", access_token, body=body
        )
        usage_array = result.get("usageArray") or []
        LOG.info("  got %s usage points", len(usage_array))

        i = 0
        d = cursor
        while d <= chunk_end:
            for h in range(24):
                if i >= len(usage_array):
                    break
                item = usage_array[i]
                i += 1
                while i < len(usage_array) and str(usage_array[i].get("date")) == str(
                    item.get("date")
                ):
                    i += 1
                try:
                    kwh = float(item.get("usage") or 0)
                except (TypeError, ValueError):
                    continue
                if kwh < 0:
                    continue
                stamp = datetime(d.year, d.month, d.day, h, tzinfo=TZ)
                by_start[stamp.isoformat()] = kwh
            d += timedelta(days=1)

        cursor = chunk_end + timedelta(days=1)

    return [{"start": k, "kwh": v} for k, v in sorted(by_start.items())]


def _click_visible_primary(page) -> None:
    for sel in (
        "button[data-action-button-primary]:visible",
        "button:has-text('Continue'):visible",
        "button:has-text('Sign In'):visible",
        "button[type='submit']:visible",
    ):
        try:
            loc = page.locator(sel).first
            if not loc.count():
                continue
            cls = loc.get_attribute("class") or ""
            if "ulp-hidden" in cls or loc.get_attribute("aria-hidden") == "true":
                continue
            if loc.is_visible(timeout=400):
                loc.click(timeout=8000)
                return
        except Exception:
            continue
    page.keyboard.press("Enter")


def _new_browser_context(pw, storage_state: str | None = None):
    browser = pw.chromium.launch(
        headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
    )
    kwargs: dict[str, Any] = {
        "accept_downloads": True,
        "viewport": {"width": 1400, "height": 900},
        "user_agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "locale": "en-US",
        "timezone_id": "America/New_York",
    }
    if storage_state and Path(storage_state).exists():
        kwargs["storage_state"] = storage_state
    return browser, browser.new_context(**kwargs)


class MfaRequiredError(RuntimeError):
    """Web session missing/expired; HA should prompt for MFA."""

    def __init__(self, message: str = "MFA required for Duke web session") -> None:
        super().__init__(message)
        self.error_code = "mfa_required"


def _passkey_enrolled() -> bool:
    return WEBAUTHN_PATH.is_file() and WEBAUTHN_PATH.stat().st_size > 20


def _load_webauthn_credential() -> dict[str, Any] | None:
    if not _passkey_enrolled():
        return None
    try:
        return json.loads(WEBAUTHN_PATH.read_text(encoding="utf-8"))
    except Exception as err:
        LOG.warning("Failed to load webauthn credential: %s", err)
        return None


def _save_webauthn_credential(cred: dict[str, Any]) -> None:
    """Persist a CDP WebAuthn credential dict."""
    payload = {
        "credentialId": cred.get("credentialId") or cred.get("credentialID"),
        "isResidentCredential": bool(
            cred.get("isResidentCredential", cred.get("isResidentKey", True))
        ),
        "rpId": cred.get("rpId") or AUTH0_DOMAIN,
        "privateKey": cred.get("privateKey"),
        "userHandle": cred.get("userHandle") or "",
        "signCount": int(cred.get("signCount") or 0),
    }
    if not payload["credentialId"] or not payload["privateKey"]:
        raise RuntimeError("Incomplete WebAuthn credential; not saving")
    WEBAUTHN_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    LOG.info("Saved worker passkey to %s", WEBAUTHN_PATH)


def _attach_virtual_authenticator(context, page, *, seed: bool = True) -> tuple[Any, str]:
    """Enable CDP WebAuthn and add a user-verifying platform authenticator."""
    cdp = context.new_cdp_session(page)
    cdp.send("WebAuthn.enable")
    auth = cdp.send(
        "WebAuthn.addVirtualAuthenticator",
        {
            "options": {
                "protocol": "ctap2",
                "ctap2Version": "ctap2_1",
                "transport": "internal",
                "hasResidentKey": True,
                "hasUserVerification": True,
                "isUserVerified": True,
                "automaticPresenceSimulation": True,
            }
        },
    )
    authenticator_id = auth["authenticatorId"]
    if seed:
        cred = _load_webauthn_credential()
        if cred and cred.get("credentialId") and cred.get("privateKey"):
            try:
                cdp.send(
                    "WebAuthn.addCredential",
                    {
                        "authenticatorId": authenticator_id,
                        "credential": {
                            "credentialId": cred["credentialId"],
                            "isResidentCredential": bool(
                                cred.get("isResidentCredential", True)
                            ),
                            "rpId": cred.get("rpId") or AUTH0_DOMAIN,
                            "privateKey": cred["privateKey"],
                            "userHandle": cred.get("userHandle") or "",
                            "signCount": int(cred.get("signCount") or 1),
                        },
                    },
                )
                LOG.info("Seeded virtual authenticator with stored passkey")
            except Exception as err:
                LOG.warning("Could not seed stored passkey: %s", err)
    return cdp, authenticator_id


def _capture_authenticator_credential(cdp, authenticator_id: str) -> dict[str, Any] | None:
    try:
        result = cdp.send(
            "WebAuthn.getCredentials", {"authenticatorId": authenticator_id}
        )
    except Exception as err:
        LOG.warning("getCredentials failed: %s", err)
        return None
    creds = result.get("credentials") or []
    if not creds:
        return None
    return creds[0]


def _page_looks_logged_in(page) -> bool:
    url = page.url.lower()
    # Transitional Auth0 callback — not logged in yet
    if "login-result" in url or "/authorize" in url or "login.duke-energy.com" in url:
        return False
    if "login" in url and "my-account/login" in url:
        return False
    if "dashboard" in url or "my-account" in url:
        try:
            if page.locator("text=Sign Out").first.is_visible(timeout=2000):
                return True
        except Exception:
            pass
        return "duke-energy.com/my-account" in url and "login" not in url
    return False


def _await_logged_in(page, timeout_ms: int = 45000) -> bool:
    """Wait through Auth0 login-result / passkey redirects until My Account loads."""
    import time as _time

    deadline = _time.time() + (timeout_ms / 1000.0)
    while _time.time() < deadline:
        url = page.url.lower()
        if _page_looks_logged_in(page):
            return True
        if "login-result" in url or "callback" in url or "login.duke-energy.com" in url:
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                page.wait_for_timeout(1500)
            continue
        page.wait_for_timeout(1000)

    # Final nudge: open dashboard with whatever cookies we have
    try:
        page.goto(WEB_DASHBOARD_URL, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(4000)
    except Exception:
        pass
    return _page_looks_logged_in(page)


def _try_click_passkey_continue(page) -> bool:
    for sel in (
        "button:has-text('Continue with Passkey')",
        "button:has-text('Passkey')",
        "button:has-text('Use passkey')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=800):
                LOG.info("Clicking passkey control via %s", sel)
                loc.click(timeout=8000)
                page.wait_for_timeout(3000)
                _await_logged_in(page, timeout_ms=45000)
                return True
        except Exception:
            continue
    return False


def _fill_email_password(page, email: str, password: str) -> None:
    user = page.locator(
        "input#username, input[name='username'], input[type='email']"
    ).first
    user.wait_for(state="visible", timeout=30000)
    user.fill(email)
    _click_visible_primary(page)
    page.wait_for_timeout(2000)
    pwd = page.locator("input[type='password']").first
    pwd.wait_for(state="visible", timeout=30000)
    pwd.fill(password)
    _click_visible_primary(page)
    page.wait_for_timeout(6000)


def _on_passkey_enrollment(page) -> bool:
    url = page.url.lower()
    if "passkey-enrollment" in url or "passkey_enrollment" in url:
        return True
    try:
        text = page.locator("body").inner_text(timeout=1500).lower()
    except Exception:
        return False
    return "simplify your sign in with passkeys" in text or (
        "passkeys" in text and "fingerprint or face" in text
    )


def _try_complete_passkey_enroll_ui(page, cdp, authenticator_id: str) -> bool:
    """If Auth0 shows a passkey setup screen, complete it and persist the credential."""
    try:
        text = page.locator("body").inner_text(timeout=2000).lower()
    except Exception:
        text = ""
    url = page.url.lower()
    prompts = (
        "create a passkey",
        "create passkey",
        "set up a passkey",
        "set up your passkey",
        "add a passkey",
        "save a passkey",
        "register this device",
        "simplify your sign in with passkeys",
        "fingerprint or face",
        "continue without passkey",
    )
    if "passkey-enrollment" not in url and not any(p in text for p in prompts):
        cred = _capture_authenticator_credential(cdp, authenticator_id)
        if cred:
            _save_webauthn_credential(cred)
            return True
        return False

    LOG.info("Passkey enrollment UI detected (%s)", page.url)
    return _click_create_passkey(page, cdp, authenticator_id)


def _skip_passkey_enroll_ui(page) -> bool:
    """Click Continue Without Passkey / Not now when user disabled worker passkey."""
    if not _on_passkey_enrollment(page):
        return False
    LOG.info("Skipping passkey enrollment (use_passkey=false) on %s", page.url)
    for sel in (
        "button:has-text('Continue Without Passkey')",
        "button:has-text('Not now')",
        "button:has-text('Skip')",
        "a:has-text('Continue Without Passkey')",
    ):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1000):
                loc.click(timeout=8000)
                page.wait_for_timeout(4000)
                return True
        except Exception:
            continue
    return False


def _maybe_handle_passkey_enroll(
    page, cdp, authenticator_id: str | None, *, use_passkey: bool
) -> bool:
    """Enroll or skip Auth0 passkey interstitial based on preference."""
    if not _on_passkey_enrollment(page):
        return False
    if not use_passkey:
        _skip_passkey_enroll_ui(page)
        return False
    if cdp and authenticator_id:
        return _try_complete_passkey_enroll_ui(page, cdp, authenticator_id)
    return False


def _click_create_passkey(page, cdp, authenticator_id: str) -> bool:
    # Prefer Create Passkey — NEVER click "Continue Without Passkey"
    # (Playwright :has-text('Continue') matches that skip button).
    create_selectors = (
        "button:has-text('Create Passkey')",
        "button:has-text('Create a passkey')",
        "button:has-text('Set up passkey')",
        "button:has-text('Set up a passkey')",
        "[data-action-button-primary]:has-text('Create')",
    )
    secondary_ok = (
        "button:has-text('Save')",
        "button:has-text('Register')",
        "button:has-text('Done')",
    )
    skip_phrases = (
        "without passkey",
        "not now",
        "skip",
        "later",
        "no thanks",
        "don't show",
    )

    for _attempt in range(6):
        clicked = False
        for sel in create_selectors + secondary_ok:
            try:
                loc = page.locator(sel).first
                if not loc.count() or not loc.is_visible(timeout=500):
                    continue
                label = (loc.inner_text(timeout=500) or "").strip()
                low = label.lower()
                if any(x in low for x in skip_phrases):
                    continue
                if "continue" in low and "create" not in low and "passkey" in low:
                    # e.g. "Continue Without Passkey"
                    continue
                LOG.info("Clicking passkey enroll control %r via %s", label, sel)
                loc.click(timeout=8000)
                page.wait_for_timeout(6000)
                clicked = True
                break
            except Exception:
                continue

        if not clicked:
            # Last resort: primary button that is NOT the skip control
            try:
                for loc in page.locator("button:visible").all():
                    label = (loc.inner_text(timeout=300) or "").strip()
                    low = label.lower()
                    if not label or any(x in low for x in skip_phrases):
                        continue
                    if "create" in low or (
                        "continue" in low and "without" not in low
                    ):
                        LOG.info("Clicking fallback enroll button %r", label)
                        loc.click(timeout=8000)
                        page.wait_for_timeout(6000)
                        clicked = True
                        break
            except Exception:
                pass

        cred = _capture_authenticator_credential(cdp, authenticator_id)
        if cred:
            _save_webauthn_credential(cred)
            LOG.info("Worker passkey captured from enrollment ceremony")
            try:
                cont = page.locator("button:has-text('Continue'):visible").first
                if cont.count() and cont.is_visible(timeout=1000):
                    label = (cont.inner_text(timeout=500) or "").lower()
                    if "without" not in label:
                        cont.click(timeout=5000)
                        page.wait_for_timeout(4000)
            except Exception:
                pass
            return True

        if _page_looks_logged_in(page) and not clicked:
            break
        page.wait_for_timeout(2000)

    cred = _capture_authenticator_credential(cdp, authenticator_id)
    if cred:
        _save_webauthn_credential(cred)
        return True
    LOG.warning(
        "Passkey enrollment did not produce a virtual credential "
        "(likely clicked skip or create ceremony failed)"
    )
    return False


def _login_still_blocked(page) -> bool:
    """True if we are stuck on an Auth0 challenge that still needs user input."""
    url = page.url.lower()
    if _on_passkey_enrollment(page):
        return False
    if _page_looks_logged_in(page):
        return False
    # MFA / password / identifier still pending
    if any(
        x in url
        for x in (
            "mfa-email",
            "mfa-login",
            "mfa-sms",
            "/u/login/password",
            "/u/login/identifier",
            "challenge",
        )
    ):
        return True
    if "login.duke-energy.com" in url and "passkey" not in url:
        # Generic Auth0 page — treat as blocked unless dashboard cookies already work
        return True
    return False


def _refresh_web_session(
    email: str, password: str, *, use_passkey: bool = True
) -> dict[str, Any]:
    """Silently rebuild web_storage_state via passkey and/or password login.

    Returns status dict. Raises MfaRequiredError only when interactive MFA is needed.
    """
    with sync_playwright() as pw:
        browser, context = _new_browser_context(pw)
        page = context.new_page()
        cdp, authenticator_id = _attach_virtual_authenticator(
            context, page, seed=use_passkey and _passkey_enrolled()
        )
        try:
            page.goto(WEB_DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2500)

            # Identifier step first (passkey CTA often appears only after email)
            try:
                user = page.locator(
                    "input#username, input[name='username'], input[type='email']"
                ).first
                if user.count() and user.is_visible(timeout=1500):
                    user.fill(email)
                    _click_visible_primary(page)
                    page.wait_for_timeout(2500)
            except Exception:
                pass

            # Prefer passkey when enabled and we have a worker credential
            if use_passkey and _passkey_enrolled():
                LOG.info("Attempting Continue with Passkey")
                clicked = _try_click_passkey_continue(page)
                if clicked and (
                    _page_looks_logged_in(page) or _await_logged_in(page)
                ):
                    context.storage_state(path=str(WEB_STATE_PATH))
                    cred = _capture_authenticator_credential(cdp, authenticator_id)
                    if cred:
                        try:
                            _save_webauthn_credential(cred)
                        except Exception:
                            pass
                    LOG.info("Web session refreshed via passkey")
                    return {
                        "ok": True,
                        "status": "passkey_authenticated",
                        "web_state": True,
                        "passkey_enrolled": True,
                    }
                LOG.info("Passkey login did not land on dashboard; trying password")

            # Password path (often succeeds without MFA on this tenant)
            if page.locator("input[type='password']").count():
                page.locator("input[type='password']").first.fill(password)
                _click_visible_primary(page)
                page.wait_for_timeout(6000)
                _await_logged_in(page, timeout_ms=30000)
            elif page.locator(
                "input#username, input[name='username'], input[type='email']"
            ).count():
                _fill_email_password(page, email, password)
                _await_logged_in(page, timeout_ms=30000)

            if _page_looks_logged_in(page) or _on_passkey_enrollment(page):
                _maybe_handle_passkey_enroll(
                    page, cdp, authenticator_id, use_passkey=use_passkey
                )
                if _page_looks_logged_in(page) or not _login_still_blocked(page):
                    context.storage_state(path=str(WEB_STATE_PATH))
                    LOG.info("Web session refreshed via password")
                    return {
                        "ok": True,
                        "status": "password_authenticated",
                        "web_state": True,
                        "passkey_enrolled": _passkey_enrolled(),
                    }

            # MFA required
            if "mfa" in page.url.lower():
                raise MfaRequiredError(
                    "Duke web MFA required after session expiry. "
                    "Open the integration and complete the MFA code step."
                )

            snippet = ""
            try:
                snippet = page.locator("body").inner_text(timeout=2000)[:300]
            except Exception:
                pass
            raise MfaRequiredError(
                f"Could not refresh Duke web session (url={page.url}). {snippet!r}"
            )
        finally:
            browser.close()


def _enroll_worker_passkey(email: str, password: str) -> dict[str, Any]:
    """Drive login with a fresh virtual authenticator and capture a new passkey if offered."""
    with sync_playwright() as pw:
        browser, context = _new_browser_context(pw)
        page = context.new_page()
        # Do not seed — we want a new registration ceremony
        cdp, authenticator_id = _attach_virtual_authenticator(
            context, page, seed=False
        )
        try:
            page.goto(WEB_DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(2000)
            # Nudge Auth0 with a failed passkey attempt (surfaces setup hint)
            _try_click_passkey_continue(page)
            page.wait_for_timeout(2000)
            if page.locator(
                "input#username, input[name='username'], input[type='email']"
            ).count():
                _fill_email_password(page, email, password)
            else:
                raise RuntimeError("Login form not available for passkey enrollment")

            enrolled = _try_complete_passkey_enroll_ui(page, cdp, authenticator_id)
            # Poll briefly in case create happens after redirect
            if not enrolled:
                for _ in range(10):
                    if _try_complete_passkey_enroll_ui(page, cdp, authenticator_id):
                        enrolled = True
                        break
                    if _page_looks_logged_in(page):
                        cred = _capture_authenticator_credential(cdp, authenticator_id)
                        if cred:
                            _save_webauthn_credential(cred)
                            enrolled = True
                        break
                    page.wait_for_timeout(1500)

            if _page_looks_logged_in(page):
                context.storage_state(path=str(WEB_STATE_PATH))

            if enrolled or _passkey_enrolled():
                return {
                    "ok": True,
                    "status": "passkey_enrolled",
                    "passkey_enrolled": True,
                    "web_state": WEB_STATE_PATH.exists(),
                }

            return {
                "ok": False,
                "status": "enroll_unavailable",
                "passkey_enrolled": False,
                "web_state": WEB_STATE_PATH.exists(),
                "error": (
                    "Duke did not offer a passkey enrollment screen. "
                    "If an iOS/phone passkey already exists, Auth0 often skips "
                    "adding another. Remove it under My Account → Passkeys "
                    "(or use an account without a passkey), then retry. "
                    "Password silent refresh still renews the web session."
                ),
            }
        finally:
            browser.close()


class _MfaSessionManager:
    """Run Playwright MFA flows on one dedicated thread (sync API is not thread-safe)."""

    def __init__(self) -> None:
        self._q: queue.Queue = queue.Queue()
        self._thread = threading.Thread(
            target=self._loop, name="duke-mfa", daemon=True
        )
        self._thread.start()
        # Live Playwright objects — only touched on MFA thread
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._authenticator_id: str | None = None
        self._email: str | None = None
        self._started_at: datetime | None = None
        self._use_passkey: bool = True

    def _loop(self) -> None:
        while True:
            job = self._q.get()
            if job is None:
                break
            op, kwargs, box = job
            try:
                if op == "start":
                    box["result"] = self._do_start(**kwargs)
                elif op == "complete":
                    box["result"] = self._do_complete(**kwargs)
                elif op == "cancel":
                    box["result"] = self._do_cancel()
                elif op == "status":
                    box["result"] = self._do_status()
                elif op == "enroll_passkey":
                    box["result"] = _enroll_worker_passkey(**kwargs)
                else:
                    raise RuntimeError(f"Unknown MFA op {op}")
            except Exception as err:
                box["error"] = err
            finally:
                box["done"].set()

    def _call(self, op: str, timeout: float = 180, **kwargs: Any) -> dict[str, Any]:
        done = threading.Event()
        box: dict[str, Any] = {"done": done}
        self._q.put((op, kwargs, box))
        if not done.wait(timeout=timeout):
            raise RuntimeError(f"MFA {op} timed out after {timeout}s")
        if "error" in box:
            raise box["error"]
        return box["result"]

    def start(
        self, email: str, password: str, *, use_passkey: bool = True
    ) -> dict[str, Any]:
        return self._call(
            "start",
            timeout=120,
            email=email,
            password=password,
            use_passkey=use_passkey,
        )

    def complete(self, code: str, *, use_passkey: bool | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {"code": code}
        if use_passkey is not None:
            kwargs["use_passkey"] = use_passkey
        return self._call("complete", timeout=90, **kwargs)

    def cancel(self) -> dict[str, Any]:
        return self._call("cancel", timeout=30)

    def status(self) -> dict[str, Any]:
        return self._call("status", timeout=10)

    def enroll_passkey(self, email: str, password: str) -> dict[str, Any]:
        return self._call(
            "enroll_passkey", timeout=180, email=email, password=password
        )

    def _close_unlocked(self) -> None:
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None
        self._cdp = None
        self._authenticator_id = None
        self._email = None
        self._started_at = None
        self._use_passkey = True

    def _do_status(self) -> dict[str, Any]:
        pending = self._page is not None
        age = None
        if self._started_at:
            age = int((datetime.now(timezone.utc) - self._started_at).total_seconds())
        return {
            "ok": True,
            "pending": pending,
            "email": self._email,
            "age_seconds": age,
            "web_state": WEB_STATE_PATH.exists(),
            "passkey_enrolled": _passkey_enrolled(),
        }

    def _do_cancel(self) -> dict[str, Any]:
        self._close_unlocked()
        return {"ok": True, "cancelled": True}

    def _do_start(
        self, email: str, password: str, use_passkey: bool = True
    ) -> dict[str, Any]:
        self._close_unlocked()
        self._use_passkey = bool(use_passkey)
        LOG.info("MFA start for %s (use_passkey=%s)", email, self._use_passkey)
        self._pw = sync_playwright().start()
        self._browser, self._context = _new_browser_context(self._pw)
        page = self._context.new_page()
        self._page = page
        self._cdp, self._authenticator_id = _attach_virtual_authenticator(
            self._context, page, seed=self._use_passkey and _passkey_enrolled()
        )
        self._email = email
        self._started_at = datetime.now(timezone.utc)

        page.goto(WEB_DASHBOARD_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)

        # Prefer passkey if enrolled and enabled
        if (
            self._use_passkey
            and _passkey_enrolled()
            and _try_click_passkey_continue(page)
        ):
            if _page_looks_logged_in(page):
                self._context.storage_state(path=str(WEB_STATE_PATH))
                self._close_unlocked()
                return {
                    "ok": True,
                    "status": "already_authenticated",
                    "web_state": True,
                    "passkey_enrolled": True,
                }

        # Already signed in via saved cookies in a fresh context? (no storage loaded)
        if "dashboard" in page.url and "login" not in page.url.lower():
            try:
                if page.locator("text=Sign Out").first.is_visible(timeout=2000):
                    self._context.storage_state(path=str(WEB_STATE_PATH))
                    self._close_unlocked()
                    return {
                        "ok": True,
                        "status": "already_authenticated",
                        "web_state": True,
                        "passkey_enrolled": _passkey_enrolled(),
                    }
            except Exception:
                pass

        user = page.locator(
            "input#username, input[name='username'], input[type='email']"
        ).first
        user.wait_for(state="visible", timeout=30000)
        user.fill(email)
        _click_visible_primary(page)
        page.wait_for_timeout(2000)
        pwd = page.locator("input[type='password']").first
        pwd.wait_for(state="visible", timeout=30000)
        pwd.fill(password)
        _click_visible_primary(page)
        page.wait_for_timeout(5000)

        # No MFA — dashboard, or passkey enrollment after password
        if _on_passkey_enrollment(page):
            enrolled = _maybe_handle_passkey_enroll(
                page,
                self._cdp,
                self._authenticator_id,
                use_passkey=self._use_passkey,
            )
            self._context.storage_state(path=str(WEB_STATE_PATH))
            self._close_unlocked()
            return {
                "ok": True,
                "status": "already_authenticated",
                "web_state": True,
                "passkey_enrolled": enrolled or _passkey_enrolled(),
            }

        if _page_looks_logged_in(page) or (
            "mfa" not in page.url.lower()
            and "login.duke-energy.com" not in page.url.lower()
        ):
            _maybe_handle_passkey_enroll(
                page,
                self._cdp,
                self._authenticator_id,
                use_passkey=self._use_passkey,
            )
            self._context.storage_state(path=str(WEB_STATE_PATH))
            self._close_unlocked()
            LOG.info("Web login succeeded without MFA")
            return {
                "ok": True,
                "status": "already_authenticated",
                "web_state": True,
                "passkey_enrolled": _passkey_enrolled(),
            }

        # MFA chooser → Email
        if "mfa-login-options" in page.url or page.get_by_role(
            "button", name="Email"
        ).count():
            try:
                page.get_by_role("button", name="Email").click(timeout=8000)
                page.wait_for_timeout(3000)
            except Exception as err:
                self._close_unlocked()
                raise RuntimeError(f"Could not select Email MFA: {err}") from err

        if "mfa-email-challenge" not in page.url and "mfa" not in page.url.lower():
            snippet = ""
            try:
                snippet = page.locator("body").inner_text(timeout=2000)[:300]
            except Exception:
                pass
            url = page.url
            self._close_unlocked()
            raise RuntimeError(f"Expected MFA challenge page, got {url}: {snippet!r}")

        # Remember this device for 30 days
        try:
            label = page.locator("label:has-text('Remember this device')").first
            if label.count() and label.is_visible(timeout=1000):
                label.click(timeout=2000)
            else:
                box = page.locator("input[type='checkbox']").first
                if box.count() and not box.is_checked():
                    box.check(timeout=2000)
        except Exception:
            pass

        LOG.info("MFA email code sent; waiting for /mfa/complete")
        return {
            "ok": True,
            "status": "code_sent",
            "method": "email",
            "email": email,
            "web_state": False,
        }

    def _do_complete(self, code: str, use_passkey: bool | None = None) -> dict[str, Any]:
        if not self._page or not self._context:
            raise RuntimeError(
                "No pending MFA session. Press Request code first."
            )
        if use_passkey is not None:
            self._use_passkey = bool(use_passkey)
        page = self._page
        code = (code or "").strip()
        if not code:
            raise RuntimeError("MFA code is required")

        # Expire stale sessions (~10 min)
        if self._started_at and datetime.now(timezone.utc) - self._started_at > timedelta(
            minutes=10
        ):
            self._close_unlocked()
            raise RuntimeError("MFA session expired. Request a new code.")

        LOG.info("Submitting MFA code (use_passkey=%s)", self._use_passkey)
        try:
            remember = page.locator("label:has-text('Remember this device')").first
            if remember.count() and remember.is_visible(timeout=500):
                remember.click(timeout=2000)
        except Exception:
            pass

        otp = page.locator(
            "input[name='code'], input[inputmode='numeric'], "
            "input[autocomplete='one-time-code'], input[type='text']"
        ).first
        otp.wait_for(state="visible", timeout=20000)
        otp.fill(code)
        _click_visible_primary(page)
        page.wait_for_timeout(8000)

        enrolled = False
        if _on_passkey_enrollment(page):
            LOG.info("MFA accepted; handling passkey enrollment interstitial")
            enrolled = _maybe_handle_passkey_enroll(
                page,
                self._cdp,
                self._authenticator_id,
                use_passkey=self._use_passkey,
            )
            page.wait_for_timeout(3000)

        if _login_still_blocked(page):
            snippet = ""
            try:
                snippet = page.locator("body").inner_text(timeout=2000)[:300]
            except Exception:
                pass
            raise RuntimeError(
                f"MFA code rejected or login incomplete (url={page.url}). {snippet!r}"
            )

        if not _page_looks_logged_in(page):
            page.wait_for_timeout(5000)

        if not enrolled:
            enrolled = _maybe_handle_passkey_enroll(
                page,
                self._cdp,
                self._authenticator_id,
                use_passkey=self._use_passkey,
            )

        self._context.storage_state(path=str(WEB_STATE_PATH))
        LOG.info(
            "Saved web storage state after MFA to %s (passkey_enrolled=%s)",
            WEB_STATE_PATH,
            enrolled or _passkey_enrolled(),
        )
        self._close_unlocked()
        return {
            "ok": True,
            "status": "authenticated",
            "web_state": True,
            "passkey_enrolled": enrolled or _passkey_enrolled(),
        }


_MFA = _MfaSessionManager()


def _web_session_valid() -> bool:
    if not WEB_STATE_PATH.exists():
        return False
    with sync_playwright() as pw:
        browser, context = _new_browser_context(pw, str(WEB_STATE_PATH))
        page = context.new_page()
        try:
            page.goto(WEB_USAGE_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(4000)
            ok = "usage" in page.url and "login" not in page.url.lower()
            try:
                ok = ok and page.locator("text=Download My Data").first.is_visible(
                    timeout=5000
                )
            except Exception:
                ok = False
            return bool(ok)
        finally:
            browser.close()


def _ensure_web_session(
    email: str,
    password: str,
    mfa_code: str | None = None,
    *,
    use_passkey: bool = True,
) -> None:
    """Ensure a valid web storage state exists for Green Button download."""
    debug_state = DATA_DIR / "debug" / "web_dashboard_state.json"
    if not WEB_STATE_PATH.exists() and debug_state.exists():
        WEB_STATE_PATH.write_bytes(debug_state.read_bytes())
        LOG.info("Seeded web state from %s", debug_state)

    if mfa_code:
        # Complete any pending MFA, or start+complete in one shot
        status = _MFA.status()
        if not status.get("pending"):
            started = _MFA.start(email, password, use_passkey=use_passkey)
            if started.get("status") == "already_authenticated":
                return
        _MFA.complete(mfa_code, use_passkey=use_passkey)
        return

    if _web_session_valid():
        LOG.info("Web session OK for usage download")
        return

    LOG.warning("Web session missing or expired; attempting silent refresh")
    try:
        result = _refresh_web_session(email, password, use_passkey=use_passkey)
        LOG.info("Silent web refresh: %s", result.get("status"))
        return
    except MfaRequiredError:
        raise
    except Exception as err:
        LOG.warning("Silent web refresh failed: %s", err)
        raise MfaRequiredError(
            "Duke web session expired or MFA is required. "
            "Open the integration and complete the MFA code step."
        ) from err


def _download_green_button_xml() -> Path:
    """Click Download My Data on usage page; return path to saved XML."""
    if not WEB_STATE_PATH.exists():
        raise MfaRequiredError("Missing web storage state; complete MFA first")

    out = DOWNLOADS_DIR / f"download-my-data-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}.xml"
    with sync_playwright() as pw:
        browser, context = _new_browser_context(pw, str(WEB_STATE_PATH))
        page = context.new_page()
        page.goto(WEB_USAGE_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        if "login" in page.url.lower():
            browser.close()
            raise MfaRequiredError("Web session expired during download")
        loc = page.locator("text=Download My Data").first
        loc.wait_for(state="visible", timeout=30000)
        LOG.info("Clicking Download My Data")
        with page.expect_download(timeout=120000) as dl_info:
            loc.click()
        download = dl_info.value
        download.save_as(out)
        context.storage_state(path=str(WEB_STATE_PATH))
        browser.close()
    if not out.exists() or out.stat().st_size < 1000:
        raise RuntimeError(f"Green Button download empty/missing: {out}")
    LOG.info("Downloaded Green Button XML %s (%s bytes)", out, out.stat().st_size)
    return out


def _parse_espi_intervals(
    xml_path: Path,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """Parse Green Button ESPI IntervalReading values into 15-min kWh rows."""
    LOG.info("Parsing ESPI XML %s for %s → %s", xml_path, start, end)
    start_ts = datetime(start.year, start.month, start.day, tzinfo=TZ).timestamp()
    end_exclusive = datetime(
        end.year, end.month, end.day, tzinfo=TZ
    ) + timedelta(days=1)
    end_ts = end_exclusive.timestamp()

    by_start: dict[str, float] = {}
    # Streaming parse to keep memory reasonable
    for _event, elem in ET.iterparse(xml_path, events=("end",)):
        if not elem.tag.endswith("IntervalReading"):
            continue
        start_el = None
        value_el = None
        for child in elem.iter():
            if child.tag.endswith("start") and start_el is None:
                start_el = child
            elif child.tag.endswith("value") and value_el is None:
                value_el = child
        if start_el is None or value_el is None or start_el.text is None:
            elem.clear()
            continue
        try:
            ts = int(start_el.text)
            kwh = float(value_el.text or 0)
        except (TypeError, ValueError):
            elem.clear()
            continue
        if ts < start_ts or ts >= end_ts:
            elem.clear()
            continue
        if kwh < 0:
            elem.clear()
            continue
        stamp = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(TZ)
        # Align to 15-minute boundary
        minute = (stamp.minute // 15) * 15
        stamp = stamp.replace(minute=minute, second=0, microsecond=0)
        by_start[stamp.isoformat()] = kwh
        elem.clear()

    rows = [{"start": k, "kwh": v} for k, v in sorted(by_start.items())]
    LOG.info("Parsed %s fifteen-minute readings", len(rows))
    return rows


def run_validate(email: str, password: str) -> dict[str, Any]:
    _ensure_dirs()
    tokens = _get_tokens(email, password)
    tokens = _ensure_duke_api_token(tokens, email)
    payload = _jwt_payload(tokens["id_token"])
    return {
        "ok": True,
        "email": tokens.get("de_email") or payload.get("email") or email,
        "has_id_token": True,
        "has_de_access_token": bool(tokens.get("de_access_token")),
        "has_refresh_token": bool(tokens.get("refresh_token")),
        "has_web_state": WEB_STATE_PATH.exists(),
    }


def _aggregate_daily(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sum kWh rows into calendar-day buckets (America/New_York)."""
    by_day: dict[str, float] = {}
    for item in rows:
        try:
            stamp = datetime.fromisoformat(item["start"])
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=TZ)
            else:
                stamp = stamp.astimezone(TZ)
            day = stamp.replace(hour=0, minute=0, second=0, microsecond=0)
            by_day[day.isoformat()] = by_day.get(day.isoformat(), 0.0) + float(
                item.get("kwh") or 0
            )
        except Exception:
            continue
    return [{"start": k, "kwh": v} for k, v in sorted(by_day.items())]


def run_export(
    email: str,
    password: str,
    start: date,
    end: date,
    meter_serial: str | None = None,
    interval: str = "hourly",
    mfa_code: str | None = None,
    *,
    use_passkey: bool = True,
) -> dict[str, Any]:
    _ensure_dirs()
    interval_norm = (interval or "hourly").strip().lower().replace("-", "_")
    if interval_norm in {"fifteen_minute", "fifteen", "15", "15min", "15_minute"}:
        LOG.info("Fifteen-minute export via Green Button XML %s → %s", start, end)
        _ensure_web_session(
            email, password, mfa_code=mfa_code, use_passkey=use_passkey
        )
        xml_path = _download_green_button_xml()
        rows = _parse_espi_intervals(xml_path, start, end)
        return {
            "ok": True,
            "hours": rows,  # kept key name for HA coordinator compatibility
            "count": len(rows),
            "interval": "fifteen_minute",
            "meter_serial": meter_serial,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "source": "green_button_xml",
        }

    want_daily = interval_norm in {"daily", "day", "1d", "1_day"}

    tokens = _get_tokens(email, password)
    tokens = _ensure_duke_api_token(tokens, email)
    access_token = tokens["de_access_token"]
    payload = _jwt_payload(tokens["id_token"])
    internal = (
        tokens.get("de_internal_user_id")
        or payload.get("internal_identifier")
        or payload.get("internalUserID")
        or ""
    )
    token_email = tokens.get("de_email") or payload.get("email") or email
    LOG.info("Hourly export as %s internal=%s", token_email, str(internal)[:48])

    meters = _get_meters(access_token, token_email, str(internal))
    electric = [
        m for m in meters if str(m.get("serviceType", "")).upper() == "ELECTRIC"
    ]
    if not electric:
        raise RuntimeError(f"No ELECTRIC meters found ({len(meters)} total)")

    chosen = None
    if meter_serial:
        for m in electric:
            if str(m.get("serialNum")) == str(meter_serial):
                chosen = m
                break
    chosen = chosen or electric[0]
    LOG.info("Using meter %s", chosen.get("serialNum"))

    hours = _hourly_for_range(access_token, chosen, start, end)
    filtered = [
        item
        for item in hours
        if start <= datetime.fromisoformat(item["start"]).date() <= end
    ]
    if want_daily:
        filtered = _aggregate_daily(filtered)
        out_interval = "daily"
    else:
        out_interval = "hourly"
    return {
        "ok": True,
        "hours": filtered,
        "count": len(filtered),
        "interval": out_interval,
        "meter_serial": chosen.get("serialNum"),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "source": "cma_api",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.rstrip("/")
        if path == "/health":
            self._send(
                200,
                {
                    "ok": True,
                    "playwright_ready": _PLAYWRIGHT_READY,
                    "tokens": TOKENS_PATH.exists(),
                    "web_state": WEB_STATE_PATH.exists(),
                    "passkey_enrolled": _passkey_enrolled(),
                    "mfa_pending": _MFA.status().get("pending", False),
                },
            )
            return
        if path == "/mfa/status":
            self._send(200, _MFA.status())
            return
        self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "invalid json"})
            return
        path = self.path.rstrip("/")
        try:
            if path == "/validate":
                result = run_validate(body["email"], body["password"])
                self._send(200 if result.get("ok") else 401, result)
                return
            if path == "/mfa/start":
                result = _MFA.start(
                    body["email"],
                    body["password"],
                    use_passkey=bool(body.get("use_passkey", True)),
                )
                self._send(200, result)
                return
            if path == "/mfa/complete":
                result = _MFA.complete(
                    str(body.get("mfa_code") or body.get("code") or ""),
                    use_passkey=bool(body.get("use_passkey", True)),
                )
                self._send(200, result)
                return
            if path == "/mfa/cancel":
                self._send(200, _MFA.cancel())
                return
            if path == "/passkey/enroll":
                result = _MFA.enroll_passkey(body["email"], body["password"])
                self._send(200 if result.get("ok") else 400, result)
                return
            if path == "/export":
                try:
                    result = run_export(
                        body["email"],
                        body["password"],
                        date.fromisoformat(body["start"]),
                        date.fromisoformat(body["end"]),
                        body.get("meter_serial"),
                        interval=str(body.get("interval") or "hourly"),
                        mfa_code=body.get("mfa_code"),
                        use_passkey=bool(body.get("use_passkey", True)),
                    )
                except MfaRequiredError as err:
                    self._send(
                        401,
                        {
                            "ok": False,
                            "error": str(err),
                            "error_code": "mfa_required",
                            "passkey_enrolled": _passkey_enrolled(),
                        },
                    )
                    return
                self._send(200 if result.get("ok") else 500, result)
                return
            self._send(404, {"ok": False, "error": "not found"})
        except Exception as err:
            LOG.error("Request failed: %s\n%s", err, traceback.format_exc())
            self._send(500, {"ok": False, "error": str(err)})


def main() -> None:
    global _PLAYWRIGHT_READY
    _ensure_dirs()
    _publish_worker_url()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True, args=["--disable-dev-shm-usage", "--no-sandbox"]
            )
            browser.close()
        _PLAYWRIGHT_READY = True
        LOG.info("Playwright Chromium ready")
    except Exception as err:
        _PLAYWRIGHT_READY = False
        LOG.error("Playwright not ready: %s", err)

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    LOG.info("Duke scraper worker listening on %s:%s", HOST, PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()