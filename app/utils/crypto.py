"""
Шифрование чувствительных полей БД at-rest (hh-токены, Telegram-сессии).

Прозрачно через SQLAlchemy TypeDecorator EncryptedText: код читает/пишет
обычные строки, а в БД лежит зашифрованный текст. Ключ Fernet выводится из
settings.encryption_key (любая фраза → sha256 → base64). Если ключ не задан —
passthrough (без шифрования) для обратной совместимости со старыми данными.

Расшифровка «мягкая»: если значение не является валидным Fernet-токеном
(старые записи открытым текстом), возвращаем как есть — это позволяет
шифровать данные постепенно, по мере перезаписи полей.
"""
from __future__ import annotations

import base64
import hashlib

import structlog
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

from app.config import settings

log = structlog.get_logger()


def _fernet():
    key = (settings.encryption_key or "").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        log.warning("crypto_no_cryptography")
        return None
    fkey = base64.urlsafe_b64encode(hashlib.sha256(key.encode()).digest())
    return Fernet(fkey)


def encrypt(value: str | None) -> str | None:
    if value is None:
        return None
    f = _fernet()
    if not f:
        return value
    return f.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if value is None:
        return None
    f = _fernet()
    if not f:
        return value
    try:
        from cryptography.fernet import InvalidToken
        return f.decrypt(value.encode()).decode()
    except InvalidToken:
        # Старое значение открытым текстом — вернуть как есть.
        return value
    except Exception:
        return value


class EncryptedText(TypeDecorator):
    """Строковый столбец, прозрачно шифруемый в БД."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
