# handlers.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Iterable, Callable, Any

from aiogram import Router, F, Bot, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    BotCommand,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from db import (
    Database,
    User,
    Transaction,
    TransactionStatus,
    Reminder,
    ReminderKind,
    AttachmentType,
    MisfirePolicy,
    upsert_user_basic,
    create_reminder,
    schedule_new_reminder_job,
)
from keyboards import (
    build_main_kb,
    choose_kind_kb,
    confirm_interval_kb,
)
from timeparse import handle_time_parse_and_confirm
from utils import (
    set_expected,        # мастер-шага диалога (строковый «expected» в FSM-данных)
    get_expected,
    detect_payload,      # определение content_type + file_id + текст
)
from fsm import PaymentGuard, expect_any
from text import phrases
from tools import (
    parse_interval_with_ai,   # можно заменить позже на ваш локальный разбор
    normalize_interval_def,
    summarize_interval,
    norm_title,
)

# =========================================================
# i18n helpers
# =========================================================

def get_lang(m: Message) -> str:
    code = (m.from_user and m.from_user.language_code) or "en"
    return "ru" if code and code.startswith("ru") else "en"


def T(locale: str, key: str, **fmt) -> str:
    val = phrases.get(locale, {}).get(key) or phrases["en"].get(key) or key
    return val.format(**fmt)


def T_item(locale: str, key: str, subkey: str) -> str:
    return phrases.get(locale, {}).get(key, {}).get(subkey) or \
        phrases["en"].get(key, {}).get(subkey, subkey)


# =========================================================
# /set_my_commands
# =========================================================

async def install_bot_commands(bot: Bot, lang: str = "en") -> None:
    items = phrases[lang]["help_items"]
    cmds = [
        BotCommand(command="start", description=items["start"]),
        BotCommand(command="help", description=items["help"]),
        BotCommand(command="profile", description=items["profile"]),
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot commands installed", extra={"lang": lang})


# =========================================================
# Profile (SQLite)
# =========================================================

@dataclass
class Profile:
    user: Optional[User]
    txn_count: int
    txn_sum: Decimal
    currency: str
    balance_xtr: Decimal


async def get_profile(session: AsyncSession, *, tg_user_id: int) -> Profile:
    user = (
        await session.execute(select(User).where(User.user_id == tg_user_id))
    ).scalar_one_or_none()

    stats_row = (
        await session.execute(
            select(
                func.count(Transaction.id),
                func.coalesce(func.sum(Transaction.amount), 0),
            ).where(
                and_(
                    Transaction.user_id == tg_user_id,
                    Transaction.status == TransactionStatus.succeeded,
                )
            )
        )
    ).first()

    txn_count = int(stats_row[0] or 0)
    txn_sum = Decimal(str(stats_row[1] or 0))
    currency = "XTR"
    balance = user.balance if (user and user.balance is not None) else Decimal("0")

    return Profile(
        user=user,
        txn_count=txn_count,
        txn_sum=txn_sum,
        currency=currency,
        balance_xtr=balance,
    )


# =========================================================
# Reminders list (SQLite) + keyboard
# =========================================================

def _alerts_list_kb(reminders: Iterable[Reminder], locale: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for r in reminders:
        when = "-"
        if r.next_run_utc:
            when = r.next_run_utc.strftime("%Y-%m-%d %H:%M")
        short_id = (r.id or "")[:8]
        title = f"{when} • {short_id or '…'}"
        rows.append([InlineKeyboardButton(text=title, callback_data=f"alert:{r.id}")])

    if not rows:
        rows = [[InlineKeyboardButton(text=T(locale, "no_alerts_btn"), callback_data="noop")]]

    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _load_user_reminders(session: AsyncSession, *, tg_user_id: int) -> list[Reminder]:
    q = (
        select(Reminder)
        .where(Reminder.user_id == tg_user_id)
        .order_by(Reminder.created_at.desc())
    )
    res = await session.execute(q)
    return list(res.scalars().all())


def _menu_labels_for(column_index: int) -> set[str]:
    out: set[str] = set()
    for loc, data in phrases.items():
        btns = data.get("buttons")
        if isinstance(btns, list) and btns and isinstance(btns[0], list):
            if 0 <= column_index < len(btns[0]):
                text = btns[0][column_index]
                if isinstance(text, str):
                    out.add(text)
    return out


NEW_ALERT_LABELS = _menu_labels_for(0)
MY_ALERTS_LABELS = _menu_labels_for(1)


# =========================================================
# Helpers for saving reminder (SQL)
# =========================================================

def _map_media_to_attachment_type(media_type: str) -> AttachmentType:
    media_type = (media_type or "").lower()
    mapping = {
        "photo": AttachmentType.photo,
        "video": AttachmentType.video,
        "document": AttachmentType.document,
        "audio": AttachmentType.audio,
        "voice": AttachmentType.voice,
        "animation": AttachmentType.animation,
        "video_note": AttachmentType.video_note,
        "sticker": AttachmentType.sticker,
    }
    return mapping.get(media_type, AttachmentType.document)


async def _finalize_and_save_alert_sql(
    *,
    session: AsyncSession,
    scheduler: AsyncIOScheduler,
    job_func: Callable[..., Any],
    user_id: int,
    chat_id: int,
    fsm_state: FSMContext,
    phrases_loc: dict,
) -> str:
    """
    Сохраняет напоминание в SQLite (таблицы reminders/attachments),
    рассчитывает next_run_utc и ставит job в APScheduler.
    Возвращает строку деталей для ответа пользователю.
    """
    data = await fsm_state.get_data()
    media_type = (data.get("media_type") or "text").lower()
    file_id = data.get("file_id")
    payload_text = (data.get("payload_text") or "").strip()
    pending_kind = (data.get("pending_kind") or "one_time").lower()

    if pending_kind != "one_time":
        # Интервальные сохранения перенесём в следующий рефактор (RRULE),
        # пока защищаемся сообщением.
        logger.warning("Interval scheduling via SQL not implemented yet.")
        return phrases_loc.get("interval_confirm_error") or "Unsupported interval."

    # one-time
    run_at_iso = data.get("pending_run_at")
    if not run_at_iso:
        raise ValueError("pending_run_at is missing for one_time reminder")
    dt = datetime.fromisoformat(run_at_iso)
    if dt.tzinfo is None:
        run_at_utc = dt.replace(tzinfo=timezone.utc)
    else:
        run_at_utc = dt.astimezone(timezone.utc)

    # Готовим kind/attachments
    attachments = None
    if media_type == "text":
        kind = ReminderKind.text
        text_to_send = payload_text
    else:
        kind = ReminderKind.media
        text_to_send = payload_text or None
        if not file_id:
            raise ValueError("file_id missing for media reminder")
        attachments = [{
            "type": _map_media_to_attachment_type(media_type),
            "file_id": file_id,
            "url": None,
            "caption": None,
            "position": 0,
        }]

    # Создаём запись и считаем next_run_utc
    reminder = await create_reminder(
        session,
        user_id=user_id,
        chat_id=chat_id,
        kind=kind,
        text=text_to_send,
        rrule_str=None,
        run_at_utc=run_at_utc,
        misfire_policy=MisfirePolicy.send,
        attachments=attachments,
    )

    # Ставим job в APScheduler
    ok = await schedule_new_reminder_job(
        session,
        scheduler,
        reminder_id=reminder.id,
        job_func=job_func,
    )
    logger.debug("Scheduled reminder id={} ok={}", reminder.id, ok)

    # Готовим детали
    details = f"{phrases_loc.get('when_label', 'Когда')}: {run_at_utc.strftime('%Y-%m-%d %H:%M')} UTC"
    return details


# =========================================================
# Router: commands + menu + message flow from old messages.py
# =========================================================

def build_router(
    db: Database,
    *,
    scheduler: AsyncIOScheduler,
    job_func: Callable[..., Any],
    ai_client: Any,
    bot: Bot,
) -> Router:
    """
    Единый роутер:
      - /start, /help, /profile
      - кнопки «Новая нотификация», «Мои уведомления»
      - обработчик произвольных сообщений (логика из messages.py), но на FSM+SQL
    """
    r = Router()
    r.message.middleware(PaymentGuard())

    # ---------- Commands ----------

    @r.message(Command("start"))
    async def cmd_start(m: Message, state: FSMContext) -> None:
        lang = get_lang(m)
        async with db.session() as s:
            await upsert_user_basic(
                s,
                user_id=m.from_user.id,
                tg_username=m.from_user.username,
                lang=m.from_user.language_code,
                last_seen_at=datetime.now(timezone.utc),
                is_premium=getattr(m.from_user, "is_premium", False),
                is_bot=m.from_user.is_bot,
            )
        await state.clear()
        await expect_any(state)                     # ждём «любой» вход (текст/медиа)
        await set_expected(state, "await_media")   # маркер шага мастера
        await m.answer(T(lang, "ask_content"), reply_markup=build_main_kb(phrases[lang]))

    @r.message(Command("help"))
    async def cmd_help(m: Message) -> None:
        lang = get_lang(m)
        items = phrases[lang]["help_items"]
        lines = [f"<b>{T(lang, 'help_header')}</b>"]
        for cmd, desc in items.items():
            lines.append(f"/{cmd} — {desc}")
        await m.answer("\n".join(lines))

    @r.message(Command("profile"))
    async def cmd_profile(m: Message) -> None:
        lang = get_lang(m)
        async with db.session() as s:
            prof = await get_profile(s, tg_user_id=m.from_user.id)

        if not prof.user:
            await m.answer(T(lang, "profile_not_found"))
            return

        u = prof.user
        text = "\n".join(
            [
                f"<b>{T(lang, 'profile_title')}</b>",
                T(lang, "profile_line_id", user_id=u.user_id),
                T(lang, "profile_line_user", username=u.tg_username or "-"),
                T(lang, "profile_line_lang", lang=u.lang or "-"),
                T(lang, "profile_line_created", created=str(u.created_at) if u.created_at else "-"),
                T(lang, "profile_line_last_seen", last_seen=str(u.last_seen_at) if u.last_seen_at else "-"),
                T(lang, "profile_line_txn", count=prof.txn_count, sum=prof.txn_sum, cur=prof.currency),
                T(lang, "profile_line_balance", balance=prof.balance_xtr),
            ]
        )
        await m.answer(text)

    # ---------- Menu buttons ----------

    @r.message(F.text.in_(NEW_ALERT_LABELS))
    async def new_alert(msg: types.Message, state: FSMContext) -> None:
        lang = get_lang(msg)
        logger.debug("New notification requested by user_id={}", msg.from_user.id)
        await state.clear()
        await expect_any(state)
        await set_expected(state, "await_media")
        await msg.answer(T(lang, "ask_content"))

    @r.message(F.text.in_(MY_ALERTS_LABELS))
    async def list_alerts_handler(msg: types.Message) -> None:
        lang = get_lang(msg)
        uid = msg.from_user.id
        logger.debug("Listing alerts for user_id={}", uid)
        async with db.session() as s:
            items = await _load_user_reminders(s, tg_user_id=uid)

        if not items:
            await msg.answer(T(lang, "no_alerts"))
            return
        await msg.answer(T(lang, "alerts_header"), reply_markup=_alerts_list_kb(items, lang))

    # ---------- Unified message handler (ported from messages.py) ----------

    @r.message()
    async def handle_message(msg: types.Message, state: FSMContext) -> None:
        lang = get_lang(msg)
        data = await state.get_data()
        expected = get_expected(data)
        logger.debug(
            "handle_message user_id={} expected={} text='{}' ct={}",
            msg.from_user.id, expected, msg.text, msg.content_type,
        )

        # 1) waiting media/text
        if expected in ("", "await_media"):
            payload = detect_payload(msg)
            if not payload:
                await msg.answer(phrases[lang]["expect_media"])
                return
            media_type, file_id, payload_text = payload
            await state.update_data(
                media_type=media_type,
                file_id=file_id,
                payload_text=payload_text or "",
                pending_kind=None,
                pending_run_at=None,
                pending_interval=None,
            )
            await set_expected(state, "await_kind")
            await msg.answer(phrases[lang]["choose_kind"], reply_markup=choose_kind_kb(phrases[lang]))
            return

        # 2) interval description (free text -> AI parse -> preview)
        if expected == "await_interval":
            if not msg.text:
                await msg.answer(phrases[lang]["expect_interval_desc_need"])
                return
            parsed = await parse_interval_with_ai(msg.text, ai_client)
            if not parsed:
                await msg.answer(phrases[lang]["interval_confirm_error"])
                return
            interval_def = normalize_interval_def(parsed)
            if not interval_def:
                await msg.answer(phrases[lang]["interval_confirm_error"])
                return
            await state.update_data(pending_interval=interval_def)
            preview = summarize_interval(interval_def)
            await set_expected(state, "await_interval_confirm")
            await msg.answer(
                phrases[lang]["interval_preview"].format(preview=preview),
                reply_markup=confirm_interval_kb(phrases[lang]),
            )
            return

        # 3) concrete time for one-time reminder
        if expected == "await_time":
            if not msg.text:
                await msg.answer(phrases[lang]["expect_time"])
                return
            await handle_time_parse_and_confirm(msg, state, phrases[lang])
            return

        # 4) title input -> save to SQL and schedule
        if expected == "await_title_input":
            if not msg.text:
                await msg.answer(phrases[lang]["expect_title_input_need"])
                return
            title = norm_title(msg.text)
            if len(title) > 100:
                await msg.answer(phrases[lang]["title_too_long"])
                return

            await msg.answer(phrases[lang]["saving"])

            # SQL save + schedule
            async with db.session() as s:
                details = await _finalize_and_save_alert_sql(
                    session=s,
                    scheduler=scheduler,
                    job_func=job_func,
                    user_id=msg.from_user.id,
                    chat_id=msg.chat.id,
                    fsm_state=state,
                    phrases_loc=phrases[lang],
                )

            await state.clear()
            await msg.answer(
                phrases[lang]["scheduled"].format(
                    title=(title or "без названия"),
                    details=details,
                )
            )
            return

        # 5) fallback texts
        mapping = {
            "await_kind": phrases[lang]["expect_kind"],
            "await_time_confirm": phrases[lang]["expect_time_confirm"],
            "await_title_choice": phrases[lang]["expect_title_choice"],
        }
        await msg.answer(mapping.get(expected, phrases[lang]["generic_expect"]))

    return r
