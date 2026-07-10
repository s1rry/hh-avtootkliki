"""
Оплата расширенного тарифа (мультиюзер).

ЮMoney: генерируем quickpay-ссылку с label=jh_<user_id>, а HTTP-уведомление
кошелька проверяем по sha1-подписи и поднимаем тариф. Крипта — вручную (админ).
"""
from __future__ import annotations

import datetime
import hashlib
import urllib.parse

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.payment import Payment
from app.models.user import User

log = structlog.get_logger()

LABEL_PREFIX = "jh_"


def build_yoomoney_url(user_id: int) -> str:
    """Ссылка на оплату ЮMoney (quickpay) с меткой пользователя."""
    params = {
        "receiver": settings.yoomoney_wallet,
        "quickpay-form": "button",
        "paymentType": "AC",  # с банковской карты
        "sum": settings.subscription_price,
        "label": f"{LABEL_PREFIX}{user_id}",
        "targets": "Расширенный тариф авто-откликов",
    }
    return "https://yoomoney.ru/quickpay/confirm?" + urllib.parse.urlencode(params)


def verify_yoomoney(p: dict) -> bool:
    """Проверить sha1-подпись HTTP-уведомления ЮMoney."""
    if not settings.yoomoney_secret:
        return False
    fields = [
        p.get("notification_type", ""), p.get("operation_id", ""), p.get("amount", ""),
        p.get("currency", ""), p.get("datetime", ""), p.get("sender", ""),
        p.get("codepro", ""), settings.yoomoney_secret, p.get("label", ""),
    ]
    digest = hashlib.sha1("&".join(fields).encode("utf-8")).hexdigest()
    return digest == (p.get("sha1_hash") or "")


def user_id_from_label(label: str) -> int | None:
    if label and label.startswith(LABEL_PREFIX):
        try:
            return int(label[len(LABEL_PREFIX):])
        except ValueError:
            return None
    return None


async def apply_payment(
    session: AsyncSession, telegram_id: int, provider: str,
    amount: int, days: int, operation_id: str | None = None,
) -> bool:
    """Поднять тариф пользователю (по telegram_id). Идемпотентно по operation_id.
    Возвращает True, если тариф применён (False — дубль или нет пользователя)."""
    if operation_id:
        dup = (await session.execute(
            select(Payment).where(Payment.operation_id == operation_id)
        )).scalar_one_or_none()
        if dup:
            log.info("payment_duplicate_ignored", operation_id=operation_id)
            return False

    user = (await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )).scalar_one_or_none()
    if not user:
        log.warning("payment_user_not_found", telegram_id=telegram_id)
        return False

    now = datetime.datetime.now(datetime.timezone.utc)
    base = user.tier_until if (user.tier_until and user.tier_until > now) else now
    user.tier = "paid"
    user.tier_until = base + datetime.timedelta(days=days)

    session.add(Payment(
        user_id=user.id, provider=provider, amount=amount,
        operation_id=operation_id, status="paid", days=days,
    ))
    await session.commit()
    log.info("payment_applied", telegram_id=telegram_id, provider=provider, until=str(user.tier_until))
    return True
