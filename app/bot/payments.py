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

log = structlog.get_logger()

router = Router()


@router.callback_query(F.data == "pay:start")
async def cb_pay_start(cb: CallbackQuery, **kw):
    price = settings.subscription_price
    days = settings.subscription_days
    lines = [f"💳 <b>Оплата расширенного тарифа</b>\n\n{price}₽ — {days} дней.\n"]
    buttons: list[list[InlineKeyboardButton]] = []

    if settings.yoomoney_wallet:
        url = build_yoomoney_url(cb.from_user.id)
        buttons.append([InlineKeyboardButton(text=f"Оплатить {price}₽ картой (ЮMoney)", url=url)])
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
