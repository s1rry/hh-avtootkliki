"""Отправка фирменных картинок с подписью (с откатом на текст).

Картинки кэшируются по file_id: первая отправка заливает файл в Telegram и
запоминает выданный file_id, дальше отправляем по нему — без повторной
загрузки (важно на RU-сервере, где бот ходит через медленный SOCKS-туннель).
"""
from __future__ import annotations

from pathlib import Path

import structlog
from aiogram.types import FSInputFile, Message

log = structlog.get_logger()

ASSETS = Path(__file__).resolve().parent.parent / "assets"

# name -> file_id (живёт в памяти процесса; сбрасывается при рестарте).
_FILE_ID_CACHE: dict[str, str] = {}


async def send_photo_or_text(message: Message, name: str, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Фото name.png с подписью text. Если файла нет или подпись >1024 — просто текст."""
    p = ASSETS / f"{name}.png"
    if not (p.exists() and len(text) <= 1024):
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

    photo = _FILE_ID_CACHE.get(name) or FSInputFile(p)
    try:
        sent = await message.answer_photo(photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        if sent.photo:
            _FILE_ID_CACHE[name] = sent.photo[-1].file_id
    except Exception as e:
        # Протух file_id или иная ошибка — откат на текст, не блокируем ответ.
        log.warning("send_photo_failed", name=name, error=str(e)[:120])
        _FILE_ID_CACHE.pop(name, None)
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
