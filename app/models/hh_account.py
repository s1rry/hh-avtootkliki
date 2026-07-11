"""
Дополнительный hh-аккаунт пользователя (мультиаккаунт, расширенный тариф).

Первый (основной) аккаунт хранится прямо в User.hh_* — так не ломается
одиночный путь. Второй и последующие аккаунты — строки этой таблицы. Движок
автоотклика прогоняет цикл по основному аккаунту и по каждому активному
дополнительному, у каждого свой дедуп и свой дневной лимит.
"""
from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.utils.crypto import EncryptedText


class HHAccount(Base, TimestampMixin):
    __tablename__ = "hh_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    label: Mapped[str | None] = mapped_column(String(255))  # как показывать в списке

    hh_access_token: Mapped[str | None] = mapped_column(EncryptedText)
    hh_refresh_token: Mapped[str | None] = mapped_column(EncryptedText)
    hh_token_expires: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    hh_resume_id: Mapped[str | None] = mapped_column(String(64))
    resume_text: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
