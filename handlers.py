# handlers.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.enums import ChatAction
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    BotCommand,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery,
)
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from zoneinfo import ZoneInfo

from db import (
    Database,
    User,
    Alert,
    upsert_user_basic,
    create_alert,
    list_alerts,
    get_alert,
    disable_alert,
)
from text import phrases
from fsm import AlertCreate
from alerts import AlertScheduler, build_cron_from_repeat, cron_trigger, job_id_for

# ---------- i18n helpers ----------
def get_lang(m: Message | CallbackQuery) -> str:
    from_user = m.from_user if isinstance(m, Message) else m.from_user
    code = (from_user and from_user.language_code) or "en"
    return "ru" if code and code.startswith("ru") else "en"


def T(locale: str, key: str, **fmt) -> str:
    """Get string from `phrases` with fallback to English."""
    val = phrases.get(locale, {}).get(key) or phrases["en"].get(key) or key
    return val.format(**fmt)


def T_item(locale: str, key: str, subkey: str) -> str:
    """Get nested item e.g. phrases[locale]['help_items']['start']."""
    return phrases.get(locale, {}).get(key, {}).get(subkey) \
        or phrases["en"].get(key, {}).get(subkey, subkey)


# ---------- keyboards ----------

def main_kb(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        resize_keyboard=True,
        keyboard=[
            [KeyboardButton(text=T(lang, "kb_create"))],
            [KeyboardButton(text=T(lang, "kb_list")), KeyboardButton(text=T(lang, "kb_profile"))],
        ],
    )


def content_type_kb(lang: str) -> InlineKeyboardMarkup:
    btn = lambda key, data: InlineKeyboardButton(text=T(lang, key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("ctype_text", "ctype:text"), btn("ctype_photo", "ctype:photo")],
        [btn("ctype_video", "ctype:video"), btn("ctype_voice", "ctype:voice")],
        [btn("ctype_audio", "ctype:audio"), btn("ctype_document", "ctype:document")],
        [btn("ctype_video_note", "ctype:video_note")],
    ])


def repeat_kb(lang: str) -> InlineKeyboardMarkup:
    btn = lambda key, data: InlineKeyboardButton(text=T(lang, key), callback_data=data)
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("repeat_once", "repeat:once"), btn("repeat_daily", "repeat:daily")],
        [btn("repeat_weekly", "repeat:weekly"), btn("repeat_monthly", "repeat:monthly")],
        [btn("repeat_cron", "repeat:cron")],
    ])


def alerts_list_kb(lang: str, alerts: list[Alert]) -> InlineKeyboardMarkup:
    buttons = []
    for a in alerts:
        title = a.title or f"Alert #{a.id}"
        buttons.append([InlineKeyboardButton(text=title, callback_data=f"alert:open:{a.id}")])
    if not buttons:
        buttons = [[InlineKeyboardButton(text=T(lang, "kb_back"), callback_data="alert:back")]]
    else:
        buttons.append([InlineKeyboardButton(text=T(lang, "kb_back"), callback_data="alert:back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def alert_detail_kb(lang: str, alert_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=T(lang, "kb_back"), callback_data="alert:back"),
         InlineKeyboardButton(text=T(lang, "kb_delete"), callback_data=f"alert:delete:{alert_id}")],
    ])


# ---------- commands install ----------

async def install_bot_commands(bot: Bot, lang: str = "en") -> None:
    items = phrases[lang]["help_items"]
    cmds = [
        BotCommand(command="start", description=items["start"]),
        BotCommand(command="help", description=items["help"]),
        BotCommand(command="profile", description=items["profile"]),
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot commands installed", extra={"lang": lang})


# ---------- profile ----------

@dataclass
class Profile:
    user: Optional[User]
    currency: str
    balance_xtr: Decimal


async def get_profile(session: AsyncSession, *, tg_user_id: int) -> Profile:
    """Return user profile with balance."""
    user = (await session.execute(
        select(User).where(User.user_id == tg_user_id)
    )).scalar_one_or_none()
    currency = "XTR"
    balance = user.balance if (user and user.balance is not None) else Decimal("0")
    return Profile(user=user, currency=currency, balance_xtr=balance)


# ---------- helpers ----------

def parse_dt(text: str) -> Optional[datetime]:
    try:
        # Expect "YYYY-MM-DD HH:MM"
        return datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except Exception:
        return None


def human_next_fire(alert: Alert) -> str:
    """Compute next fire time (best-effort) for info screens."""
    now_utc = datetime.now(timezone.utc)
    if alert.kind == "one":
        if alert.run_at_utc and alert.run_at_utc > now_utc:
            return alert.run_at_utc.isoformat()
        return "-"
    try:
        trig = cron_trigger(alert.cron or "* * * * *", alert.tz)
        nxt = trig.get_next_fire_time(None, now_utc.astimezone(ZoneInfo(alert.tz)))
        return nxt.isoformat() if nxt else "-"
    except Exception:
        return "-"


def periodicity_human(lang: str, alert: Alert, dt_local: Optional[datetime] = None) -> str:
    if alert.kind == "one":
        return T(lang, "repeat_once")
    cron = (alert.cron or "").strip()
    return f"CRON: <code>{cron}</code>"


# ---------- main router ----------

def build_router(db: Database, scheduler: AlertScheduler, default_tz: str) -> Router:
    """Primary router: start/help/profile + alert CRUD flow."""
    r = Router()

    # ----- /start, /help, /profile -----

    @r.message(Command("start"))
    async def cmd_start(m: Message):
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
        await m.answer(f"<b>{T(lang, 'start_title')}</b>\n{T(lang, 'start_desc')}", reply_markup=main_kb(lang))

    @r.message(Command("help"))
    async def cmd_help(m: Message):
        lang = get_lang(m)
        items = phrases[lang]["help_items"]
        lines = [f"<b>{T(lang, 'help_header')}</b>"]
        for cmd, desc in items.items():
            lines.append(f"/{cmd} — {desc}")
        await m.answer("\n".join(lines), reply_markup=main_kb(lang))

    @r.message(Command("profile"))
    async def cmd_profile(m: Message):
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
                T(lang, "profile_line_balance", balance=prof.balance_xtr),
            ]
        )
        await m.answer(text, reply_markup=main_kb(lang))

    # ----- Main menu buttons -----

    @r.message(F.text.in_({phrases["ru"]["kb_create"], phrases["en"]["kb_create"]}))
    async def on_kb_create(m: Message, state: FSMContext):
        lang = get_lang(m)
        await state.clear()
        await state.update_data(tmp_owner=m.from_user.id)
        await state.set_state(AlertCreate.waiting_title)
        await m.answer(
            T(lang, "create_title"),
            reply_markup=ReplyKeyboardMarkup(
                resize_keyboard=True,
                keyboard=[
                    [KeyboardButton(text=T(lang, "kb_skip"))],
                    [KeyboardButton(text=T(lang, "kb_cancel"))],
                ],
            ),
        )


    @r.message(F.text.in_({phrases["ru"]["kb_list"], phrases["en"]["kb_list"]}))
    async def on_kb_list(m: Message):
        lang = get_lang(m)
        async with db.session() as s:
            rows = await list_alerts(s, m.from_user.id, active_only=True)

        if not rows:
            await m.answer(T(lang, "alerts_empty"), reply_markup=main_kb(lang))
            return

        await m.answer(
            T(lang, "alerts_header"),
            reply_markup=alerts_list_kb(lang, rows),
        )


    @r.message(F.text.in_({phrases["ru"]["kb_profile"], phrases["en"]["kb_profile"]}))
    async def on_kb_profile(m: Message):
        await cmd_profile(m)


    # ----- Alert create flow -----
    @r.message(AlertCreate.waiting_title)
    async def step_title(m: Message, state: FSMContext):
        lang = get_lang(m)
        text = (m.text or "").strip()
        title = None if text == T(lang, "kb_skip") else (text if text != T(lang, "kb_cancel") else None)

        if text == T(lang, "kb_cancel"):
            await state.clear()
            await m.answer(T(lang, "create_cancelled"), reply_markup=main_kb(lang))
            return

        await state.update_data(title=title)
        await state.set_state(AlertCreate.waiting_content_type)
        await m.answer(T(lang, "create_choose_type"), reply_markup=content_type_kb(lang))

    @r.callback_query(F.data.startswith("ctype:"))
    async def choose_content_type(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        ctype = cb.data.split(":", 1)[1]
        await state.update_data(content_type=ctype)
        await state.set_state(AlertCreate.waiting_content)

        prompts = {
            "text": "create_send_text",
            "photo": "create_send_photo",
            "video": "create_send_video",
            "voice": "create_send_voice",
            "audio": "create_send_audio",
            "document": "create_send_document",
            "video_note": "create_send_video_note",
        }
        await cb.message.edit_reply_markup()  # remove inline
        await cb.message.answer(T(lang, prompts.get(ctype, "create_send_text")))

    # content catch-all for this state
    @r.message(AlertCreate.waiting_content)
    async def step_content(m: Message, state: FSMContext):
        lang = get_lang(m)
        data = await state.get_data()
        ctype = data.get("content_type")
        payload: dict = {"parse_mode": "HTML"}

        try:
            if ctype == "text":
                if not m.text:
                    await m.answer(T(lang, "create_send_text"))
                    return
                payload["text"] = m.text

            elif ctype == "photo" and m.photo:
                payload["file_id"] = m.photo[-1].file_id
                if m.caption:
                    payload["caption"] = m.caption

            elif ctype == "video" and m.video:
                payload["file_id"] = m.video.file_id
                if m.caption:
                    payload["caption"] = m.caption

            elif ctype == "voice" and m.voice:
                payload["file_id"] = m.voice.file_id
                if m.caption:
                    payload["caption"] = m.caption

            elif ctype == "audio" and m.audio:
                payload["file_id"] = m.audio.file_id
                if m.caption:
                    payload["caption"] = m.caption

            elif ctype == "document" and m.document:
                payload["file_id"] = m.document.file_id
                if m.caption:
                    payload["caption"] = m.caption

            elif ctype == "video_note" and m.video_note:
                payload["file_id"] = m.video_note.file_id

            else:
                await m.answer(T(lang, "create_choose_type"))
                return
        except Exception as e:
            logger.warning(f"Failed to extract media: {e!r}")
            await m.answer(T(lang, "create_choose_type"))
            return

        await state.update_data(content_json=payload)
        await state.set_state(AlertCreate.waiting_datetime)
        await m.answer(T(lang, "create_enter_dt"))

    @r.message(AlertCreate.waiting_datetime)
    async def step_datetime(m: Message, state: FSMContext):
        lang = get_lang(m)
        dt = parse_dt(m.text or "")
        if not dt:
            await m.answer(T(lang, "errors_invalid_dt"))
            return

        await state.update_data(dt_local_naive=dt)
        await state.set_state(AlertCreate.waiting_timezone)

        # Suggest few tzs
        tz_kb = ReplyKeyboardMarkup(
            resize_keyboard=True,
            keyboard=[
                [KeyboardButton(text="UTC"), KeyboardButton(text="Europe/Moscow")],
                [KeyboardButton(text="Europe/Copenhagen")],
                [KeyboardButton(text=T(lang, "kb_skip")), KeyboardButton(text=T(lang, "kb_cancel"))],
            ],
        )
        await m.answer(T(lang, "create_enter_tz", tz="UTC"), reply_markup=tz_kb)

    @r.message(AlertCreate.waiting_timezone)
    async def step_timezone(m: Message, state: FSMContext):
        lang = get_lang(m)
        text = (m.text or "").strip()
        if text == T(lang, "kb_cancel"):
            await state.clear()
            await m.answer(T(lang, "create_cancelled"), reply_markup=main_kb(lang))
            return

        # default UTC
        tz = "UTC" if text == T(lang, "kb_skip") else text
        try:
            ZoneInfo(tz)
        except Exception:
            await m.answer(T(lang, "errors_invalid_tz"))
            return

        await state.update_data(tz=tz)
        await state.set_state(AlertCreate.waiting_repeat)
        await m.answer(T(lang, "create_choose_repeat"), reply_markup=repeat_kb(lang))

    @r.callback_query(F.data.startswith("repeat:"))
    async def step_repeat(cb: CallbackQuery, state: FSMContext, bot: Bot):
        lang = get_lang(cb)
        choice = cb.data.split(":", 1)[1]
        await cb.message.edit_reply_markup()

        data = await state.get_data()
        title = data.get("title")
        ctype = data["content_type"]
        content_json = data["content_json"]
        tz = data.get("tz", "UTC")
        dt_local_naive: datetime = data["dt_local_naive"]

        # localize
        dt_local = dt_local_naive.replace(tzinfo=ZoneInfo(tz))
        now_local = datetime.now(ZoneInfo(tz))
        if dt_local <= now_local and choice != "cron":
            await cb.message.answer(T(lang, "errors_past_dt"))
            return

        kind = "one"
        run_at_utc: Optional[datetime] = None
        cron: Optional[str] = None

        if choice == "once":
            kind = "one"
            run_at_utc = dt_local.astimezone(timezone.utc)

        elif choice in {"daily", "weekly", "monthly"}:
            kind = "cron"
            cron = build_cron_from_repeat(choice, dt_local)

        elif choice == "cron":
            # ask for custom cron
            await state.set_state(AlertCreate.waiting_cron)
            await cb.message.answer(T(lang, "create_enter_cron"))
            await state.update_data(_pending_build=dict(
                title=title, ctype=ctype, content_json=content_json, tz=tz, dt_local_iso=dt_local.isoformat()
            ))
            return

        # create alert
        async with db.session() as s:
            alert = await create_alert(
                s,
                owner_user_id=cb.from_user.id,
                title=title,
                content_type=ctype,
                content_json=content_json,
                kind=kind,
                run_at_utc=run_at_utc,
                cron=cron,
                tz=tz,
                enabled=True,
            )

        # schedule
        scheduler._add_job_for_alert(alert)

        # done
        await state.clear()
        summary = T(
            lang,
            "alert_info",
            title=alert.title or f"Alert #{alert.id}",
            content_type=T(lang, f"ctype_{alert.content_type}") if f"ctype_{alert.content_type}" in phrases[lang] else alert.content_type,
            periodicity="once" if alert.kind == "one" else f"CRON: <code>{alert.cron}</code>",
            tz=alert.tz,
            next=human_next_fire(alert),
            created=str(alert.created_at),
            id=alert.id,
        )
        await cb.message.answer(T(lang, "create_ok", summary=summary), reply_markup=main_kb(lang))

    @r.message(AlertCreate.waiting_cron)
    async def step_cron(m: Message, state: FSMContext):
        lang = get_lang(m)
        cron = (m.text or "").strip()
        data = await state.get_data()
        pend = data.get("_pending_build") or {}
        tz = pend.get("tz", "UTC")
        try:
            cron_trigger(cron, tz)  # validate
        except Exception:
            await m.answer(T(lang, "errors_invalid_cron"))
            return

        # reconstruct dt_local only for info; not needed for scheduling
        async with db.session() as s:
            alert = await create_alert(
                s,
                owner_user_id=m.from_user.id,
                title=pend.get("title"),
                content_type=pend["ctype"],
                content_json=pend["content_json"],
                kind="cron",
                run_at_utc=None,
                cron=cron,
                tz=tz,
                enabled=True,
            )

        scheduler._add_job_for_alert(alert)
        await state.clear()
        summary = T(
            lang,
            "alert_info",
            title=alert.title or f"Alert #{alert.id}",
            content_type=T(lang, f"ctype_{alert.content_type}") if f"ctype_{alert.content_type}" in phrases[lang] else alert.content_type,
            periodicity=f"CRON: <code>{alert.cron}</code>",
            tz=alert.tz,
            next=human_next_fire(alert),
            created=str(alert.created_at),
            id=alert.id,
        )
        await m.answer(T(lang, "create_ok", summary=summary), reply_markup=main_kb(lang))

    # ----- Alert list / detail / delete -----

    @r.callback_query(F.data.startswith("alert:"))
    async def on_alert_callbacks(cb: CallbackQuery):
        lang = get_lang(cb)
        parts = cb.data.split(":")
        action = parts[1]

        if action == "open":
            alert_id = int(parts[2])
            async with db.session() as s:
                alert = await get_alert(s, alert_id, cb.from_user.id)
            if not alert:
                await cb.answer("Not found", show_alert=True)
                return
            text = T(
                lang, "alert_info",
                title=alert.title or f"Alert #{alert.id}",
                content_type=T(lang, f"ctype_{alert.content_type}") if f"ctype_{alert.content_type}" in phrases[lang] else alert.content_type,
                periodicity=periodicity_human(lang, alert),
                tz=alert.tz,
                next=human_next_fire(alert),
                created=str(alert.created_at),
                id=alert.id,
            )
            await cb.message.edit_text(text, reply_markup=alert_detail_kb(lang, alert.id))

        elif action == "back":
            async with db.session() as s:
                rows = await list_alerts(s, cb.from_user.id, active_only=True)
            if not rows:
                await cb.message.edit_text(T(lang, "alerts_empty"))
            else:
                await cb.message.edit_text(T(lang, "alerts_header"), reply_markup=alerts_list_kb(lang, rows))

        elif action == "delete":
            alert_id = int(parts[2])
            async with db.session() as s:
                ok = await disable_alert(s, alert_id, cb.from_user.id)
            try:
                scheduler.scheduler.remove_job(job_id_for(alert_id))
            except Exception:
                pass
            await cb.message.edit_text(T(lang, "deleted"))

    return r
