import asyncio

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from app.config import settings
from app.database import engine
from app.models.base import Base
from app.bot.handlers import router, set_scheduler
from app.workers.scheduler import WorkerScheduler

log = structlog.get_logger()

HAS_PLAYWRIGHT = False
try:
    from app.utils.browser import browser_manager
    HAS_PLAYWRIGHT = True
except ImportError:
    pass


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    log.info("database_initialized")


async def notify_telegram(bot: Bot, text: str):
    try:
        await bot.send_message(
            chat_id=settings.tg_admin_chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        log.error("telegram_notify_error", error=str(e))


async def main():
    structlog.configure(
        processors=[
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.dev.ConsoleRenderer(),
        ],
    )

    log.info("starting_job_hunter")

    await init_db()

    session = None
    if settings.tg_api_server:
        session = AiohttpSession(api=settings.tg_api_server)
        log.info("using_custom_tg_api", api=settings.tg_api_server)
    elif settings.tg_proxy:
        from aiohttp import BasicAuth
        session = AiohttpSession(proxy=settings.tg_proxy)
        log.info("using_tg_proxy", proxy=settings.tg_proxy)
    bot = Bot(token=settings.tg_bot_token, session=session)
    dp = Dispatcher()
    dp.include_router(router)

    playwright_ok = HAS_PLAYWRIGHT
    if playwright_ok:
        try:
            await browser_manager.start()
            log.info("playwright_started")
        except Exception as e:
            playwright_ok = False
            log.warning("playwright_start_failed", error=str(e), mode="api_only")
    else:
        log.info("playwright_not_available", mode="api_only")

    scheduler = WorkerScheduler(
        notify_callback=lambda text: notify_telegram(bot, text)
    )
    set_scheduler(scheduler)
    scheduler.start()

    await notify_telegram(
        bot,
        "🚀 <b>Job Hunter запущен!</b>\n\n"
        f"Позиция: {settings.desired_position}\n"
        f"Зарплата: {settings.desired_salary_min:,}–{settings.desired_salary_max:,}\n"
        f"Интервал: {settings.check_interval_sec // 60} мин\n"
        f"Лимит: {settings.max_applies_per_day} откликов/день\n"
        f"Режим: {'Playwright' if playwright_ok else 'API-only'}",
    )

    # Wait for proxy to stabilize after boot
    if settings.tg_proxy:
        log.info("waiting_for_proxy", seconds=15)
        await asyncio.sleep(15)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.stop()
        if playwright_ok:
            await browser_manager.close()
        await engine.dispose()
        log.info("job_hunter_stopped")


if __name__ == "__main__":
    asyncio.run(main())
