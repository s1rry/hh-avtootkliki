"""Отправка фирменных картинок с подписью (с откатом на текст)."""
from __future__ import annotations

from pathlib import Path

from aiogram.types import FSInputFile, Message

ASSETS = Path(__file__).resolve().parent.parent / "assets"


async def send_photo_or_text(message: Message, name: str, text: str, reply_markup=None, parse_mode: str = "HTML"):
    """Фото name.png с подписью text. Если файла нет или подпись >1024 — просто текст."""
    p = ASSETS / f"{name}.png"
    if p.exists() and len(text) <= 1024:
        await message.answer_photo(FSInputFile(p), caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
