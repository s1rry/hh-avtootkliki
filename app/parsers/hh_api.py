"""
HH.ru API client — fast applies via direct HTTP requests
(вместо Playwright). Использует существующие cookies из Playwright-сессии.

Скорость: ~1 сек на отклик вместо 60-90 сек.
"""
from __future__ import annotations

import json
import random
import re
from pathlib import Path

import httpx
import structlog

from app.config import settings

log = structlog.get_logger()

HH_STATE_PATH = Path("data/browser_sessions/hh_state.json")


def _load_cookies() -> dict[str, str]:
    """Load cookies from Playwright storage state into a flat dict."""
    if not HH_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(HH_STATE_PATH.read_text())
    except Exception:
        return {}
    cookies = {}
    for c in data.get("cookies", []):
        # Only top-level hh.ru cookies (skip .chatik.hh.ru etc)
        domain = c.get("domain", "")
        if "hh.ru" in domain:
            cookies[c["name"]] = c["value"]
    return cookies


def _headers(xsrf: str) -> dict[str, str]:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
        "Origin": "https://hh.ru",
        "Referer": "https://hh.ru/",
        "X-Requested-With": "XMLHttpRequest",
        "X-XsrfToken": xsrf,
    }


def _randomize_letter(text: str) -> str:
    """Add tiny randomness so cover letters don't dedupe in hh-side."""
    # Random invisible variation
    if random.random() < 0.5:
        text = text.replace(". ", ".  ", 1) if ". " in text else text
    return text


class HHApiClient:
    platform = "hh"

    def __init__(self):
        self._cookies: dict[str, str] = {}
        self._xsrf: str = ""

    def reload_cookies(self) -> bool:
        self._cookies = _load_cookies()
        self._xsrf = self._cookies.get("_xsrf", "")
        if not self._xsrf:
            log.warning("hh_api_no_xsrf")
            return False
        return True

    async def is_logged_in(self) -> bool:
        if not self.reload_cookies():
            return False
        async with httpx.AsyncClient(cookies=self._cookies, headers=_headers(self._xsrf), timeout=15) as c:
            try:
                r = await c.get("https://hh.ru/applicant/resumes", follow_redirects=False)
                # Login page = redirect to /account/login or status 302
                if r.status_code in (301, 302):
                    loc = r.headers.get("location", "")
                    if "/account/login" in loc or "/auth/" in loc:
                        return False
                if r.status_code == 200 and "/applicant/" in str(r.url):
                    return True
                return False
            except Exception as e:
                log.warning("hh_api_login_check_error", error=str(e))
                return False

    async def fetch_applied_vacancy_ids(self) -> set[str]:
        """Pull list of vacancy_ids the user already applied to (via negotiations API).
        Used to pre-mark them in DB and skip re-attempts.
        """
        if not self.reload_cookies():
            return set()
        ids: set[str] = set()
        url = "https://hh.ru/shards/applicant/negotiations"
        params = {"page": 0, "perPage": 50}
        async with httpx.AsyncClient(cookies=self._cookies, headers=_headers(self._xsrf), timeout=20) as c:
            for page in range(0, 10):  # up to 500 items
                params["page"] = page
                try:
                    r = await c.get(url, params=params)
                    if r.status_code != 200:
                        break
                    data = r.json()
                except Exception as e:
                    log.warning("hh_api_fetch_applied_error", error=str(e), page=page)
                    break
                items = data.get("topicsList", []) or data.get("items", []) or []
                if not items:
                    break
                for it in items:
                    vid = (
                        it.get("vacancyId")
                        or it.get("vacancy", {}).get("@id")
                        or it.get("vacancy", {}).get("id")
                    )
                    if vid:
                        ids.add(str(vid))
                # if less than perPage — last page
                if len(items) < params["perPage"]:
                    break
        log.info("hh_api_fetched_applied_ids", count=len(ids))
        return ids

    async def apply(self, vacancy_id: str, cover_letter: str, resume_hash: str | None = None) -> tuple[bool | str, dict]:
        """Submit application via internal HH API.

        Returns (result, info):
          result is True (sent) / "already" / False (failed)
          info contains http details / error
        """
        if not self.reload_cookies():
            return False, {"error": "no cookies / not logged in"}

        rhash = resume_hash or settings.hh_resume_id or ""
        if not rhash:
            return False, {"error": "HH_RESUME_ID not set in .env"}

        url = "https://hh.ru/applicant_negotiations"
        form = {
            "resume_hash": rhash,
            "vacancy_id": str(vacancy_id),
            "letterRequired": "true",
            "letter": _randomize_letter(cover_letter or ""),
            "lux": "true",
            "ignore_postponed": "true",
        }
        async with httpx.AsyncClient(cookies=self._cookies, headers=_headers(self._xsrf), timeout=20) as c:
            try:
                r = await c.post(url, data=form)
            except httpx.RequestError as e:
                return False, {"error": f"http: {e}"}

            text = r.text or ""
            ct = r.headers.get("content-type", "")
            data = None
            if "application/json" in ct:
                try:
                    data = r.json()
                except Exception:
                    data = None

            # 200 / 204 / 303 considered success
            if r.status_code in (200, 204):
                # Check JSON body for specific error markers
                if isinstance(data, dict):
                    if data.get("error") == "negotiations-creation-error":
                        # Possible reasons: already_applied, daily_limit, test_required
                        reason = data.get("errors") or data.get("reason") or ""
                        s = str(reason).lower()
                        if "already" in s or "duplicate" in s:
                            return "already", {"data": data}
                        if "limit" in s or "quota" in s:
                            return False, {"error": "daily_limit", "data": data}
                        if "test" in s or "questionnaire" in s:
                            return False, {"error": "needs_test", "data": data}
                        return False, {"error": "neg-creation-error", "data": data}
                return True, {"status": r.status_code}

            if r.status_code == 303:
                # Successful: HH redirects to topic page
                return True, {"status": 303, "location": r.headers.get("location", "")}

            if r.status_code == 409:
                return "already", {"status": 409}

            if r.status_code in (401, 403):
                return False, {"error": "auth_required", "status": r.status_code}

            # Other
            return False, {"status": r.status_code, "body": text[:400]}


hh_api_client = HHApiClient()
