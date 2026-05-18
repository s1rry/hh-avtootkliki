import asyncio
import structlog
from sqlalchemy import select, func

from app.config import settings
from app.database import async_session
from app.models.vacancy import Vacancy, VacancyStatus
from app.models.application import Application, ApplicationStatus
from app.models.ai_generation import AIGeneration
from app.ai.claude import claude_ai
from app.parsers.hh import HHParser
from app.utils.anti_detect import random_delay

log = structlog.get_logger()


async def run_auto_apply(auto_mode: bool = False, min_score: float = 70):
    log.info("auto_apply_started", auto_mode=auto_mode, min_score=min_score)
    applied = 0

    # Per-platform daily limits
    platform_caps = {
        "hh": settings.max_applies_per_day_hh,
        "habr": settings.max_applies_per_day_habr,
    }
    async with async_session() as session:
        today_rows = (await session.execute(
            select(Application.platform, func.count(Application.id))
            .where(
                Application.status == ApplicationStatus.SENT,
                func.date(Application.created_at) == func.current_date(),
            )
            .group_by(Application.platform)
        )).all()
        today_by_plat = {p: c for p, c in today_rows}

        remaining_by_plat: dict[str, int] = {}
        for plat, cap in platform_caps.items():
            done = today_by_plat.get(plat, 0)
            left = max(0, cap - done)
            if left > 0:
                remaining_by_plat[plat] = left

        if not remaining_by_plat:
            log.info("daily_limit_reached", today=today_by_plat)
            return 0

        # Берём одобренные вакансии по платформам с лимитом per-platform
        # Исключаем те, что уже падали 3+ раз (бессмысленно ретраить)
        from sqlalchemy import select as _select
        failed_3plus = _select(Application.vacancy_id).where(
            Application.status == ApplicationStatus.FAILED,
        ).group_by(Application.vacancy_id).having(func.count(Application.id) >= 3)

        all_vacs = []
        for plat, limit in remaining_by_plat.items():
            result = await session.execute(
                select(Vacancy)
                .where(
                    Vacancy.platform == plat,
                    Vacancy.status == VacancyStatus.APPROVED,
                    Vacancy.ai_score >= min_score,
                    Vacancy.id.notin_(failed_3plus),
                )
                .order_by(Vacancy.ai_score.desc())
                .limit(limit)
            )
            all_vacs.extend(result.scalars().all())
        # Mix platforms a bit: interleave
        vacancies = all_vacs

    for vacancy in vacancies:
        try:
            # Генерируем сопроводительное
            letter, inp_tok, out_tok = await claude_ai.generate_cover_letter(
                vacancy.title,
                vacancy.description or "",
            )

            # Сохраняем генерацию AI
            async with async_session() as session:
                session.add(AIGeneration(
                    vacancy_id=vacancy.id,
                    gen_type="cover_letter",
                    prompt=f"Cover letter for: {vacancy.title}",
                    response=letter,
                    input_tokens=inp_tok,
                    output_tokens=out_tok,
                ))
                await session.commit()

            # Отправляем отклик с глобальным таймаутом 2 минуты
            result = False
            parser = None
            if vacancy.platform == "hh":
                parser = HHParser()
            elif vacancy.platform == "habr":
                from app.parsers.habr import HabrParser
                parser = HabrParser()

            if parser:
                try:
                    await asyncio.wait_for(parser.login(), timeout=60)
                    result = await asyncio.wait_for(
                        parser.apply_to_vacancy(vacancy.url, letter),
                        timeout=120,
                    )
                except asyncio.TimeoutError:
                    log.error("apply_timeout_global", vacancy_id=vacancy.id, url=vacancy.url)
                    result = False

            success = result is True  # True != "already"
            already = result == "already"

            # Записываем результат
            async with async_session() as session:
                if not already:
                    # Don't log application if already applied
                    app = Application(
                        vacancy_id=vacancy.id,
                        platform=vacancy.platform,
                        cover_letter=letter,
                        status=ApplicationStatus.SENT if success else ApplicationStatus.FAILED,
                        attempt_count=1,
                    )
                    session.add(app)

                v = await session.get(Vacancy, vacancy.id)
                if success:
                    v.status = VacancyStatus.APPLIED
                    applied += 1
                elif already:
                    v.status = VacancyStatus.APPLIED
                await session.commit()

            log.info(
                "apply_result",
                vacancy_id=vacancy.id,
                platform=vacancy.platform,
                success=success,
            )

            await random_delay(settings.apply_delay_min, settings.apply_delay_max)

        except Exception as e:
            log.error("apply_error", vacancy_id=vacancy.id, error=str(e))
            async with async_session() as session:
                session.add(Application(
                    vacancy_id=vacancy.id,
                    platform=vacancy.platform,
                    status=ApplicationStatus.FAILED,
                    error_message=str(e),
                    attempt_count=1,
                ))
                await session.commit()

    log.info("auto_apply_complete", applied=applied)
    return applied
