"""
Платёж за расширенный тариф (мультиюзер).

Хранит историю и обеспечивает идемпотентность: одно и то же уведомление
ЮMoney (operation_id) не поднимает тариф дважды.
"""
from __future__ import annotations

from sqlalchemy import BigInteger, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Payment(Base, TimestampMixin):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(20))  # yoomoney | crypto | manual
    amount: Mapped[int] = mapped_column(Integer, default=0)
    # operation_id ЮMoney (уникальный) — защита от повторной обработки.
    operation_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="paid")  # paid
    days: Mapped[int] = mapped_column(Integer, default=30)
