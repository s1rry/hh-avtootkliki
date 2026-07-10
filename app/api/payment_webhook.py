"""
HTTP-вебхук уведомлений ЮMoney (мультиюзер, Фаза 6).

ЮMoney шлёт POST (form-urlencoded) на этот эндпоинт при оплате. Проверяем
sha1-подпись, поднимаем тариф пользователю (по label=jh_<telegram_id>) и
уведомляем его в Telegram. Сервер поднимается рядом с ботом в main.py.
"""
from __future__ import annotations

import structlog
from aiohttp import web

from app.config import settings
from app.database import async_session
from app.services.payments import verify_yoomoney, user_id_from_label, apply_payment

log = structlog.get_logger()


async def _yoomoney_handler(request: web.Request) -> web.Response:
    data = dict(await request.post())
    if not verify_yoomoney(data):
        log.warning("yoomoney_bad_signature")
        return web.Response(text="bad signature", status=400)

    telegram_id = user_id_from_label(data.get("label", ""))
    if not telegram_id:
        log.warning("yoomoney_no_label", label=data.get("label"))
        return web.Response(text="OK")  # 200, чтобы ЮMoney не долбил повторами

    try:
        amount = int(float(data.get("amount", "0")))
    except ValueError:
        amount = 0

    async with async_session() as session:
        applied = await apply_payment(
            session, telegram_id, provider="yoomoney", amount=amount,
            days=settings.subscription_days, operation_id=data.get("operation_id"),
        )

    if applied:
        bot = request.app.get("bot")
        if bot:
            try:
                await bot.send_message(
                    telegram_id,
                    "✅ Оплата получена, расширенный тариф активирован. Спасибо!",
                )
            except Exception as e:
                log.warning("yoomoney_notify_failed", error=str(e))
    return web.Response(text="OK")


def create_payment_app(bot) -> web.Application:
    app = web.Application()
    app["bot"] = bot
    app.router.add_post("/yoomoney", _yoomoney_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    return app
