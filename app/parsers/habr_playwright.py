"""
Habr Career Playwright automation: login, apply.
Used when Playwright is available on VPS.
"""
import structlog
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.config import settings
from app.utils.browser import browser_manager
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

HABR_BASE = "https://career.habr.com"
# Habr Career uses Habr Account SSO for login
HABR_LOGIN_URL = "https://career.habr.com/users/auth/tmid"
HABR_RESPONSES = "https://career.habr.com/responses"


class HabrPlaywright:
    platform = "habr"

    def __init__(self):
        self._logged_in = False
        self._page: Page | None = None

    async def _get_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        self._page = await browser_manager.new_page("habr")
        return self._page

    async def login(self) -> bool:
        if self._logged_in:
            return True

        page = await self._get_page()

        # Check if already logged in via saved cookies
        try:
            await page.goto(HABR_BASE, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            logged = await page.query_selector(
                'a[href="/profile/personal/edit"], a[href="/responses"], '
                'a[href*="/profile/favorites"], a[href="/users/logout"], '
                '[data-qa="header__user"]'
            )
            if logged:
                self._logged_in = True
                await browser_manager.save_context("habr")
                log.info("habr_already_logged_in")
                return True
        except Exception as e:
            log.warning("habr_login_check_error", error=str(e))

        if not settings.habr_login or not settings.habr_password:
            log.error("habr_credentials_missing")
            return False

        try:
            await page.goto(HABR_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            email_input = await page.query_selector('input[name="email"]')
            if not email_input:
                email_input = await page.query_selector('input[type="email"]')
            if email_input:
                await email_input.fill(settings.habr_login)

            pwd_input = await page.query_selector('input[name="password"]')
            if not pwd_input:
                pwd_input = await page.query_selector('input[type="password"]')
            if pwd_input:
                await pwd_input.fill(settings.habr_password)

            await page.wait_for_timeout(500)

            submit = await page.query_selector('button[type="submit"]')
            if submit:
                await submit.click()
            else:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(5000)

            logged = await page.query_selector(
                'a[href="/profile/personal/edit"], a[href="/responses"], '
                'a[href*="/profile/favorites"], a[href="/users/logout"], '
                '[data-qa="header__user"]'
            )
            if logged:
                self._logged_in = True
                await browser_manager.save_context("habr")
                log.info("habr_login_success")
                return True

            log.error("habr_login_failed", url=page.url)
            return False
        except Exception as e:
            log.error("habr_login_error", error=str(e))
            return False

    async def apply_to_vacancy(self, vacancy_url: str, cover_letter: str) -> bool | str:
        if not self._logged_in:
            if not await self.login():
                return False

        page = await self._get_page()
        try:
            await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=45000)
            await random_delay(2, 4)

            # "Откликнуться" button on vacancy page
            apply_btn = await page.query_selector('a[href*="/respond"]')
            if not apply_btn:
                apply_btn = await page.query_selector('button:has-text("Откликнуться")')

            # Already applied check
            already = await page.query_selector('a[href*="/responses"]:has-text("отклик")')
            if already and not apply_btn:
                log.info("habr_already_applied", url=vacancy_url)
                return "already"

            if not apply_btn:
                log.warning("habr_apply_btn_not_found", url=vacancy_url)
                return False

            await apply_btn.click()
            await page.wait_for_timeout(3500)

            # Cover letter textarea
            letter_area = await page.query_selector('textarea[name="response[body]"]')
            if not letter_area:
                letter_area = await page.query_selector('textarea')
            if letter_area and cover_letter:
                await letter_area.fill(cover_letter)
                await page.wait_for_timeout(800)

            # Submit
            submit_btn = await page.query_selector('button[type="submit"]')
            if not submit_btn:
                submit_btn = await page.query_selector('button:has-text("Отправить")')
            if submit_btn:
                await submit_btn.click()
                await page.wait_for_timeout(5000)

                if "/responses" in page.url or "успешно" in (await page.content()).lower():
                    log.info("habr_apply_success", url=vacancy_url)
                    await browser_manager.save_context("habr")
                    return True

            log.warning("habr_apply_uncertain", url=vacancy_url, final=page.url)
            return False
        except PlaywrightTimeout:
            log.error("habr_apply_timeout", url=vacancy_url)
            return False
        except Exception as e:
            log.error("habr_apply_error", url=vacancy_url, error=str(e))
            return False


    async def check_messages(self) -> list[dict]:
        """Scrape /responses page for messages from employers."""
        if not self._logged_in:
            if not await self.login():
                return []

        page = await self._get_page()
        out: list[dict] = []
        try:
            await page.goto(HABR_RESPONSES, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3500)

            items = await page.evaluate(
                """() => {
                    const cards = document.querySelectorAll('.response-card, [class*="response-card"], .responses-list__item');
                    const out = [];
                    for (const c of cards) {
                        const titleEl = c.querySelector('a[href*="/vacancies/"]');
                        const companyEl = c.querySelector('a[href*="/companies/"]');
                        const statusEl = c.querySelector('[class*="status"], .response-card__status');
                        // Try to find unread / message indicator
                        const unreadEl = c.querySelector('[class*="unread"], [class*="new"]');
                        const lastMsgEl = c.querySelector('[class*="message"], .response-card__last-message');
                        out.push({
                            title: titleEl ? (titleEl.innerText || '').trim() : '',
                            href: titleEl ? titleEl.getAttribute('href') || '' : '',
                            company: companyEl ? (companyEl.innerText || '').trim() : '',
                            status: statusEl ? (statusEl.innerText || '').trim() : '',
                            text: lastMsgEl ? (lastMsgEl.innerText || '').trim() : '',
                            has_unread: !!unreadEl,
                        });
                    }
                    return out;
                }"""
            )

            for d in items[:30]:
                if not d.get("title") and not d.get("status"):
                    continue
                href = d.get("href", "")
                thread_id = f"habr_{href}" if href else ""
                out.append({
                    "platform": "habr",
                    "title": d.get("title", ""),
                    "company": d.get("company", ""),
                    "status": d.get("status", ""),
                    "text": d.get("text") or (f"Статус: {d.get('status','')}" if d.get("status") else ""),
                    "thread_id": thread_id,
                    "sender": d.get("company", ""),
                    "has_unread": d.get("has_unread", False),
                })

            log.info("habr_messages_fetched", count=len(out))
        except Exception as e:
            log.error("habr_messages_error", error=str(e))
        return out


habr_playwright: HabrPlaywright | None = None
try:
    from app.utils.browser import browser_manager  # noqa: F811
    habr_playwright = HabrPlaywright()
except ImportError:
    pass
