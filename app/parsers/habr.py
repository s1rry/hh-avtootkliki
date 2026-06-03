"""
Habr Career parser — search-only via HTML scraping.
Apply/messages require login and will be added later.
"""
import re

import httpx
import structlog
from bs4 import BeautifulSoup

from app.parsers.base import BaseParser, ParsedVacancy
from app.utils.rate_limiter import hh_limiter  # reuse limiter

log = structlog.get_logger()

HABR_BASE = "https://career.habr.com"
HABR_SEARCH = "https://career.habr.com/vacancies"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class HabrParser(BaseParser):
    platform = "habr"

    async def login(self) -> bool:
        # No login needed for search — HTML scraping works anonymously
        return True

    async def search_vacancies(self, query: str, **filters) -> list[ParsedVacancy]:
        params = {
            "q": query,
            "type": "all",
            "sort": "date",
        }
        if filters.get("remote", True):
            params["remote"] = "true"

        vacancies: list[ParsedVacancy] = []
        try:
            async with hh_limiter:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
                    resp = await client.get(HABR_SEARCH, params=params)
                    resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")
            cards = soup.select(".vacancy-card")
            log.info("habr_search_results", query=query, count=len(cards))

            for card in cards[:50]:
                vacancy = self._parse_card(card)
                if vacancy:
                    vacancies.append(vacancy)

        except Exception as e:
            log.error("habr_search_error", query=query, error=str(e))

        return vacancies

    def _parse_card(self, card) -> ParsedVacancy | None:
        title_el = card.select_one(".vacancy-card__title-link")
        if not title_el:
            return None

        title = title_el.get_text(strip=True)
        href = title_el.get("href", "")
        url = href if href.startswith("http") else f"{HABR_BASE}{href}"

        ext_id_match = re.search(r"/vacancies/(\d+)", href)
        ext_id = ext_id_match.group(1) if ext_id_match else url

        company_link = card.select_one(".vacancy-card__company a")
        company_name = company_link.get_text(strip=True) if company_link else ""
        company_url = ""
        if company_link:
            ch = company_link.get("href", "")
            company_url = ch if ch.startswith("http") else f"{HABR_BASE}{ch}"

        salary_from, salary_to, currency = None, None, ""
        salary_el = card.select_one(".basic-salary, .vacancy-card__salary")
        if salary_el:
            salary_from, salary_to, currency = self._parse_salary(salary_el.get_text())

        meta_text = ""
        meta_el = card.select_one(".vacancy-card__meta")
        if meta_el:
            meta_text = meta_el.get_text(" ", strip=True)
        is_remote = (
            "можно удалённо" in meta_text.lower()
            or "удалённо" in meta_text.lower()
            or "remote" in meta_text.lower()
        )

        # Skills as tag chips
        skills = [
            s.get_text(strip=True)
            for s in card.select(".link-comp--appearance-dark, .vacancy-card__skills a")
        ]

        return ParsedVacancy(
            platform="habr",
            external_id=ext_id,
            url=url,
            title=title,
            company_name=company_name,
            company_url=company_url,
            salary_from=salary_from,
            salary_to=salary_to,
            salary_currency=currency,
            location="" if is_remote else "Россия",
            is_remote=is_remote,
            skills=skills,
        )

    def _parse_salary(self, text: str) -> tuple[int | None, int | None, str]:
        text = text.replace("\xa0", "").replace(" ", "")
        currency = ""
        if "₽" in text or "руб" in text:
            currency = "RUB"
        elif "$" in text or "USD" in text:
            currency = "USD"
        elif "€" in text or "EUR" in text:
            currency = "EUR"

        numbers = [int(x) for x in re.findall(r"\d+", text)]
        if "от" in text and "до" in text and len(numbers) >= 2:
            return numbers[0], numbers[1], currency
        elif "от" in text and numbers:
            return numbers[0], None, currency
        elif "до" in text and numbers:
            return None, numbers[0], currency
        elif numbers:
            return numbers[0], numbers[-1] if len(numbers) > 1 else None, currency
        return None, None, currency

    async def get_vacancy_details(self, url: str) -> ParsedVacancy | None:
        try:
            async with hh_limiter:
                async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=30) as client:
                    resp = await client.get(url)
                    resp.raise_for_status()

            soup = BeautifulSoup(resp.text, "lxml")
            title_el = soup.select_one("h1.page-title__title, .page-title__title")
            title = title_el.get_text(strip=True) if title_el else ""

            desc_el = soup.select_one(".vacancy-description__text, .style-ugc")
            description = desc_el.get_text("\n", strip=True) if desc_el else ""

            skills = [
                s.get_text(strip=True)
                for s in soup.select(".content-section .link-comp--appearance-dark")
            ]

            ext_id_match = re.search(r"/vacancies/(\d+)", url)
            ext_id = ext_id_match.group(1) if ext_id_match else url

            return ParsedVacancy(
                platform="habr",
                external_id=ext_id,
                url=url,
                title=title,
                description=description,
                skills=skills,
            )
        except Exception as e:
            log.error("habr_details_error", url=url, error=str(e))
            return None

    async def apply_to_vacancy(self, url: str, cover_letter: str):
        pw = self._get_playwright()
        if pw:
            return await pw.apply_to_vacancy(url, cover_letter)
        log.warning("habr_apply_not_supported", url=url, reason="playwright not available")
        return False

    async def check_messages(self) -> list[dict]:
        pw = self._get_playwright()
        if pw:
            return await pw.check_messages()
        return []

    def _get_playwright(self):
        try:
            from app.parsers.habr_playwright import habr_playwright
            return habr_playwright
        except ImportError:
            return None
