"""
Playwright-based hh.ru operations: login, apply, messages, negotiations.
Only used when Playwright is available (VPS deployment).
"""

import asyncio
import re

import structlog
from playwright.async_api import Page, TimeoutError as PlaywrightTimeout

from app.config import settings
from app.utils.browser import browser_manager
from app.utils.anti_detect import random_delay

log = structlog.get_logger()

HH_BASE = "https://hh.ru"
HH_LOGIN_URL = "https://hh.ru/account/login"
HH_NEGOTIATIONS = "https://hh.ru/applicant/negotiations"
HH_RESUMES = "https://hh.ru/applicant/resumes"


async def human_type(element, text: str, min_ms: int = 30, max_ms: int = 120):
    """Type text into element with random per-character delay.
    Uses click() to focus, then press_sequentially with mid-range delay.
    For very random feel we split text into 3-5 chunks with varied speeds.
    """
    import random
    if not text:
        return
    try:
        await element.click()
    except Exception:
        pass
    try:
        await element.fill("")  # clear existing
    except Exception:
        pass
    # Type in chunks with varied speed to look more human
    chunk_size = max(20, len(text) // 6)
    pos = 0
    while pos < len(text):
        chunk = text[pos:pos + chunk_size]
        delay = random.randint(min_ms, max_ms)
        try:
            await element.press_sequentially(chunk, delay=delay)
        except Exception:
            # fallback
            try:
                await element.type(chunk, delay=delay)
            except Exception:
                await element.fill(text)
                return
        pos += chunk_size


def _classify_status(status: str) -> str:
    """Map hh.ru status text to one of: invitations, discard, pending."""
    s = (status or "").lower()
    if any(k in s for k in ("приглаш", "пригласил", "интервью", "собеседован", "оффер", "офер")):
        return "invitations"
    if any(k in s for k in ("отказ", "не подош", "отклонил", "решил остановить")):
        return "discard"
    # everything else ("думают", "просмотрено", "не просмотрено", "новый") → noise
    return "pending"


class HHPlaywright:
    """Playwright-based hh.ru automation for login, apply, messages."""

    def __init__(self):
        self._logged_in = False
        self._page: Page | None = None

    async def _get_page(self) -> Page:
        if self._page and not self._page.is_closed():
            return self._page
        self._page = await browser_manager.new_page("hh")
        return self._page

    async def login(self) -> bool:
        """Login to hh.ru using saved session or credentials."""
        if self._logged_in:
            return True

        page = await self._get_page()

        # Check if already logged in via saved session (cookies)
        try:
            await page.goto(HH_BASE, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)

            # Check for user menu (means logged in)
            logged = await page.query_selector('[data-qa="mainmenu_applicantProfile"]')
            if not logged:
                # Try alternative selectors for logged-in state
                logged = await page.query_selector('[data-qa="mainmenu_myResumes"]')
            if not logged:
                logged = await page.query_selector('a[href*="/applicant/resumes"]')

            if logged:
                self._logged_in = True
                log.info("hh_already_logged_in")
                await browser_manager.save_context("hh")
                return True

            # Save screenshot for debugging
            await self._save_debug_screenshot(page, "login_check")
            log.warning("hh_session_expired", url=page.url)

        except Exception as e:
            log.warning("hh_login_check_error", error=str(e))

        # Need to login with credentials
        if not settings.hh_login or not settings.hh_password:
            log.error("hh_credentials_missing")
            return False

        try:
            await page.goto(HH_LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2000)

            # Click "Войти с паролем" if available
            pwd_btn = await page.query_selector('[data-qa="expand-login-by-password"]')
            if pwd_btn:
                await pwd_btn.click()
                await page.wait_for_timeout(1000)

            # Fill login
            login_input = await page.query_selector('[data-qa="login-input-username"]')
            if login_input:
                await login_input.fill(settings.hh_login)
            else:
                login_input = await page.query_selector('input[name="login"]')
                if login_input:
                    await login_input.fill(settings.hh_login)

            await page.wait_for_timeout(500)

            # Fill password
            pwd_input = await page.query_selector('[data-qa="login-input-password"]')
            if pwd_input:
                await pwd_input.fill(settings.hh_password)
            else:
                pwd_input = await page.query_selector('input[type="password"]')
                if pwd_input:
                    await pwd_input.fill(settings.hh_password)

            await page.wait_for_timeout(500)

            # Click submit
            submit_btn = await page.query_selector('[data-qa="account-login-submit"]')
            if submit_btn:
                await submit_btn.click()
            else:
                await page.keyboard.press("Enter")

            # Wait for navigation
            await page.wait_for_timeout(5000)

            # Check if login was successful
            logged = await page.query_selector('[data-qa="mainmenu_applicantProfile"]')
            if not logged:
                logged = await page.query_selector('[data-qa="mainmenu_myResumes"]')
            if logged:
                self._logged_in = True
                await browser_manager.save_context("hh")
                log.info("hh_login_success")
                return True

            # Save screenshot showing the failure
            await self._save_debug_screenshot(page, "login_failed")

            error_el = await page.query_selector('[data-qa="account-login-error"]')
            if error_el:
                error_text = await error_el.inner_text()
                log.error("hh_login_failed", reason=error_text)
            else:
                log.error("hh_login_failed", reason="unknown, possibly captcha")

            return False

        except Exception as e:
            log.error("hh_login_error", error=str(e))
            return False

    async def _save_debug_screenshot(self, page: Page, name: str):
        """Save debug screenshot to data/ directory."""
        try:
            path = f"data/debug_{name}.png"
            await page.screenshot(path=path, full_page=False)
            log.info("debug_screenshot_saved", path=path)
        except Exception:
            pass

    async def apply_to_vacancy(self, vacancy_url: str, cover_letter: str, screenshot_name: str | None = None) -> bool | str:
        """Apply to vacancy via Playwright browser automation.

        Handles employer questions/test tasks: extracts question text,
        asks Claude AI to generate an answer, fills it in.

        If screenshot_name is given, saves screenshots before+after submit
        as data/test_apply_{screenshot_name}_{before,after}.png — used by
        /test_apply command to give visual feedback.
        """
        if not self._logged_in:
            if not await self.login():
                return False

        # If the cached page is closed/crashed, recreate it
        if self._page and self._page.is_closed():
            self._page = None
        page = await self._get_page()

        try:
            try:
                await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_err:
                # Page may have crashed — recreate and retry once
                err_str = str(nav_err)
                if "Target page" in err_str or "frame was detached" in err_str or "ERR_ABORTED" in err_str:
                    log.warning("hh_apply_page_recover", error=err_str[:100])
                    try:
                        if self._page and not self._page.is_closed():
                            await self._page.close()
                    except Exception:
                        pass
                    self._page = None
                    page = await self._get_page()
                    await page.goto(vacancy_url, wait_until="domcontentloaded", timeout=45000)
                else:
                    raise
            await random_delay(2, 4)

            # If hh redirected to /applicant/vacancy_response — skip clicking apply
            if "/applicant/vacancy_response" not in page.url:
                # Try multiple selectors and JS text-search as fallback
                apply_btn = None
                for sel in (
                    '[data-qa="vacancy-response-link-top"]',
                    '[data-qa="vacancy-response-link-bottom"]',
                    'a[data-qa*="vacancy-response-link"]',
                    'button[data-qa*="vacancy-response"]',
                    'a[href*="/applicant/vacancy_response"]',
                ):
                    apply_btn = await page.query_selector(sel)
                    if apply_btn and await apply_btn.is_visible():
                        break
                    apply_btn = None
                if not apply_btn:
                    # JS text-search as last resort
                    handle = await page.evaluate_handle(
                        """() => {
                            const all = document.querySelectorAll('a, button');
                            for (const el of all) {
                                const t = (el.innerText || '').trim().toLowerCase();
                                if (t === 'откликнуться' && el.offsetParent !== null) return el;
                            }
                            return null;
                        }"""
                    )
                    if await handle.evaluate("el => !!el"):
                        apply_btn = handle.as_element()

                if not apply_btn:
                    # Already applied check
                    for sel in (
                        '[data-qa="vacancy-response-link-view-topic"]',
                        'a[href*="/applicant/negotiations/topic"]',
                    ):
                        applied_el = await page.query_selector(sel)
                        if applied_el:
                            await self._save_debug_screenshot(page, "already_applied")
                            log.info("hh_already_applied", url=vacancy_url)
                            return "already"
                    # Text-search "Перейти к переписке"
                    has_already = await page.evaluate(
                        """() => {
                            const all = document.querySelectorAll('a, button');
                            for (const el of all) {
                                const t = (el.innerText || '').trim().toLowerCase();
                                if (t.includes('перейти к переписке') || t.includes('вы уже откликались')) return true;
                            }
                            return false;
                        }"""
                    )
                    if has_already:
                        log.info("hh_already_applied_text", url=vacancy_url)
                        return "already"
                    await self._save_debug_screenshot(page, "apply_no_btn")
                    log.warning("hh_apply_btn_not_found", url=vacancy_url, page_url=page.url)
                    return False

                try:
                    async with page.expect_navigation(timeout=10000, wait_until="domcontentloaded"):
                        await apply_btn.click()
                except PlaywrightTimeout:
                    # No navigation — modal opened instead, that's fine
                    pass
                await page.wait_for_timeout(2000)

            # We're now either on the vacancy_response page or in the response modal
            await self._fill_response_form(page, cover_letter, vacancy_url)

            # Submit
            submit_btn = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            if not submit_btn:
                submit_btn = await page.query_selector('[data-qa="vacancy-response-letter-submit"]')
            if not submit_btn:
                submit_btn = await page.query_selector('button[data-qa*="response-submit"]')
            if not submit_btn:
                submit_btn = await page.query_selector('.vacancy-response-popup-actions button[type="submit"]')
            if not submit_btn:
                # On the new response page — "Откликнуться" button at the bottom
                submit_btn = await page.query_selector('button:has-text("Откликнуться")')

            if submit_btn:
                if screenshot_name:
                    try:
                        await page.screenshot(path=f"data/test_apply_{screenshot_name}_before.png")
                    except Exception:
                        pass
                await submit_btn.click()

                # Wait up to 12s for any of: URL change to negotiations,
                # success element, or visible 'отклик отправлен' text
                success = False
                for _ in range(12):
                    await page.wait_for_timeout(1000)
                    if "/applicant/negotiations" in page.url or "vacancy_response_success" in page.url:
                        success = True
                        break
                    try:
                        if await page.query_selector('[data-qa="vacancy-response-link-view-topic"]'):
                            success = True
                            break
                        if await page.query_selector('[data-qa*="response-success"], [class*="response-success"]'):
                            success = True
                            break
                        # Check text on page for confirmation
                        body_text = await page.evaluate("() => document.body.innerText.slice(0, 2000)")
                        if "отклик отправлен" in body_text.lower() or "вы откликнулись" in body_text.lower():
                            success = True
                            break
                    except Exception:
                        pass

                if success:
                    if screenshot_name:
                        try:
                            await page.screenshot(path=f"data/test_apply_{screenshot_name}_after.png")
                        except Exception:
                            pass
                    log.info("hh_apply_success", url=vacancy_url, final=page.url)
                    await browser_manager.save_context("hh")
                    return True

                # Look for inline validation errors
                try:
                    err_text = await page.evaluate(
                        """() => {
                            const errs = document.querySelectorAll('[class*="error"], [data-qa*="error"]');
                            return Array.from(errs).slice(0, 5).map(e => (e.innerText || '').trim()).filter(Boolean);
                        }"""
                    )
                    if err_text:
                        log.warning("hh_apply_validation_errors", errors=err_text[:3])
                except Exception:
                    pass

            await self._save_debug_screenshot(page, "apply_fail")
            if screenshot_name:
                try:
                    await page.screenshot(path=f"data/test_apply_{screenshot_name}_after.png")
                except Exception:
                    pass
            log.warning("hh_apply_uncertain", url=vacancy_url, current_url=page.url)
            return False

        except PlaywrightTimeout:
            try:
                await self._save_debug_screenshot(page, "apply_timeout")
            except Exception:
                pass
            log.error("hh_apply_timeout", url=vacancy_url)
            return False
        except Exception as e:
            try:
                await self._save_debug_screenshot(page, "apply_error")
            except Exception:
                pass
            log.error("hh_apply_error", url=vacancy_url, error=str(e))
            return False

    async def _fill_response_form(self, page: Page, cover_letter: str, vacancy_url: str):
        """Fill cover letter, employer questions (test task), and resume picker."""
        from app.ai.claude import claude_ai
        from app.config import settings as cfg

        # 1. Find ALL textareas that look like employer-question answer fields.
        # On the new hh response page they have placeholder "Писать тут",
        # but if hh changes wording we want to be robust — take any visible
        # textarea that isn't the cover-letter textarea.
        all_textareas = await page.query_selector_all('textarea')
        question_textareas = []
        for ta in all_textareas:
            try:
                if not await ta.is_visible():
                    continue
                placeholder = (await ta.get_attribute('placeholder')) or ''
                data_qa = (await ta.get_attribute('data-qa')) or ''
                name = (await ta.get_attribute('name')) or ''
                # Skip the cover-letter textarea
                if 'letter' in data_qa.lower() or name == 'text' or 'опровод' in placeholder.lower():
                    continue
                question_textareas.append(ta)
            except Exception:
                continue
        # Fallback to old hh format with task-body blocks
        if not question_textareas:
            blocks = await page.query_selector_all('[data-qa="task-body"]')
            for b in blocks:
                ta = await b.query_selector('textarea')
                if ta:
                    question_textareas.append(ta)
        log.info("hh_question_textareas_found", count=len(question_textareas))

        for ta in question_textareas:
            try:
                # Extract question text — closest preceding label or paragraph
                question = await ta.evaluate(
                    """el => {
                        // Walk up looking for previous siblings/labels
                        let cur = el;
                        for (let i = 0; i < 6; i++) {
                            cur = cur.parentElement;
                            if (!cur) break;
                            // Look at children before the textarea
                            const labels = cur.querySelectorAll('label, p, div, span, h1, h2, h3, h4');
                            for (const node of labels) {
                                if (node.contains(el)) continue;
                                const text = (node.innerText || '').trim();
                                if (text && text.length > 5 && text.length < 500 && !text.includes('Писать тут')) {
                                    return text;
                                }
                            }
                        }
                        return '';
                    }"""
                )
                if not question:
                    log.warning("hh_question_text_empty")
                    continue

                log.info("hh_question_found", question=question[:120])

                user_msg = (
                    f"Вопрос работодателя в отклике на вакансию:\n{question}\n\n"
                    f"Контекст вакансии: {vacancy_url}\n\n"
                    "Дай чёткий короткий ответ от первого лица (2-4 предложения максимум). "
                    "Используй факты из моего резюме, не выдумывай. "
                    "Если это тестовое задание — выполни его. "
                    "Если спрашивают про зарплату — укажи от 200 000 руб. "
                    "Если спрашивают про команду — отвечай исходя из проектов в резюме."
                )
                system = (
                    "Ты — кандидат, отвечающий на вопрос работодателя при отклике на вакансию. "
                    "Используй ТОЛЬКО факты из резюме, ничего не выдумывай. "
                    "НЕ представляйся (HR видит имя в резюме).\n\n"
                    f"Профиль кандидата:\n{cfg.resume_text}"
                )
                try:
                    answer_text, _, _ = await claude_ai._call(system, user_msg, max_tokens=600)
                    answer_text = answer_text.strip()
                except Exception as e:
                    log.error("hh_answer_gen_error", error=str(e))
                    answer_text = "Готов обсудить детали на собеседовании."

                await human_type(ta, answer_text, cfg.type_delay_min, cfg.type_delay_max)
                await page.wait_for_timeout(700)
                log.info("hh_question_answered", chars=len(answer_text), q=question[:60])
            except Exception as e:
                log.warning("hh_question_fill_error", error=str(e))

        # 2. Click "Добавить сопроводительное" link if textarea is hidden
        add_letter_btn = await page.query_selector('[data-qa="vacancy-response-letter-toggle"]')
        if not add_letter_btn:
            add_letter_btn = await page.query_selector('button:has-text("Добавить сопроводительное")')
        if not add_letter_btn:
            add_letter_btn = await page.query_selector('a:has-text("Добавить сопроводительное")')
        if not add_letter_btn:
            # On the new response page the link is just "Добавить" next to "Сопроводительное письмо"
            add_letter_btn = await page.query_selector('a:has-text("Добавить"):right-of(:text("Сопроводительное письмо"))')
        if not add_letter_btn:
            add_letter_btn = await page.query_selector('button:has-text("Добавить")')
        if add_letter_btn:
            try:
                await add_letter_btn.click()
                await page.wait_for_timeout(800)
                log.info("hh_letter_toggle_clicked")
            except Exception as e:
                log.warning("hh_letter_toggle_error", error=str(e))

        # 3. Fill cover letter
        letter_area = await page.query_selector('[data-qa="vacancy-response-popup-form-letter-input"]')
        if not letter_area:
            letter_area = await page.query_selector('[data-qa="cover-letter-input"]')
        if not letter_area:
            letter_area = await page.query_selector('textarea[name="text"]')
        if not letter_area:
            letter_area = await page.query_selector('textarea[placeholder*="опроводительн"]')

        if letter_area and cover_letter:
            await human_type(letter_area, cover_letter, cfg.type_delay_min, cfg.type_delay_max)
            await page.wait_for_timeout(800)
            log.info("hh_letter_filled", chars=len(cover_letter))
        elif cover_letter:
            log.warning("hh_letter_area_not_found")

        # 3. Resume picker (if multiple resumes)
        resume_select = await page.query_selector('[data-qa="vacancy-response-popup-form-resume-dropdown"]')
        if resume_select:
            try:
                await resume_select.click()
                await page.wait_for_timeout(500)
                first_resume = await page.query_selector('[data-qa="vacancy-response-popup-form-resume-option"]')
                if first_resume:
                    await first_resume.click()
                    await page.wait_for_timeout(500)
            except Exception:
                pass

    async def check_messages(self) -> list[dict]:
        """Check negotiations/messages on hh.ru."""
        if not self._logged_in:
            if not await self.login():
                return []

        page = await self._get_page()
        messages = []

        try:
            await page.goto(HH_NEGOTIATIONS, wait_until="domcontentloaded", timeout=45000)
            try:
                await page.wait_for_selector(
                    '[data-qa="negotiations-item"], .negotiations-list-item, [data-qa="empty-negotiations"]',
                    timeout=10000,
                )
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(2000)

            items_data = await page.evaluate(
                """() => {
                    const sel = document.querySelectorAll('[data-qa="negotiations-item"], .negotiations-list-item');
                    const out = [];
                    for (const el of sel) {
                        const titleEl = el.querySelector('[data-qa="negotiations-item-title"]')
                            || el.querySelector('a[href*="/vacancy/"]');
                        const companyEl = el.querySelector('[data-qa="negotiations-item-company"]');
                        const statusEl = el.querySelector('[data-qa="negotiations-item-status"]');
                        const unreadEl = el.querySelector('.negotiations-item__unread, [data-qa="negotiations-item-unread"]');
                        out.push({
                            title: titleEl ? (titleEl.innerText || '').trim() : '',
                            href: titleEl ? titleEl.getAttribute('href') || '' : '',
                            company: companyEl ? (companyEl.innerText || '').trim() : '',
                            status: statusEl ? (statusEl.innerText || '').trim() : '',
                            has_unread: !!unreadEl,
                        });
                    }
                    return out;
                }"""
            )

            for d in items_data[:20]:
                thread_id = ""
                href = d.get("href", "")
                if href:
                    m = re.search(r"/(\d+)/?$", href)
                    if m:
                        thread_id = f"hh_{m.group(1)}"
                if not d.get("title") and not d.get("status"):
                    continue
                messages.append({
                    "platform": "hh",
                    "title": d.get("title", ""),
                    "company": d.get("company", ""),
                    "status": d.get("status", ""),
                    "text": f"Статус: {d.get('status','')}" if d.get("status") else "",
                    "thread_id": thread_id,
                    "sender": d.get("company", ""),
                    "has_unread": d.get("has_unread", False),
                })

            log.info("hh_messages_fetched", count=len(messages))

        except Exception as e:
            log.error("hh_messages_error", error=str(e))

        return messages

    async def _parse_negotiation_item(self, item) -> dict | None:
        """Parse a single negotiation row from the page."""
        title_el = await item.query_selector('[data-qa="negotiations-item-title"]')
        if not title_el:
            title_el = await item.query_selector('a[href*="/vacancy/"]')

        title = await title_el.inner_text() if title_el else ""
        href = await title_el.get_attribute("href") if title_el else ""

        company_el = await item.query_selector('[data-qa="negotiations-item-company"]')
        company = await company_el.inner_text() if company_el else ""

        status_el = await item.query_selector('[data-qa="negotiations-item-status"]')
        status = await status_el.inner_text() if status_el else ""

        # Extract thread ID from href
        thread_id = ""
        if href:
            tid_match = re.search(r"/(\d+)/?$", href)
            if tid_match:
                thread_id = f"hh_{tid_match.group(1)}"

        # Check for new/unread messages indicator
        unread_el = await item.query_selector('.negotiations-item__unread, [data-qa="negotiations-item-unread"]')
        has_unread = unread_el is not None

        if not title and not status:
            return None

        return {
            "platform": "hh",
            "title": title.strip(),
            "company": company.strip(),
            "status": status.strip(),
            "text": f"Статус: {status.strip()}" if status else "",
            "thread_id": thread_id,
            "sender": company.strip(),
            "has_unread": has_unread,
        }

    async def check_negotiations_status(self) -> list[dict]:
        """Check the status of all active negotiations (invites, rejections, etc.).

        hh.ru ignores ?state= URL params in the new chat widget — we fetch
        the page once and classify each chat by its status text instead.
        """
        if not self._logged_in:
            if not await self.login():
                return []

        page = await self._get_page()
        statuses = []

        # Single fetch — categorize by status text
        tabs = [("all", HH_NEGOTIATIONS)]

        for tab_name, url in tabs:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=45000)
                # Wait for content to settle before scraping
                try:
                    await page.wait_for_selector(
                        '[data-qa="negotiations-item"], .negotiations-list-item, [data-qa="empty-negotiations"]',
                        timeout=10000,
                    )
                except PlaywrightTimeout:
                    pass
                await page.wait_for_timeout(2000)

                # Extract all items via single JS evaluation — avoids stale element handles
                items_data = await page.evaluate(
                    """() => {
                        const sel = document.querySelectorAll('[data-qa="negotiations-item"], .negotiations-list-item');
                        // If no items found by data-qa, try generic — find any link list inside main
                        const out = [];
                        let firstHtml = '';
                        for (let i = 0; i < sel.length; i++) {
                            const el = sel[i];
                            if (i === 0) {
                                firstHtml = (el.outerHTML || '').substring(0, 1500);
                            }
                            const titleEl = el.querySelector('[data-qa="negotiations-item-title"]')
                                || el.querySelector('a[href*="/vacancy/"]')
                                || el.querySelector('a');
                            const companyEl = el.querySelector('[data-qa="negotiations-item-company"]');
                            const statusEl = el.querySelector('[data-qa="negotiations-item-status"]');
                            const unreadEl = el.querySelector('.negotiations-item__unread, [data-qa="negotiations-item-unread"]');
                            // Collect ALL links inside the item — we'll pick the topic one in Python
                            const allLinks = Array.from(el.querySelectorAll('a')).map(a => a.getAttribute('href') || '').filter(Boolean);
                            out.push({
                                title: titleEl ? (titleEl.innerText || '').trim() : '',
                                href: titleEl ? titleEl.getAttribute('href') || '' : '',
                                all_links: allLinks,
                                company: companyEl ? (companyEl.innerText || '').trim() : '',
                                status: statusEl ? (statusEl.innerText || '').trim() : '',
                                has_unread: !!unreadEl,
                            });
                        }
                        return {items: out, sample_html: firstHtml};
                    }"""
                )
                if isinstance(items_data, dict):
                    if items_data.get("sample_html"):
                        log.info("hh_neg_sample_html", tab=tab_name, html=items_data["sample_html"][:800])
                    items_data = items_data.get("items", [])

                for d in items_data[:20]:
                    thread_id = ""
                    topic_url = ""
                    href = d.get("href", "")
                    all_links = d.get("all_links", []) or []

                    # Find topic link among all links
                    for link in all_links:
                        if "topicId=" in link or "/negotiations/item" in link:
                            topic_url = link
                            break

                    # Extract topicId from topic_url
                    m = re.search(r"topicId=(\d+)", topic_url)
                    if not m:
                        m = re.search(r"/negotiations/(?:item/)?(\d+)", topic_url)
                    if m:
                        thread_id = f"hh_{m.group(1)}"
                    elif href:
                        m2 = re.search(r"/(\d+)/?$", href)
                        if m2:
                            thread_id = f"hh_{m2.group(1)}"
                    if not d.get("title") and not d.get("status"):
                        continue
                    # Build absolute negotiation URL
                    full_topic_url = ""
                    if topic_url:
                        full_topic_url = topic_url if topic_url.startswith("http") else f"https://hh.ru{topic_url}"
                    statuses.append({
                        "platform": "hh",
                        "tab": _classify_status(d.get("status", "")),
                        "title": d.get("title", ""),
                        "company": d.get("company", ""),
                        "status": d.get("status", ""),
                        "text": f"Статус: {d.get('status','')}" if d.get("status") else "",
                        "thread_id": thread_id,
                        "topic_url": full_topic_url,
                        "sender": d.get("company", ""),
                        "has_unread": d.get("has_unread", False),
                    })

            except Exception as e:
                log.warning("hh_negotiations_tab_error", tab=tab_name, error=str(e))

        log.info("hh_negotiations_status", total=len(statuses),
                 invites=sum(1 for s in statuses if s["tab"] == "invitations"),
                 discards=sum(1 for s in statuses if s["tab"] == "discard"),
                 pending=sum(1 for s in statuses if s["tab"] == "pending"))
        return statuses

    async def is_logged_in(self) -> bool:
        """Cheap check: visit /applicant/resumes and see if we get redirected
        to login. Returns True only if we land on a real applicant page."""
        page = await self._get_page()
        try:
            await page.goto(
                "https://hh.ru/applicant/resumes",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            await page.wait_for_timeout(2000)
            url = page.url
            if "/account/login" in url or "/auth/" in url:
                self._logged_in = False
                return False
            # Look for user menu / resume list
            for sel in (
                '[data-qa="mainmenu-user"]',
                '[data-qa="mainmenu_myResumes"]',
                '[data-qa="resume"]',
            ):
                if await page.query_selector(sel):
                    self._logged_in = True
                    return True
            self._logged_in = False
            return False
        except Exception as e:
            log.warning("hh_login_check_error", error=str(e))
            return False

    async def bump_resumes(self) -> int:
        """Click 'Поднять в поиске' on all resumes. Returns number bumped."""
        if not self._logged_in:
            if not await self.login():
                return 0

        # Recreate page to avoid stale crashed state
        if self._page and not self._page.is_closed():
            try:
                await self._page.close()
            except Exception:
                pass
            self._page = None

        page = await self._get_page()
        bumped = 0

        try:
            await page.goto(HH_RESUMES, wait_until="domcontentloaded", timeout=60000)
            # Wait for the resume container to actually render before scanning
            try:
                await page.wait_for_selector(
                    '[data-qa="resume-update-button_actions"], [data-qa="resume"], main',
                    timeout=15000,
                )
            except PlaywrightTimeout:
                pass
            await page.wait_for_timeout(3000)

            # Find all "Поднять в поиске" buttons (free bump available)
            buttons = await page.query_selector_all('[data-qa="resume-update-button_actions"]')
            if not buttons:
                buttons = await page.query_selector_all('button:has-text("Поднять в поиске")')

            for btn in buttons:
                try:
                    is_disabled = await btn.get_attribute("disabled")
                    if is_disabled is not None:
                        continue
                    await btn.click()
                    await page.wait_for_timeout(2000)
                    bumped += 1
                    log.info("hh_resume_bumped")
                    await random_delay(2, 5)
                except Exception as e:
                    log.warning("hh_resume_bump_btn_error", error=str(e))

            if bumped > 0:
                await browser_manager.save_context("hh")

            log.info("hh_resumes_bump_complete", count=bumped)

        except Exception as e:
            log.error("hh_resume_bump_error", error=str(e))

        return bumped

    async def send_thanks_via_clicks(self, max_count: int = 3) -> int:
        """Open the chatik widget, find rejection chats and send thanks.

        Diagnostic-first: tries once, saves screenshots at every step.
        """
        if not self._logged_in:
            if not await self.login():
                return 0

        page = await self._get_page()
        sent = 0

        try:
            # 1. Go to main page so the chatik activator is in navbar
            await page.goto(HH_BASE, wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            await self._save_debug_screenshot(page, "thanks_step1_home")

            # 2. Click "Чаты" activator in navbar to open the widget
            activator = await page.query_selector('[data-qa="chatikActivator-button"]')
            if not activator:
                activator = await page.query_selector('[data-qa*="chatik-activator"]')
            if not activator:
                await self._save_debug_screenshot(page, "thanks_step2_no_activator")
                log.warning("hh_thanks_no_activator")
                return 0

            await activator.click()
            await page.wait_for_timeout(5500)
            await self._save_debug_screenshot(page, "thanks_step2_widget_open")

            # The chatik widget renders inside a cross-origin iframe
            # https://chatik.hh.ru/?platform=xhh&dest=iframe
            # Playwright lets us drive it via page.frames
            chatik_frame = None
            for f in page.frames:
                if "chatik" in (f.url or ""):
                    chatik_frame = f
                    break
            if not chatik_frame:
                log.warning("hh_thanks_no_chatik_frame", urls=[f.url for f in page.frames][:8])
                return 0
            log.info("hh_thanks_chatik_frame", url=chatik_frame.url)
            ctx = chatik_frame

            # Search inside the chatik iframe
            click_result = await chatik_frame.evaluate(
                """() => {
                    function* deepNodes(root) {
                        yield root;
                        if (root.shadowRoot) {
                            for (const n of deepNodes(root.shadowRoot)) yield n;
                        }
                        const kids = root.children || [];
                        for (const c of kids) {
                            for (const n of deepNodes(c)) yield n;
                        }
                    }

                    const matches = [];
                    for (const el of deepNodes(document.body)) {
                        if (!el.children || el.children.length > 0) continue;
                        const t = (el.textContent || el.innerText || '').trim();
                        if (!t || t.length > 30) continue;
                        if (/^Отказ$/i.test(t)) {
                            // Check visibility — climb up checking offsetParent
                            let v = el;
                            while (v && !v.offsetParent && v !== document.body) {
                                if (v.parentElement) v = v.parentElement; else break;
                            }
                            matches.push(el);
                        }
                    }

                    // Also check iframes
                    const iframes = [];
                    for (const f of document.querySelectorAll('iframe')) {
                        try {
                            iframes.push({src: f.src, has_doc: !!f.contentDocument});
                        } catch (e) {
                            iframes.push({src: f.src, error: e.message});
                        }
                    }

                    if (!matches.length) return {count: 0, iframes};

                    // Climb up to find clickable card
                    let target = matches[0];
                    for (let i = 0; i < 12; i++) {
                        if (!target.parentElement) break;
                        const p = target.parentElement;
                        const cs = window.getComputedStyle(p);
                        if (cs.cursor === 'pointer' || p.getAttribute('role') === 'button' || p.hasAttribute('tabindex')) {
                            target = p;
                            break;
                        }
                        target = p;
                    }
                    try { target.scrollIntoView({block: 'center'}); } catch(e) {}
                    target.click();
                    return {
                        count: matches.length,
                        clicked: true,
                        tag: target.tagName,
                        cls: (target.className || '').toString().slice(0, 80),
                        iframes: iframes
                    };
                }"""
            )
            log.info("hh_thanks_click_result", info=click_result)

            if not click_result or not click_result.get("count"):
                await self._save_debug_screenshot(page, "thanks_step3_no_chats")
                return 0

            await page.wait_for_timeout(4500)
            await self._save_debug_screenshot(page, "thanks_step3_chat_open")

            # Dump all chatik-* data-qa to discover real selectors
            chatik_info = await page.evaluate(
                """() => {
                    const all = document.querySelectorAll('[data-qa*="chatik"], [class*="chatik"]');
                    const found = new Set();
                    for (const el of all) {
                        const dq = el.getAttribute('data-qa');
                        if (dq) found.add('data-qa:' + dq);
                        const cls = el.className;
                        if (typeof cls === 'string') {
                            for (const c of cls.split(/\\s+/)) {
                                if (c.includes('chatik')) found.add('class:' + c);
                            }
                        }
                    }
                    // Also list every textarea / contenteditable on the page
                    const inputs = [];
                    for (const el of document.querySelectorAll('textarea, [contenteditable="true"]')) {
                        inputs.push({
                            tag: el.tagName,
                            dq: el.getAttribute('data-qa') || '',
                            placeholder: el.getAttribute('placeholder') || '',
                            visible: el.offsetParent !== null,
                            inChatik: !!el.closest('[data-qa*="chatik"], [class*="chatik"]'),
                        });
                    }
                    return {chatik: Array.from(found).slice(0, 40), inputs: inputs.slice(0, 10)};
                }"""
            )
            log.info("hh_thanks_chatik_dump", info=chatik_info)

            # Look for chat input — in chatik widget OR any visible textarea
            input_selectors = [
                '[data-qa="chatik-new-message-text"]',
                '[data-qa*="chatik"] textarea',
                '[data-qa*="chatik"] [contenteditable="true"]',
                '[class*="chatik"] textarea',
                '[class*="chatik"] [contenteditable="true"]',
                'textarea[placeholder*="Сообщение" i]',
                'textarea[placeholder*="сообщение" i]',
                'textarea[placeholder*="Введите" i]',
                'div[contenteditable="true"]',
                'textarea',
            ]
            chat_input = None
            for sel in input_selectors:
                try:
                    el = await ctx.query_selector(sel)
                    if el:
                        visible = await el.is_visible()
                        if visible:
                            chat_input = el
                            log.info("hh_thanks_input_found", selector=sel, in_frame=bool(chat_frame))
                            break
                except Exception:
                    pass

            if not chat_input:
                await self._save_debug_screenshot(page, "thanks_step3_no_input")
                log.warning("hh_thanks_no_input", url=page.url)
                return 0

            await chat_input.fill("Спасибо за обратную связь! Желаю успехов в подборе кандидата.")
            await page.wait_for_timeout(1500)
            await self._save_debug_screenshot(page, "thanks_step4_filled")

            # Try send button (inside chatik widget)
            send_selectors = [
                '[data-qa="chatik-do-send-message"]',
                '[data-qa*="chatik"] button[type="submit"]',
                '[data-qa*="chatik"] button:has-text("Отправить")',
                '[class*="chatik"] button[type="submit"]',
                'button:has-text("Отправить")',
            ]
            send_btn = None
            for sel in send_selectors:
                try:
                    el = await ctx.query_selector(sel)
                    if el:
                        send_btn = el
                        log.info("hh_thanks_send_btn_found", selector=sel)
                        break
                except Exception:
                    pass

            if not send_btn:
                await self._save_debug_screenshot(page, "thanks_step5_no_send_btn")
                log.warning("hh_thanks_no_send_btn")
                return 0

            await send_btn.click()
            await page.wait_for_timeout(3500)
            await self._save_debug_screenshot(page, "thanks_step6_after_send")
            sent = 1
            log.info("hh_thanks_done", sent=sent)
            return sent

        except Exception as e:
            try:
                await self._save_debug_screenshot(page, "thanks_overall_error")
            except Exception:
                pass
            log.error("hh_thanks_overall_error", error=str(e))
            return sent

    async def _try_send_thanks_on_current_page(self) -> bool:
        """We are on a negotiation chat page. Try to send the thanks message."""
        page = self._page
        if not page or page.is_closed():
            return False

        try:
            chat_input = await page.query_selector('[data-qa="chatik-new-message-text"]')
            if not chat_input:
                chat_input = await page.query_selector('textarea[placeholder*="Сообщение"]')
            if not chat_input:
                chat_input = await page.query_selector('textarea[name="message"]')
            if not chat_input:
                chat_input = await page.query_selector('div[contenteditable="true"]')

            if not chat_input:
                await self._save_debug_screenshot(page, "thanks_no_input")
                log.info("hh_thanks_no_input", url=page.url)
                return False

            await chat_input.fill("Спасибо за обратную связь! Желаю успехов в подборе кандидата.")
            await page.wait_for_timeout(800)

            send_btn = await page.query_selector('[data-qa="chatik-do-send-message"]')
            if not send_btn:
                send_btn = await page.query_selector('button:has-text("Отправить")')
            if not send_btn:
                send_btn = await page.query_selector('button[type="submit"]')
            if not send_btn:
                await self._save_debug_screenshot(page, "thanks_no_send_btn")
                return False

            await send_btn.click()
            await page.wait_for_timeout(2500)
            return True

        except Exception as e:
            log.warning("hh_thanks_send_error", error=str(e))
            return False

    async def send_rejection_thanks(self, negotiation_url: str) -> bool:
        """Send a 'thanks for feedback' message in a rejected negotiation chat.
        This keeps the resume active in hh.ru rankings."""
        if not self._logged_in:
            if not await self.login():
                return False

        page = await self._get_page()

        try:
            await page.goto(negotiation_url, wait_until="domcontentloaded", timeout=45000)
            await random_delay(2, 4)

            # Find chat input
            chat_input = await page.query_selector('[data-qa="chatik-new-message-text"]')
            if not chat_input:
                chat_input = await page.query_selector('textarea[placeholder*="Сообщение"]')
            if not chat_input:
                chat_input = await page.query_selector('textarea[name="message"]')

            if not chat_input:
                await self._save_debug_screenshot(page, "chat_input_not_found")
                log.warning("hh_chat_input_not_found", url=negotiation_url, page_url=page.url)
                return False

            message = "Спасибо за обратную связь! Желаю успехов в подборе кандидата."
            await chat_input.fill(message)
            await page.wait_for_timeout(1000)

            # Find send button
            send_btn = await page.query_selector('[data-qa="chatik-do-send-message"]')
            if not send_btn:
                send_btn = await page.query_selector('button[type="submit"]')

            if send_btn:
                await send_btn.click()
                await page.wait_for_timeout(2000)
                await browser_manager.save_context("hh")
                log.info("hh_thanks_sent", url=negotiation_url)
                return True

            log.warning("hh_send_btn_not_found", url=negotiation_url)
            return False

        except Exception as e:
            log.error("hh_thanks_error", url=negotiation_url, error=str(e))
            return False

    async def close(self):
        """Close page and save session."""
        if self._page and not self._page.is_closed():
            await browser_manager.save_context("hh")
            await self._page.close()
            self._page = None
        self._logged_in = False


# Singleton - created only when Playwright is available
hh_playwright: HHPlaywright | None = None

try:
    from app.utils.browser import browser_manager  # noqa: F811
    hh_playwright = HHPlaywright()
except ImportError:
    pass
