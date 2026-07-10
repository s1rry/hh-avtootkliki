"""
Подключение второго Telegram-аккаунта (userbot) через бота.

Пользователь даёт СВОИ api_id/api_hash (https://my.telegram.org/auth) и входит
своим кодом — сервис получает только сессию его аккаунта, пароль не хранит.
После входа входящие ЛС от HR на этот аккаунт пересылаются владельцу в бота.
"""
from __future__ import annotations

import structlog
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

from app.database import async_session
from app.services.user_service import get_or_create_user
from app.userbot.manager import manager

log = structlog.get_logger()

router = Router()


class UBConnect(StatesGroup):
    api_id = State()
    api_hash = State()
    phone = State()
    code = State()
    password = State()


async def _cancelled(message: Message, state: FSMContext) -> bool:
    if (message.text or "").strip().lower() in ("/cancel", "отмена"):
        await manager.stop_for_user(message.from_user.id)  # сбросить незавершённый вход
        await state.clear()
        await message.answer("Отменено.")
        return True
    return False


def status_kb(active: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardButton
    if active:
        rows = [[b(text="🔌 Отключить пересылку", callback_data="ub:disable")]]
    else:
        rows = [[b(text="📨 Подключить второй ТГ", callback_data="ub:connect")]]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("forwarding"))
async def cmd_forwarding(message: Message, **kw):
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id, message.from_user.username)
        active = user.tg_userbot_active
    await message.answer(_status_text(active), reply_markup=status_kb(active), parse_mode="HTML")


def _status_text(active: bool) -> str:
    if active:
        return (
            "📨 <b>Пересылка сообщений</b>\n\n"
            "Статус: <b>🟢 включена</b>\n\n"
            "Когда HR пишет на твой контактный ТГ, сообщение приходит сюда."
        )
    return (
        "📨 <b>Пересылка сообщений со второго ТГ</b>\n\n"
        "Укажи в письмах второй ТГ-аккаунт как контакт — а бот будет пересылать тебе "
        "сюда всё, что напишут на него HR. Личный аккаунт не светится.\n\n"
        "Понадобятся <b>api_id</b> и <b>api_hash</b> того аккаунта."
    )


@router.callback_query(F.data == "ub:menu")
async def cb_menu(cb: CallbackQuery, **kw):
    async with async_session() as session:
        user = await get_or_create_user(session, cb.from_user.id, cb.from_user.username)
        active = user.tg_userbot_active
    await cb.message.answer(_status_text(active), reply_markup=status_kb(active), parse_mode="HTML")
    await cb.answer()


@router.callback_query(F.data == "ub:disable")
async def cb_disable(cb: CallbackQuery, **kw):
    await manager.stop_for_user(cb.from_user.id, forget=True)
    await cb.message.edit_text(_status_text(False), reply_markup=status_kb(False), parse_mode="HTML")
    await cb.answer("Пересылка отключена")


@router.callback_query(F.data == "ub:connect")
async def cb_connect(cb: CallbackQuery, state: FSMContext, **kw):
    await state.clear()
    await cb.message.answer(
        "🔐 <b>Подключение второго ТГ</b>\n\n"
        "1️⃣ Зайди на <a href=\"https://my.telegram.org/auth\">my.telegram.org/auth</a> "
        "с ТОГО аккаунта (по номеру телефона).\n"
        "2️⃣ Открой раздел <b>API development tools</b>, создай приложение "
        "(любое название) — получишь <b>api_id</b> и <b>api_hash</b>.\n\n"
        "Пришли сюда <b>api_id</b> (число).\n\nОтмена: /cancel",
        parse_mode="HTML", disable_web_page_preview=True,
    )
    await state.set_state(UBConnect.api_id)
    await cb.answer()


@router.message(UBConnect.api_id)
async def on_api_id(message: Message, state: FSMContext, **kw):
    if await _cancelled(message, state):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer("api_id — это число. Пришли ещё раз или /cancel.")
        return
    await state.update_data(api_id=int(raw))
    await state.set_state(UBConnect.api_hash)
    await message.answer("Теперь пришли <b>api_hash</b> (строка из букв и цифр).", parse_mode="HTML")


@router.message(UBConnect.api_hash)
async def on_api_hash(message: Message, state: FSMContext, **kw):
    if await _cancelled(message, state):
        return
    api_hash = (message.text or "").strip()
    if len(api_hash) < 16:
        await message.answer("Не похоже на api_hash. Пришли ещё раз или /cancel.")
        return
    await state.update_data(api_hash=api_hash)
    await state.set_state(UBConnect.phone)
    await message.answer(
        "Пришли номер телефона второго аккаунта (например <code>+79991234567</code>).",
        parse_mode="HTML",
    )


@router.message(UBConnect.phone)
async def on_phone(message: Message, state: FSMContext, **kw):
    if await _cancelled(message, state):
        return
    phone = (message.text or "").strip()
    if len(phone) < 5:
        await message.answer("Не похоже на номер. Пришли ещё раз или /cancel.")
        return
    data = await state.get_data()
    await message.answer("⏳ Запрашиваю код у Telegram...")
    res = await manager.start_login(message.from_user.id, data["api_id"], data["api_hash"], phone)
    if res.get("status") != "code_sent":
        await state.clear()
        await message.answer(f"❌ Не удалось начать вход: {res.get('error')}\nПопробуй заново: /forwarding")
        return
    await state.set_state(UBConnect.code)
    await message.answer(
        "📩 Telegram прислал код (в приложение на том аккаунте). Пришли его сюда.\n\n"
        "⚠️ Вводи код через пробелы или дефис (например <code>1 2 3 4 5</code>), "
        "иначе Telegram может его аннулировать.",
        parse_mode="HTML",
    )


@router.message(UBConnect.code)
async def on_code(message: Message, state: FSMContext, **kw):
    if await _cancelled(message, state):
        return
    code = (message.text or "").replace(" ", "").replace("-", "").strip()
    await message.answer("⏳ Проверяю код...")
    res = await manager.submit_code(message.from_user.id, code)
    st = res.get("status")
    if st == "password":
        await state.set_state(UBConnect.password)
        await message.answer("Аккаунт защищён облачным паролем (2FA). Пришли пароль.")
        return
    if st == "ok":
        await state.clear()
        await _done(message, res)
        return
    await state.clear()
    await message.answer(f"❌ Код не подошёл: {res.get('error')}\nПопробуй заново: /forwarding")


@router.message(UBConnect.password)
async def on_password(message: Message, state: FSMContext, **kw):
    if await _cancelled(message, state):
        return
    res = await manager.submit_password(message.from_user.id, (message.text or "").strip())
    await state.clear()
    if res.get("status") == "ok":
        await _done(message, res)
    else:
        await message.answer(f"❌ Пароль не подошёл: {res.get('error')}\nПопробуй заново: /forwarding")


async def _done(message: Message, res: dict):
    uname = res.get("username")
    who = f" (@{uname})" if uname else ""
    await message.answer(
        f"✅ Второй ТГ{who} подключён. Входящие ЛС от HR буду присылать сюда.\n\n"
        "Не забудь указать этот аккаунт как контакт в письмах: "
        "⚙️ Настройки → ✉️ Контакт для писем."
    )
