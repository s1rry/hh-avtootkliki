"""
Пользователь мультиюзерного режима.

Каждый пользователь Telegram = одна строка. Хранит свою hh-авторизацию
(токены получаются через OTP-вход, т.к. официальный API для соискателей
закрыт с 15.12.2025), своё резюме и свои настройки поиска/автоотклика.

В одиночном режиме (MODE=single) создаётся один служебный пользователь
из .env — остальной код работает одинаково.
"""
from __future__ import annotations

import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.user_settings import UserSettings


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))

    # hh-авторизация (per-user). TODO: шифровать токены at-rest.
    hh_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    hh_access_token: Mapped[str | None] = mapped_column(Text)
    hh_refresh_token: Mapped[str | None] = mapped_column(Text)
    hh_token_expires: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))
    hh_resume_id: Mapped[str | None] = mapped_column(String(64))

    # Резюме (текст для писем/скоринга).
    resume_text: Mapped[str | None] = mapped_column(Text)

    # Настройки "Задачи" (см. UserSettings). Хранятся как JSON.
    settings: Mapped[dict] = mapped_column(JSON, default=lambda: UserSettings().model_dump())

    # Тариф.
    tier: Mapped[str] = mapped_column(String(16), default="free")  # free | paid
    tier_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True))

    # Активен ли автоотклик у пользователя.
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)

    def get_settings(self) -> UserSettings:
        """Разобрать JSON-настройки в типизированную схему (с дефолтами)."""
        return UserSettings(**(self.settings or {}))

    def set_settings(self, s: UserSettings) -> None:
        self.settings = s.model_dump()

    @property
    def is_paid(self) -> bool:
        if self.tier != "paid":
            return False
        if self.tier_until is None:
            return True
        return self.tier_until > datetime.datetime.now(datetime.timezone.utc)
