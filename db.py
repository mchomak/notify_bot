# db.py
from __future__ import annotations

import enum
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
    func,
    event,
    select,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TransactionKind(str, enum.Enum):
    purchase = "purchase"
    refund = "refund"
    payout = "payout"
    subscription = "subscription"


class TransactionStatus(str, enum.Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


class User(Base):
    """Basic Telegram user info (extend as needed)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    tg_username: Mapped[Optional[str]] = mapped_column(String(64))
    lang: Mapped[Optional[str]] = mapped_column(String(8))
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    balance: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    subscribed_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    consent_privacy: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    transactions: Mapped[list["Transaction"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_users_user_id", "user_id"),)



class Transaction(Base):
    """Generic transaction model for purchases/refunds/payouts/subscriptions."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    external_id: Mapped[Optional[str]] = mapped_column(String(128), unique=True)
    user_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[TransactionKind] = mapped_column(SAEnum(TransactionKind), nullable=False)
    status: Mapped[TransactionStatus] = mapped_column(
        SAEnum(TransactionStatus), nullable=False, default=TransactionStatus.pending
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="RUB")

    provider: Mapped[Optional[str]] = mapped_column(String(64))
    title: Mapped[Optional[str]] = mapped_column(String(256))
    meta: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    user: Mapped[Optional[User]] = relationship(back_populates="transactions")

    __table_args__ = (
        CheckConstraint("amount >= 0", name="ck_transactions_amount_non_negative"),
        Index("ix_transactions_user_id", "user_id"),
        Index("ix_transactions_external_id", "external_id"),
        Index("ix_transactions_kind_status", "kind", "status"),
    )


@dataclass
class Database:
    """
    Lightweight async SQLAlchemy wrapper for SQLite.
    Usage:
        db = await Database.create("sqlite+aiosqlite:///./data/app.db")
        async with db.session() as s: ...
    """

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]

    @classmethod
    async def create(cls, url: str) -> "Database":
        """Create engine/session and apply SQLite PRAGMAs."""
        if not url.startswith("sqlite+aiosqlite://"):
            raise ValueError("Expected URL like sqlite+aiosqlite:///path/to.db")

        engine = create_async_engine(
            url,
            echo=False,
            pool_pre_ping=True,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine.sync_engine, "connect", insert=True)
        def _set_sqlite_pragma(dbapi_connection, _):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            cursor.close()

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        return cls(engine=engine, session_factory=session_factory)

    @asynccontextmanager
    async def session(self) -> AsyncSession:
        """Auto-commit on success; rollback on error."""
        session: AsyncSession = self.session_factory()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def close(self) -> None:
        await self.engine.dispose()


async def upsert_user_basic(
    session: AsyncSession,
    *,
    user_id: int,
    tg_username: Optional[str] = None,
    lang: Optional[str] = None,
    is_premium: Optional[bool] = None,
    is_bot: Optional[bool] = None,
    last_seen_at: Optional[datetime] = None,
    consent_privacy: Optional[bool] = None,
) -> User:
    """Create or update user by Telegram user_id."""
    result = await session.execute(select(User).where(User.user_id == user_id))
    user: Optional[User] = result.scalar_one_or_none()

    if user is None:
        user = User(
            user_id=user_id,
            tg_username=tg_username,
            lang=lang,
            is_premium=bool(is_premium) if is_premium is not None else False,
            is_bot=bool(is_bot) if is_bot is not None else False,
            last_seen_at=last_seen_at,
            consent_privacy=bool(consent_privacy) if consent_privacy is not None else False,
        )
        session.add(user)
    else:
        if tg_username is not None:
            user.tg_username = tg_username
        if lang is not None:
            user.lang = lang
        if is_premium is not None:
            user.is_premium = bool(is_premium)
        if is_bot is not None:
            user.is_bot = bool(is_bot)
        if last_seen_at is not None:
            user.last_seen_at = last_seen_at
        if consent_privacy is not None:
            user.consent_privacy = bool(consent_privacy)

    return user


async def record_transaction(
    session: AsyncSession,
    *,
    user_id: Optional[int],
    kind: TransactionKind,
    amount: Decimal | float | int,
    currency: str = "RUB",
    provider: Optional[str] = None,
    status: TransactionStatus = TransactionStatus.pending,
    title: Optional[str] = None,
    external_id: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> Transaction:
    """
    Create a transaction. If external_id exists — return the existing row (idempotency).
    """
    if external_id:
        result = await session.execute(
            select(Transaction).where(Transaction.external_id == external_id)
        )
        existing: Optional[Transaction] = result.scalar_one_or_none()
        if existing:
            return existing

    tx = Transaction(
        user_id=user_id,
        kind=kind,
        amount=Decimal(str(amount)),
        currency=currency.upper(),
        provider=provider,
        status=status,
        title=title,
        external_id=external_id,
        meta=meta,
    )
    session.add(tx)
    return tx
