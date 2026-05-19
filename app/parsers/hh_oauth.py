"""
HH.ru OAuth via official Android app credentials.

This bypasses DDoS Guard entirely because api.hh.ru is a clean API host
without anti-bot protection. The Android app credentials are publicly
known and used by many third-party HH clients.

Flow:
1. GET hh.ru/oauth/authorize?... with logged-in cookies → returns code
2. POST hh.ru/oauth/token (code + client_secret) → access_token + refresh_token
3. POST api.hh.ru/negotiations (Bearer token + vacancy_id + resume_id) → apply
4. Token cached + refreshed automatically
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

# Public HH Android app credentials — used by all OSS HH clients
CLIENT_ID = "HIOMIAS39CA9DICTA7JIO64LQKQJF5AGIK74G9ITJKLNEDAOH5FHS5G1JI7FOEGD"
CLIENT_SECRET = "V9M870DE342BGHFRUJ5FTCGCUA1482AN0DI8C5TFI9ULMA89H10N60NOP8I4JMVS"
REDIRECT_URI = "hhandroid://oauthresponse"

UA = "ru.hh.android/8.116 (Android 13; samsung SM-S908B)"
TOKEN_FILE = Path("data/hh_oauth_token.json")
COOKIES_FILE = Path("data/browser_sessions/hh_state.json")


def _load_hh_cookies() -> dict[str, str]:
    if not COOKIES_FILE.exists():
        return {}
    try:
        data = json.loads(COOKIES_FILE.read_text())
    except Exception:
        return {}
    out = {}
    for c in data.get("cookies", []):
        if "hh.ru" in c.get("domain", ""):
            out[c["name"]] = c["value"]
    return out


def _load_token() -> dict | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text())
    except Exception:
        return None


def _save_token(token: dict):
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(json.dumps(token))


class HHOAuth:
    platform = "hh"

    async def _refresh(self, refresh_token: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "refresh_token": refresh_token,
                    },
                    headers={"User-Agent": UA},
                )
                if r.status_code == 200:
                    d = r.json()
                    return {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token", refresh_token),
                        "expires_at": time.time() + d.get("expires_in", 1209599),
                    }
                log.warning("oauth_refresh_failed", status=r.status_code, body=r.text[:200])
        except Exception as e:
            log.warning("oauth_refresh_error", error=str(e))
        return None

    async def _authorize(self) -> dict | None:
        """Full authorize flow using browser-session cookies."""
        cookies = _load_hh_cookies()
        if not cookies:
            log.error("oauth_no_cookies")
            return None
        try:
            async with httpx.AsyncClient(timeout=15, verify=False, cookies=cookies, headers={"User-Agent": UA}) as c:
                # Step 1: authorize, expect 302 to hhandroid:// with code
                r1 = await c.get(
                    "https://hh.ru/oauth/authorize",
                    params={
                        "response_type": "code",
                        "client_id": CLIENT_ID,
                        "redirect_uri": REDIRECT_URI,
                        "state": "bot",
                    },
                    follow_redirects=False,
                )
                code = None
                loc = r1.headers.get("location", "")
                m = re.search(r"code=([^&]+)", loc)
                if m:
                    code = m.group(1)
                if not code:
                    # Need approve form submit
                    text = r1.text or ""
                    if r1.status_code == 200 and ("разрешить" in text.lower() or "approve" in text.lower()):
                        r2 = await c.post(
                            "https://hh.ru/oauth/authorize",
                            data={
                                "response_type": "code",
                                "client_id": CLIENT_ID,
                                "redirect_uri": REDIRECT_URI,
                                "state": "bot",
                                "action": "approve",
                                "_xsrf": cookies.get("_xsrf", ""),
                            },
                            follow_redirects=False,
                        )
                        loc2 = r2.headers.get("location", "")
                        m2 = re.search(r"code=([^&]+)", loc2)
                        if m2:
                            code = m2.group(1)
                if not code:
                    log.error("oauth_no_code", status=r1.status_code, location=loc[:200])
                    return None

                # Step 2: exchange for token
                r3 = await c.post(
                    "https://hh.ru/oauth/token",
                    data={
                        "grant_type": "authorization_code",
                        "client_id": CLIENT_ID,
                        "client_secret": CLIENT_SECRET,
                        "redirect_uri": REDIRECT_URI,
                        "code": code,
                    },
                    headers={
                        "User-Agent": UA,
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
                if r3.status_code == 200:
                    d = r3.json()
                    token = {
                        "access_token": d["access_token"],
                        "refresh_token": d.get("refresh_token", ""),
                        "expires_at": time.time() + d.get("expires_in", 1209599),
                    }
                    log.info("oauth_authorize_success", expires_in=d.get("expires_in"))
                    return token
                log.error("oauth_token_exchange_failed", status=r3.status_code, body=r3.text[:200])
        except Exception as e:
            log.error("oauth_authorize_error", error=str(e))
        return None

    async def get_token(self) -> str:
        cached = _load_token()
        if cached and cached.get("expires_at", 0) > time.time() + 300:
            return cached["access_token"]
        # Try refresh
        if cached and cached.get("refresh_token"):
            new = await self._refresh(cached["refresh_token"])
            if new:
                _save_token(new)
                return new["access_token"]
        # Full authorize
        new = await self._authorize()
        if new:
            _save_token(new)
            return new["access_token"]
        return ""

    async def apply(self, vacancy_id: str, message: str = "") -> tuple[bool | str, dict]:
        token = await self.get_token()
        if not token:
            return False, {"error": "no_oauth_token"}
        rhash = settings.hh_resume_id
        if not rhash:
            return False, {"error": "no_resume_id"}

        data: dict[str, Any] = {"vacancy_id": str(vacancy_id), "resume_id": rhash}
        if message:
            data["message"] = message

        try:
            async with httpx.AsyncClient(timeout=15, verify=False) as c:
                r = await c.post(
                    "https://api.hh.ru/negotiations",
                    headers={
                        "User-Agent": UA,
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=data,
                )
        except httpx.RequestError as e:
            return False, {"error": f"http: {e}"}

        if r.status_code in (200, 201, 204):
            return True, {"status": r.status_code}
        if r.status_code == 400:
            try:
                d = r.json()
            except Exception:
                d = {"raw": r.text[:200]}
            errors = d.get("errors") or []
            err_value = ""
            if errors and isinstance(errors, list):
                err_value = errors[0].get("value", "") or errors[0].get("type", "")
            elif d.get("description"):
                err_value = str(d.get("description"))
            low = (err_value or "").lower()
            if "limit" in low or "limit" in str(d).lower():
                return False, {"error": "daily_limit", "data": d}
            if "already" in low or "exist" in low or "duplicate" in low:
                return "already", {"data": d}
            if "test" in low or "questionnaire" in low:
                return False, {"error": "needs_test", "data": d}
            if "archived" in low or "not_found" in low or "unavailable" in low or "hidden" in low:
                return "already", {"error": "unavailable", "data": d}
            return False, {"error": err_value or "bad_request", "data": d}
        if r.status_code in (401, 403):
            # Token expired — clear and retry once
            TOKEN_FILE.unlink(missing_ok=True)
            return False, {"error": "auth_expired"}
        if r.status_code == 404:
            return "already", {"error": "not_found"}
        return False, {"status": r.status_code, "body": (r.text or "")[:300]}


hh_oauth = HHOAuth()
