import json
from pathlib import Path

from playwright.async_api import async_playwright, Browser, BrowserContext, Page
import structlog

from app.config import settings
from app.utils.anti_detect import random_user_agent, random_viewport

log = structlog.get_logger()

STORAGE_DIR = Path("data/browser_sessions")
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Подстроки ошибок, после которых стоит пересоздать браузер/контекст
_DEAD_MARKERS = (
    "target page, context or browser has been closed",
    "browser has been closed",
    "browser closed",
    "browser has disconnected",
    "target closed",
    "connection closed",
    "context or browser",
)


def _looks_dead(err: BaseException) -> bool:
    msg = str(err).lower()
    return any(m in msg for m in _DEAD_MARKERS)


_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    # Memory savings for 1GB RAM VPS
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-features=TranslateUI,BlinkGenPropertyTrees",
    "--disable-ipc-flooding-protection",
    "--memory-pressure-off",
    "--js-flags=--max-old-space-size=512",
]


class BrowserManager:
    def __init__(self):
        self._playwright = None
        self._browser: Browser | None = None
        self._contexts: dict[str, BrowserContext] = {}

    async def _ensure_browser(self) -> None:
        """Гарантирует, что у нас живой playwright и подключённый browser.
        Если процесс умер (OOM на 1ГБ VPS) — поднимаем заново и сбрасываем
        контексты, потому что они принадлежали мёртвому браузеру."""
        if self._browser is not None and self._browser.is_connected():
            return

        # Старый браузер мёртв — контексты уже невалидны
        if self._contexts:
            log.warning("browser_dead_dropping_contexts", count=len(self._contexts))
            self._contexts.clear()

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self._browser = await self._playwright.chromium.launch(
            headless=settings.browser_headless,
            args=_LAUNCH_ARGS,
        )
        log.info("browser_launched", headless=settings.browser_headless)

    async def start(self):
        # Тонкая обёртка для совместимости — реальный launch в _ensure_browser
        await self._ensure_browser()

    async def _build_context(self, platform: str) -> BrowserContext:
        storage_path = STORAGE_DIR / f"{platform}_state.json"
        ctx_opts = {
            "user_agent": random_user_agent(),
            "viewport": random_viewport(),
            "locale": "ru-RU",
            "timezone_id": "Europe/Moscow",
        }
        if settings.proxy_url:
            ctx_opts["proxy"] = {"server": settings.proxy_url}
        if storage_path.exists():
            ctx_opts["storage_state"] = str(storage_path)

        ctx = await self._browser.new_context(**ctx_opts)
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU', 'ru', 'en-US', 'en']});
            window.chrome = {runtime: {}};
        """)
        self._contexts[platform] = ctx
        log.info("browser_context_created", platform=platform)
        return ctx

    async def get_context(self, platform: str) -> BrowserContext:
        await self._ensure_browser()
        if platform in self._contexts:
            return self._contexts[platform]
        return await self._build_context(platform)

    async def save_context(self, platform: str):
        if platform not in self._contexts:
            return
        try:
            storage_path = STORAGE_DIR / f"{platform}_state.json"
            state = await self._contexts[platform].storage_state()
            storage_path.write_text(json.dumps(state), encoding="utf-8")
            log.info("browser_state_saved", platform=platform)
        except Exception as e:
            # Мёртвый контекст — сохранять нечего, но и падать не надо
            if _looks_dead(e):
                log.warning("browser_state_save_skip_dead", platform=platform)
            else:
                log.warning("browser_state_save_error", platform=platform, error=str(e))

    async def new_page(self, platform: str) -> Page:
        """Открывает страницу с self-healing: если браузер/контекст умерли —
        один раз полностью пересобираем стек и повторяем."""
        for attempt in (1, 2):
            try:
                ctx = await self.get_context(platform)
                return await ctx.new_page()
            except Exception as e:
                if attempt == 2 or not _looks_dead(e):
                    raise
                log.warning("browser_new_page_recover", platform=platform, error=str(e)[:160])
                # Полный сброс: контекст + браузер. _ensure_browser поднимет заново.
                self._contexts.pop(platform, None)
                if self._browser is not None:
                    try:
                        await self._browser.close()
                    except Exception:
                        pass
                self._browser = None
        # unreachable, но mypy/линтер хочет return
        raise RuntimeError("new_page: unreachable")

    async def close(self):
        for platform in list(self._contexts):
            await self.save_context(platform)
            try:
                await self._contexts[platform].close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
        log.info("browser_closed")


browser_manager = BrowserManager()
