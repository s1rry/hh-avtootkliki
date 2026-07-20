"""
Работа с пользователями и мостик между одиночным и мультиюзерным режимами.

- single (self-host): один служебный пользователь, собранный из .env. Старый
  код получает его через get_current_user() и работает как раньше.
- multi (cloud): пользователи создаются при /start, настройки — свои у каждого.
"""
from __future__ import annotations

import datetime

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.user import User
from app.models.user_settings import UserSettings

log = structlog.get_logger()


async def beta_slots(session: AsyncSession) -> tuple[int, int]:
    """(занято, всего) бета-слотов полного доступа."""
    total = settings.beta_full_access_slots
    used = (await session.execute(select(func.count(User.id)))).scalar() or 0
    return min(used, total), total


def _single_user_telegram_id() -> int:
    """ID владельца из .env (tg_admin_chat_id). 0, если не задан/не число."""
    raw = (settings.tg_admin_chat_id or "").strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _settings_from_env() -> UserSettings:
    """Собрать настройки одиночного пользователя из .env-значений."""
    return UserSettings(
        salary_min=settings.desired_salary_min or 0,
        daily_limit=settings.max_applies_per_day_hh,
        apply_hour_start=settings.notify_hour_start,
        apply_hour_end=settings.notify_hour_end,
        apply_delay_min=settings.apply_delay_min,
        apply_delay_max=settings.apply_delay_max,
    )


async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str | None = None) -> User:
    """Найти пользователя по telegram_id или создать нового (multi-режим)."""
    user = (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()
    if user is None:
        # Бета: первым N пользователям — полный доступ на beta_days.
        used = (await session.execute(select(func.count(User.id)))).scalar() or 0
        tier, tier_until = "free", None
        if used < settings.beta_full_access_slots:
            tier = "paid"
            tier_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
                days=settings.beta_days)
        user = User(
            telegram_id=telegram_id, username=username,
            settings=UserSettings().model_dump(), tier=tier, tier_until=tier_until,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        log.info("user_created", telegram_id=telegram_id, tier=tier, beta_slot=(used + 1))
    return user


async def get_current_user(session: AsyncSession) -> User:
    """
    Одиночный режим: вернуть (создав при необходимости) единственного
    пользователя из .env. Резюме и контакты берутся из настроек окружения.
    """
    tg_id = _single_user_telegram_id()
    user = (await session.execute(select(User).where(User.telegram_id == tg_id))).scalar_one_or_none()
    if user is None:
        user = User(
            telegram_id=tg_id,
            username="self-host",
            settings=_settings_from_env().model_dump(),
            resume_text=settings.resume_text or None,
            tier="paid",          # self-host — полный функционал без оплаты
            is_active=True,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        log.info("single_user_initialized", telegram_id=tg_id)
    return user
