"""Хелперы по задачам поиска (SearchTask)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.search_task import SearchTask


async def list_tasks(session: AsyncSession, user_id: int) -> list[SearchTask]:
    return list((await session.execute(
        select(SearchTask).where(SearchTask.user_id == user_id).order_by(SearchTask.id)
    )).scalars().all())


async def ensure_seeded(session: AsyncSession, user) -> None:
    """Если задач нет, но заданы старые ключевые слова — завести задачи из них."""
    existing = (await session.execute(
        select(SearchTask.id).where(SearchTask.user_id == user.id).limit(1)
    )).scalar_one_or_none()
    if existing is not None:
        return
    phrases = user.get_settings().search_phrases()
    for kw in phrases:
        session.add(SearchTask(user_id=user.id, keyword=kw, is_active=True))
    if phrases:
        await session.commit()


async def active_keywords(session: AsyncSession, user_id: int) -> list[str]:
    return list((await session.execute(
        select(SearchTask.keyword).where(
            SearchTask.user_id == user_id, SearchTask.is_active.is_(True))
        .order_by(SearchTask.id)
    )).scalars().all())


async def active_tasks(session: AsyncSession, user_id: int) -> list[SearchTask]:
    """Активные задачи целиком (ключ + своё резюме) для прогона автоотклика."""
    return list((await session.execute(
        select(SearchTask).where(
            SearchTask.user_id == user_id, SearchTask.is_active.is_(True))
        .order_by(SearchTask.id)
    )).scalars().all())
