# db.py
from __future__ import annotations

import enum
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional, Any, Callable

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
    and_,
    delete,
    update as sa_update,
)
from sqlalchemy.ext.asyncio import (
    create_async_engine,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- optional deps for scheduling ---
try:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.date import DateTrigger
except Exception:  # pragma: no cover - allow import without apscheduler installed
    AsyncIOScheduler = object  # type: ignore
    DateTrigger = object  # type: ignore

try:
    # python-dateutil for RRULE parsing
    from dateutil.rrule import rrulestr, rrule  # type: ignore
except Exception:  # pragma: no cover
    rrulestr = None
    rrule = None


# ======================================================================
# Base
# ======================================================================

class Base(DeclarativeBase):
    pass


# ======================================================================
# Enums
# ======================================================================

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


class ReminderKind(str, enum.Enum):
    """
    How the reminder will be sent.
    - text: send text/caption only
    - copy: copyMessage from original chat/message
    - media: send one or multiple attachments (with optional caption)
    """
    text = "text"
    copy = "copy"
    media = "media"


class ReminderStatus(str, enum.Enum):
    active = "active"
    paused = "paused"
    done = "done"
    cancelled = "cancelled"


class MisfirePolicy(str, enum.Enum):
    """
    What to do if the scheduled time was missed (e.g., downtime):
    - send: send immediately
    - skip: skip this run, schedule the next one
    - reschedule: move this run to the nearest future
    """
    send = "send"
    skip = "skip"
    reschedule = "reschedule"


class AttachmentType(str, enum.Enum):
    photo = "photo"
    document = "document"
    video = "video"
    audio = "audio"
    voice = "voice"
    animation = "animation"
    video_note = "video_note"
    sticker = "sticker"


# ======================================================================
# Models
# ======================================================================

class User(Base):
    """Basic Telegram user info (extend as needed)."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    tg_username: Mapped[Optional[str]] = mapped_column(String(64))
    lang: Mapped[Optional[str]] = mapped_column(String(8))
    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)

    balance: Mapped[Decimal] = mapped_column(
        Numeric(18, 6), nullable=False, default=Decimal("0")
    )

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
    kind: Mapped[TransactionKind] = mapped_column(
        SAEnum(TransactionKind), nullable=False
    )
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


class Reminder(Base):
    """
    Core reminder entity. All times must be stored in UTC.
    """

    __tablename__ = "reminders"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # UUID4
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    kind: Mapped[ReminderKind] = mapped_column(SAEnum(ReminderKind), nullable=False)

    # Payload
    text: Mapped[Optional[str]] = mapped_column(Text)  # message text or caption
    original_chat: Mapped[Optional[int]] = mapped_column(BigInteger)  # for copyMessage
    original_msg: Mapped[Optional[int]] = mapped_column(Integer)

    # Scheduling
    rrule: Mapped[Optional[str]] = mapped_column(Text)  # RFC5545 RRULE (optional)
    run_at_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    next_run_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    misfire_policy: Mapped[MisfirePolicy] = mapped_column(
        SAEnum(MisfirePolicy), nullable=False, default=MisfirePolicy.send
    )
    status: Mapped[ReminderStatus] = mapped_column(
        SAEnum(ReminderStatus), nullable=False, default=ReminderStatus.active
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    attachments: Mapped[list["Attachment"]] = relationship(
        back_populates="reminder",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        Index("ix_reminders_user_id", "user_id"),
        Index("ix_reminders_status_next", "status", "next_run_utc"),
        Index("ix_reminders_chat", "chat_id"),
    )


class Attachment(Base):
    """
    Attachment bound to a reminder (order preserved by 'position').
    Prefer storing Telegram file_id; URL is optional for lazy fetch.
    """

    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    reminder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )

    type: Mapped[AttachmentType] = mapped_column(SAEnum(AttachmentType), nullable=False)

    file_id: Mapped[Optional[str]] = mapped_column(String(512))
    url: Mapped[Optional[str]] = mapped_column(Text)
    caption: Mapped[Optional[str]] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    reminder: Mapped[Reminder] = relationship(back_populates="attachments")

    __table_args__ = (
        Index("ix_attachments_reminder_pos", "reminder_id", "position"),
    )


class DeliveryLog(Base):
    """
    Delivery attempts for reminders (idempotency and diagnostics).
    attempt_no starts from 1 for each (reminder_id, planned_utc).
    """

    __tablename__ = "delivery_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    reminder_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )

    planned_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    sent_utc: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text)
    job_id: Mapped[Optional[str]] = mapped_column(String(128))

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "reminder_id", "planned_utc", "attempt_no",
            name="uq_delivery_unique_attempt",
        ),
        Index("ix_delivery_reminder_planned", "reminder_id", "planned_utc"),
    )


# ======================================================================
# Database wrapper
# ======================================================================

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


# ======================================================================
# User helpers
# ======================================================================

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


# ======================================================================
# Transactions (kept for future)
# ======================================================================

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


# ======================================================================
# Reminder helpers (CRUD + scheduling utilities)
# ======================================================================

def reminder_job_id(reminder_id: str) -> str:
    return f"reminder:{reminder_id}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_rrule(rrule_str: Optional[str], dtstart: Optional[datetime]) -> Optional[Any]:
    if not rrule_str:
        return None
    if rrulestr is None:
        # dateutil is not installed; caller should handle this gracefully
        return None
    return rrulestr(rrule_str, dtstart=dtstart)


def compute_next_run(
    *,
    rrule_str: Optional[str],
    run_at_utc: Optional[datetime],
    after: Optional[datetime] = None,
) -> Optional[datetime]:
    """
    Compute next execution datetime in UTC.
    - One-shot: returns run_at_utc if in the future; otherwise None.
    - RRULE provided: returns the first occurrence strictly after 'after' (or now).
    """
    now = after or utcnow()

    if rrule_str:
        rule = _parse_rrule(rrule_str, dtstart=run_at_utc)
        if rule is None:
            return None
        # dateutil.rrule has 'after' to get next occurrence
        return rule.after(now, inc=False)

    if run_at_utc and run_at_utc > now:
        return run_at_utc

    return None


async def create_reminder(
    session: AsyncSession,
    *,
    user_id: int,
    chat_id: int,
    kind: ReminderKind,
    text: Optional[str] = None,
    original_chat: Optional[int] = None,
    original_msg: Optional[int] = None,
    rrule_str: Optional[str] = None,
    run_at_utc: Optional[datetime] = None,
    misfire_policy: MisfirePolicy = MisfirePolicy.send,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> Reminder:
    """
    Create reminder (and attachments) and pre-compute next_run_utc.
    'attachments' items: {'type': AttachmentType|str, 'file_id': str|None,
                          'url': str|None, 'caption': str|None, 'position': int}
    """
    rid = str(uuid.uuid4())
    next_run = compute_next_run(
        rrule_str=rrule_str,
        run_at_utc=run_at_utc,
        after=None,
    )

    reminder = Reminder(
        id=rid,
        user_id=user_id,
        chat_id=chat_id,
        kind=kind,
        text=text,
        original_chat=original_chat,
        original_msg=original_msg,
        rrule=rrule_str,
        run_at_utc=run_at_utc,
        next_run_utc=next_run,
        misfire_policy=misfire_policy,
        status=ReminderStatus.active,
    )
    session.add(reminder)
    await session.flush()  # ensure PK exists for FK inserts

    if attachments:
        rows: list[Attachment] = []
        for i, a in enumerate(attachments):
            atype = a.get("type")
            if isinstance(atype, str):
                atype = AttachmentType(atype)
            rows.append(
                Attachment(
                    reminder_id=rid,
                    type=atype,  # type: ignore[arg-type]
                    file_id=a.get("file_id"),
                    url=a.get("url"),
                    caption=a.get("caption"),
                    position=int(a.get("position", i)),
                )
            )
        session.add_all(rows)

    return reminder


async def update_reminder(
    session: AsyncSession,
    reminder_id: str,
    *,
    text: Optional[str] = None,
    kind: Optional[ReminderKind] = None,
    rrule_str: Optional[str] = None,
    run_at_utc: Optional[datetime] = None,
    misfire_policy: Optional[MisfirePolicy] = None,
    status: Optional[ReminderStatus] = None,
) -> Optional[Reminder]:
    """
    Update mutable fields and recompute next_run_utc if schedule fields changed.
    """
    result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    reminder: Optional[Reminder] = result.scalar_one_or_none()
    if reminder is None:
        return None

    schedule_changed = False

    if text is not None:
        reminder.text = text
    if kind is not None:
        reminder.kind = kind
    if misfire_policy is not None:
        reminder.misfire_policy = misfire_policy
    if status is not None:
        reminder.status = status

    if rrule_str is not None:
        reminder.rrule = rrule_str
        schedule_changed = True
    if run_at_utc is not None:
        reminder.run_at_utc = run_at_utc
        schedule_changed = True

    if schedule_changed:
        reminder.next_run_utc = compute_next_run(
            rrule_str=reminder.rrule, run_at_utc=reminder.run_at_utc
        )

    return reminder


async def delete_reminder(session: AsyncSession, reminder_id: str) -> bool:
    """
    Hard delete reminder (attachments and logs will be deleted via cascade).
    """
    res = await session.execute(
        delete(Reminder).where(Reminder.id == reminder_id)
    )
    return res.rowcount > 0


async def set_reminder_status(
    session: AsyncSession, reminder_id: str, status: ReminderStatus
) -> bool:
    res = await session.execute(
        sa_update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(status=status, updated_at=func.now())
    )
    return res.rowcount > 0


async def set_next_run(
    session: AsyncSession, reminder_id: str, next_run_utc: Optional[datetime]
) -> bool:
    res = await session.execute(
        sa_update(Reminder)
        .where(Reminder.id == reminder_id)
        .values(next_run_utc=next_run_utc, updated_at=func.now())
    )
    return res.rowcount > 0


async def log_delivery_attempt(
    session: AsyncSession,
    *,
    reminder_id: str,
    planned_utc: datetime,
    ok: bool,
    error: Optional[str] = None,
    sent_utc: Optional[datetime] = None,
    job_id: Optional[str] = None,
) -> DeliveryLog:
    """
    Insert a delivery attempt with auto-incremented attempt_no per (reminder_id, planned_utc).
    """
    result = await session.execute(
        select(func.max(DeliveryLog.attempt_no)).where(
            and_(
                DeliveryLog.reminder_id == reminder_id,
                DeliveryLog.planned_utc == planned_utc,
            )
        )
    )
    current_max = result.scalar_one_or_none() or 0
    attempt_no = int(current_max) + 1

    row = DeliveryLog(
        reminder_id=reminder_id,
        planned_utc=planned_utc,
        attempt_no=attempt_no,
        sent_utc=sent_utc or (utcnow() if ok else None),
        ok=ok,
        error=error,
        job_id=job_id,
    )
    session.add(row)
    return row


# ======================================================================
# APScheduler integration helpers
# ======================================================================

async def rehydrate_scheduler_from_db(
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    *,
    job_func: Callable[..., Any],
    ahead_seconds: int = 1,
) -> int:
    """
    Load all active reminders with next_run_utc and (re)schedule them.
    Applies misfire policy for overdue jobs.
    Returns the number of jobs scheduled.
    """
    now = utcnow()
    # Read all candidates
    result = await session.execute(
        select(Reminder).where(
            and_(
                Reminder.status == ReminderStatus.active,
                Reminder.next_run_utc.is_not(None),
            )
        )
    )
    reminders: list[Reminder] = list(result.scalars().all())

    scheduled = 0
    for r in reminders:
        assert r.next_run_utc is not None
        run_time = r.next_run_utc

        if run_time <= now:
            if r.misfire_policy == MisfirePolicy.send:
                # fire asap
                run_time = now + timedelta(seconds=ahead_seconds)
            elif r.misfire_policy == MisfirePolicy.skip:
                # compute next; if none -> mark done
                nxt = compute_next_run(rrule_str=r.rrule, run_at_utc=r.run_at_utc, after=now)
                await set_next_run(session, r.id, nxt)
                if nxt is None:
                    await set_reminder_status(session, r.id, ReminderStatus.done)
                    continue
                run_time = nxt
            elif r.misfire_policy == MisfirePolicy.reschedule:
                nxt = compute_next_run(rrule_str=r.rrule, run_at_utc=r.run_at_utc, after=now)
                if nxt is None:
                    # nothing to reschedule -> mark done
                    await set_reminder_status(session, r.id, ReminderStatus.done)
                    await set_next_run(session, r.id, None)
                    continue
                await set_next_run(session, r.id, nxt)
                run_time = nxt

        _schedule_single_job(scheduler, r.id, run_time, job_func)
        scheduled += 1

    return scheduled


def _schedule_single_job(
    scheduler: AsyncIOScheduler,
    reminder_id: str,
    run_time_utc: datetime,
    job_func: Callable[..., Any],
) -> None:
    """
    Internal helper to add/replace a one-off job for a reminder.
    """
    jid = reminder_job_id(reminder_id)
    trigger = DateTrigger(run_date=run_time_utc)
    scheduler.add_job(
        job_func,
        trigger=trigger,
        id=jid,
        args=[reminder_id, run_time_utc],
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=None,  # rely on db-layer misfire policy
    )


async def schedule_new_reminder_job(
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    *,
    reminder_id: str,
    job_func: Callable[..., Any],
) -> bool:
    """
    Read reminder from DB and schedule its next_run_utc (if present).
    """
    result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    r: Optional[Reminder] = result.scalar_one_or_none()
    if r is None or r.status != ReminderStatus.active or r.next_run_utc is None:
        return False

    _schedule_single_job(scheduler, r.id, r.next_run_utc, job_func)
    return True


def remove_scheduled_job(scheduler: AsyncIOScheduler, reminder_id: str) -> None:
    jid = reminder_job_id(reminder_id)
    try:
        scheduler.remove_job(jid)
    except Exception:
        # job might not exist; ignore
        pass


async def cancel_reminder(
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    *,
    reminder_id: str,
) -> bool:
    """
    Mark reminder as cancelled and remove its scheduled job.
    """
    removed = await set_reminder_status(session, reminder_id, ReminderStatus.cancelled)
    remove_scheduled_job(scheduler, reminder_id)
    # Also clear next_run_utc for neatness
    await set_next_run(session, reminder_id, None)
    return removed


async def finalize_one_shot_after_send(
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    *,
    reminder_id: str,
) -> None:
    """
    For one-shot reminders: mark as done and remove job.
    For RRULE reminders: compute and persist the next run, re-schedule.
    """
    result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    r: Optional[Reminder] = result.scalar_one_or_none()
    if r is None:
        return

    now = utcnow()
    if r.rrule:
        nxt = compute_next_run(rrule_str=r.rrule, run_at_utc=r.run_at_utc, after=now)
        await set_next_run(session, r.id, nxt)
        if nxt is None:
            await set_reminder_status(session, r.id, ReminderStatus.done)
            remove_scheduled_job(scheduler, r.id)
        else:
            _schedule_single_job(scheduler, r.id, nxt, job_func=lambda *_: None)  # placeholder
            # Caller should re-schedule with the real job_func right after this call
    else:
        await set_next_run(session, r.id, None)
        await set_reminder_status(session, r.id, ReminderStatus.done)
        remove_scheduled_job(scheduler, r.id)
