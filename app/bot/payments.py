"""
Кнопки и команды оплаты расширенного тарифа (мультиюзер, Фаза 6).

pay:start — показать варианты оплаты (ЮMoney-ссылка + крипта).
/grant <user_id> [дней] — ручное подтверждение (крипта), только админ.
"""
from __future__ import annotations

import structlog
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from app.config import settings
from app.database import async_session
from app.services.payments import build_yoomoney_url, apply_payment
from app.services import yookassa

log = structlog.get_logger()

router = Router()


@router.callback_query(F.data == "pay:start")
async def cb_pay_start(cb: CallbackQuery, **kw):
    price = settings.subscription_price
    days = settings.subscription_days
    lines = [f"💳 <b>Оплата расширенного тарифа</b>\n\n{price}₽ — {days} дней.\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    paid_online = False
    if yookassa.is_configured():
        url = await yookassa.create_payment(
            cb.from_user.id, price, "Расширенный тариф авто-откликов")
        if url:
            buttons.append([InlineKeyboardButton(text=f"Оплатить {price}₽ картой", url=url)])
            paid_online = True
    if not paid_online and settings.yoomoney_wallet:
        url = build_yoomoney_url(cb.from_user.id)
        buttons.append([InlineKeyboardButton(text=f"Оплатить {price}₽ картой (ЮMoney)", url=url)])
        paid_online = True
    if paid_online:
        lines.append("После оплаты тариф поднимется автоматически в течение минуты.")
    else:
        lines.append("Онлайн-оплата картой временно недоступна.")

    crypto = []
    if settings.crypto_ton:
        crypto.append(f"TON: <code>{settings.crypto_ton}</code>")
    if settings.crypto_usdt_trc20:
        crypto.append(f"USDT (TRC20): <code>{settings.crypto_usdt_trc20}</code>")
    if crypto:
        lines.append("\nИли криптой (подтверждение вручную, напиши в поддержку после оплаты):\n" + "\n".join(crypto))

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="task:menu")])
    await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await cb.answer()


@router.message(Command("grant"))
async def cmd_grant(message: Message, **kw):
    """Ручное поднятие тарифа (крипта). Только админ: /grant <telegram_id> [дней]."""
    if str(message.chat.id) != settings.tg_admin_chat_id:
        return
    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /grant <telegram_id> [дней]")
        return
    telegram_id = int(parts[1])
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else settings.subscription_days
    async with async_session() as session:
        ok = await apply_payment(session, telegram_id, provider="crypto", amount=0, days=days)
    if ok:
        await message.answer(f"✅ Тариф поднят пользователю {telegram_id} на {days} дней.")
        try:
            await message.bot.send_message(telegram_id, "✅ Оплата подтверждена, расширенный тариф активирован. Спасибо!")
        except Exception:
            pass
    else:
        await message.answer("Не удалось (пользователь не найден).")


@router.message(Command("ai"))
async def cmd_ai(message: Message, **kw):
    """Проверить ключи скоринга. Только админ: /ai

    Дёргает каждый эндпоинт пула настоящим запросом — так видно не только
    «ключ задан», но и что он реально отвечает и не упёрся в лимит.
    """
    if str(message.chat.id) != settings.tg_admin_chat_id:
        return
    from app.ai.claude import claude_ai
    pool = claude_ai.score_pool
    if not pool:
        await message.answer("AI_SCORE_POOL пуст — скоринг идёт на платном провайдере.")
        return
    await message.answer(f"⏳ Проверяю {len(pool)} ключей...")
    lines = []
    for ep in pool:
        host = ep["base_url"].split("//")[-1].split("/")[0]
        try:
            text, _, _ = await claude_ai._call_openai_compatible(
                "Ответь одним числом.", "Сколько будет 2+2? Верни только число.",
                max_tokens=800, model=ep["model"],
                base_url=ep["base_url"], api_key=ep["api_key"])
            got = (text or "").strip()[:20]
            lines.append(f"✅ {host} ({ep['model']}) → {got or 'пустой ответ'}")
        except Exception as e:
            lines.append(f"❌ {host} ({ep['model']}) → {type(e).__name__}: {str(e)[:80]}")
    await message.answer("🔑 <b>Ключи скоринга</b>\n\n" + "\n".join(lines),
                         parse_mode="HTML")
