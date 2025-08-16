# db.py
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Iterable

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Index,
    func,
    event,
    select,
    JSON,              # ← добавьте это
    Text,              # можно оставить, если используется где-то ещё
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ---------- Base ----------

class Base(DeclarativeBase):
    pass


# ---------- Models ----------

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

    __table_args__ = (Index("ix_users_user_id", "user_id"),)


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    owner_user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)  # Telegram user_id
    title: Mapped[Optional[str]] = mapped_column(String(128))

    content_type: Mapped[str] = mapped_column(String(20), nullable=False)
    content_json: Mapped[dict] = mapped_column(JSON, nullable=False)

    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="one")  # one | cron
    run_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    cron: Mapped[Optional[str]] = mapped_column(String(128))
    tz: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------- DB wrapper ----------

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


# ---------- Users ops ----------

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


# ---------- Alerts ops ----------

async def create_alert(
    session: AsyncSession,
    *,
    owner_user_id: int,
    title: Optional[str],
    content_type: str,
    content_json: dict,
    kind: str,  # "one" | "cron"
    run_at_utc: Optional[datetime],
    cron: Optional[str],
    tz: str,
    enabled: bool = True,
) -> Alert:
    alert = Alert(
        owner_user_id=owner_user_id,
        title=title,
        content_type=content_type,
        content_json=content_json,
        kind=kind,
        run_at_utc=run_at_utc,
        cron=cron,
        tz=tz,
        enabled=enabled,
    )
    session.add(alert)
    await session.flush()
    return alert


async def get_alert(session: AsyncSession, alert_id: int, owner_user_id: Optional[int] = None) -> Optional[Alert]:
    stmt = select(Alert).where(Alert.id == alert_id)
    if owner_user_id is not None:
        stmt = stmt.where(Alert.owner_user_id == owner_user_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_alerts(session: AsyncSession, owner_user_id: int, *, active_only: bool = True) -> list[Alert]:
    stmt = select(Alert).where(Alert.owner_user_id == owner_user_id)
    if active_only:
        stmt = stmt.where(Alert.enabled.is_(True))
    stmt = stmt.order_by(Alert.created_at.desc())
    return list((await session.execute(stmt)).scalars().all())


async def disable_alert(session: AsyncSession, alert_id: int, owner_user_id: Optional[int] = None) -> bool:
    alert = await get_alert(session, alert_id, owner_user_id)
    if not alert:
        return False
    alert.enabled = False
    return True


async def record_alert_run(session: AsyncSession, alert_id: int, when: datetime) -> None:
    alert = await get_alert(session, alert_id)
    if alert:
        alert.last_run_at = when
        if alert.kind == "one":
            alert.enabled = False
