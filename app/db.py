from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

log = logging.getLogger(__name__)

Session: async_sessionmaker[AsyncSession]
engine = None


def utcnow() -> datetime:
    """Наивное UTC-время: одинаково работает в PostgreSQL и SQLite (тесты)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    balance_cents: Mapped[int] = mapped_column(BigInteger, default=0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    lang: Mapped[str | None] = mapped_column(String(8), nullable=True)   # выбранный язык; None — язык по умолчанию
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))            # stars | premium
    recipient: Mapped[str] = mapped_column(String(64))
    stars_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    months: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hex_product: Mapped[str] = mapped_column(String(32))     # stars | premium_3m | ...

    pay_method: Mapped[str] = mapped_column(String(16))      # transfer | balance
    requisite: Mapped[str | None] = mapped_column(String(64), nullable=True)
    base_cents: Mapped[int] = mapped_column(BigInteger)
    tail_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger)
    quoted_nano: Mapped[int] = mapped_column(BigInteger)     # оценка цены Hex на момент продажи
    rate: Mapped[str] = mapped_column(String(32))
    markup: Mapped[str] = mapped_column(String(32))

    # awaiting_payment | proof_sent | approved | hex_waiting | price_review |
    # delivering | done | failed | cancelled | rejected
    status: Mapped[str] = mapped_column(String(24), index=True, default="awaiting_payment")
    proof_attempts: Mapped[int] = mapped_column(Integer, default=0)
    admin_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    hex_custom_id: Mapped[str] = mapped_column(String(64))
    hex_created: Mapped[bool] = mapped_column(Boolean, default=False)
    hex_total_nano: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hex_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hex_attempts: Mapped[int] = mapped_column(Integer, default=0)
    hex_errors: Mapped[int] = mapped_column(Integer, default=0)
    price_ceiling_nano: Mapped[int] = mapped_column(BigInteger, default=0)
    deposit_alerted: Mapped[bool] = mapped_column(Boolean, default=False)
    review_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    refunded: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Topup(Base):
    __tablename__ = "topups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(BigInteger)
    tail_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(BigInteger)     # столько клиент переводит и столько зачисляется
    requisite: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # awaiting_payment | proof_sent | credited | cancelled | rejected
    status: Mapped[str] = mapped_column(String(24), index=True, default="awaiting_payment")
    proof_attempts: Mapped[int] = mapped_column(Integer, default=0)
    admin_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    credited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ProofHash(Base):
    __tablename__ = "proof_hashes"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


async def init_db(url: str) -> None:
    global engine, Session
    kwargs: dict = {}
    if url.startswith("sqlite"):
        kwargs = {"poolclass": StaticPool, "connect_args": {"check_same_thread": False}}
    engine = create_async_engine(url, **kwargs)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _migrate(conn)


async def _migrate(conn) -> None:
    """Мини-миграции: create_all не добавляет колонки в уже существующие таблицы."""
    cols = await conn.run_sync(lambda c: [col["name"] for col in inspect(c).get_columns("users")])
    if "lang" not in cols:
        await conn.execute(text("ALTER TABLE users ADD COLUMN lang VARCHAR(8)"))
        log.info("migration: added users.lang")
