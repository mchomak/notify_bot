# handlers.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
import re

from aiogram import Router, F, Bot
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
from zoneinfo import ZoneInfo
from datetime import timezone as _utc_tz, datetime as _dt
from text import phrases
from fsm import AlertCreate
from alerts import AlertScheduler, build_cron_from_repeat, cron_trigger, job_id_for
from time_parse import parse_human_datetime, combine_day_with_time, format_dt_local
from ai_interval import ai_parse_interval_phrase


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


def _as_aware_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    # если без tz — считаем, что это уже UTC и проставляем tzinfo
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    # иначе приводим к UTC
    return dt.astimezone(timezone.utc)


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
    # if not buttons:
    #     buttons = [[InlineKeyboardButton(text=T(lang, "kb_back"), callback_data="alert:back")]]
    # else:
    #     buttons.append([InlineKeyboardButton(text=T(lang, "kb_back"), callback_data="alert:back")])
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
        run = _as_aware_utc(alert.run_at_utc)
        if run and run > now_utc:
            return run.isoformat()
        return "-"
    try:
        tz = ZoneInfo(alert.tz)
        trig = cron_trigger(alert.cron or "* * * * *", alert.tz)
        # для расчёта next нужен "сейчас" в TZ триггера
        now_local = now_utc.astimezone(tz)
        nxt = trig.get_next_fire_time(previous_fire_time=None, now=now_local)
        return nxt.isoformat() if nxt else "-"
    except Exception:
        return "-"


def periodicity_human(lang: str, alert: Alert, dt_local: Optional[datetime] = None) -> str:
    if alert.kind == "one":
        return T(lang, "repeat_once")
    cron = (alert.cron or "").strip()
    return f"CRON: <code>{cron}</code>"


def title_inline_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=T(lang, "kb_skip"), callback_data="title:skip"),
        InlineKeyboardButton(text=T(lang, "kb_cancel"), callback_data="title:cancel"),
    ]])

def sched_kind_kb(lang: str) -> InlineKeyboardMarkup:
    # две кнопки: однократно / циклично
    text_once = "Однократно" if lang == "ru" else "Once"
    text_cycle = "Циклично" if lang == "ru" else "Recurring"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text_once, callback_data="sched:once"),
        InlineKeyboardButton(text=text_cycle, callback_data="sched:cycle"),
    ]])

def confirm_time_kb(lang: str) -> InlineKeyboardMarkup:
    ok = "✅ Всё верно" if lang == "ru" else "✅ Looks good"
    fix = "✏️ Исправить" if lang == "ru" else "✏️ Edit"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=ok, callback_data="confirm:ok"),
        InlineKeyboardButton(text=fix, callback_data="confirm:edit"),
    ]])


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
        msg = await m.answer(T(lang, "create_title"), reply_markup=title_inline_kb(lang))
        await state.update_data(last_bot_mid=msg.message_id)


    @r.message(AlertCreate.waiting_title, F.text)
    async def step_title_text(m: Message, state: FSMContext):
        lang = get_lang(m)
        title = (m.text or "").strip()
        await state.update_data(title=title)
        await state.set_state(AlertCreate.waiting_content)
        await m.answer(T(lang, "create_send_text"))  # просим просто прислать содержимое (любое)


    @r.message(AlertCreate.waiting_title)
    async def step_title_any_other(m: Message, state: FSMContext):
        lang = get_lang(m)
        await state.clear()
        await m.answer(T(lang, "create_cancelled"), reply_markup=main_kb(lang))



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
    

    @r.callback_query(F.data == "title:skip")
    async def title_skip(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        await state.update_data(title=None)
        await state.set_state(AlertCreate.waiting_content)
        await cb.message.edit_reply_markup()  # убираем inline
        await cb.message.answer(T(lang, "create_send_text"))


    @r.callback_query(F.data == "title:cancel")
    async def title_cancel(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        await state.clear()
        await cb.message.edit_text(T(lang, "create_cancelled"))


    @r.message(F.text.in_({phrases["ru"]["kb_profile"], phrases["en"]["kb_profile"]}))
    async def on_kb_profile(m: Message):
        await cmd_profile(m)


    @r.message(AlertCreate.waiting_content)
    async def step_content_any(m: Message, state: FSMContext):
        lang = get_lang(m)
        payload: dict = {"parse_mode": "HTML"}
        content_type: Optional[str] = None

        if m.text:
            content_type = "text"
            payload["text"] = m.text
        elif m.photo:
            content_type = "photo"
            payload["file_id"] = m.photo[-1].file_id
            if m.caption:
                payload["caption"] = m.caption
        elif m.video:
            content_type = "video"
            payload["file_id"] = m.video.file_id
            if m.caption:
                payload["caption"] = m.caption
        elif m.voice:
            content_type = "voice"
            payload["file_id"] = m.voice.file_id
            if m.caption:
                payload["caption"] = m.caption
        elif m.audio:
            content_type = "audio"
            payload["file_id"] = m.audio.file_id
            if m.caption:
                payload["caption"] = m.caption
        elif m.document:
            content_type = "document"
            payload["file_id"] = m.document.file_id
            if m.caption:
                payload["caption"] = m.caption
        elif m.video_note:
            content_type = "video_note"
            payload["file_id"] = m.video_note.file_id

        if not content_type:
            # не поддерживаемые типы (стикеры/контакты и т.п.) — просим ещё раз
            await m.answer(T(lang, "create_send_text"))
            return

        await state.update_data(content_type=content_type, content_json=payload)
        await state.set_state(AlertCreate.waiting_sched_kind)
        msg = await m.answer(
            T(lang, "create_choose_repeat"),
            reply_markup=sched_kind_kb(lang),
        )
        await state.update_data(last_bot_mid=msg.message_id)


    @r.callback_query(F.data == "sched:once")
    async def sched_once(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        await state.set_state(AlertCreate.waiting_time_input)
        try:
            await cb.message.edit_text(
                T(lang, "create_enter_dt"),  # переиспользуем подсказку
                reply_markup=None
            )
        except Exception:
            await cb.message.answer(T(lang, "create_enter_dt"))


    @r.callback_query(F.data == "sched:cycle")
    async def sched_cycle(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        await state.set_state(AlertCreate.waiting_cycle_dt)

        prompt_ru = (
            "Опиши расписание своими словами, например:\n"
            "• «по выходным в 7 утра»\n"
            "• «каждый будний день в 09:30 и 18:00»\n"
            "• «каждые 15 минут с 10:00 до 18:00 по будням»\n"
            "Или пришли crontab (m h dom mon dow)."
        )
        prompt_en = (
            "Describe the schedule, e.g.:\n"
            "• “on weekends at 07:00”\n"
            "• “every weekday at 09:30 and 18:00”\n"
            "• “every 15 minutes 10:00–18:00 on weekdays”\n"
            "Or send a crontab (m h dom mon dow)."
        )
        await cb.message.edit_text(prompt_ru if lang == "ru" else prompt_en, reply_markup=None)


    @r.message(AlertCreate.waiting_cycle_dt)
    async def step_cycle_dt_ai(m: Message, state: FSMContext):
        lang = get_lang(m)
        data = await state.get_data()
        default_tz = data.get("tz") or "Europe/Moscow"

        text = (m.text or "").strip()
        # Если пользователь прислал уже крон-строку — пропустим ИИ и сразу создадим
        if re.match(r"^\s*([\d*/,-]+)\s+([\d*/,-]+)\s+([\d*/,-]+)\s+([\d*/,-]+)\s+([\w*/,?-]+)\s*$", text, flags=re.I):
            plan_tz = default_tz
            crons = [text]
        else:
            try:
                plan, crons = ai_parse_interval_phrase(text, lang=lang, default_tz=default_tz)
                plan_tz = plan.tz or default_tz
            except Exception as e:
                err = "Не удалось распознать расписание. Попробуйте переформулировать." if lang == "ru" else "Couldn't parse the schedule. Please rephrase."
                from loguru import logger
                logger.warning(f"AI interval parsing failed: {e!r}")
                await m.answer(err)
                return

        created_alerts = []
        async with db.session() as s:
            for cron in crons:
                alert = await create_alert(
                    s,
                    owner_user_id=m.from_user.id,
                    title=data.get("title"),
                    content_type=data["content_type"],
                    content_json=data["content_json"],
                    kind="cron",
                    run_at_utc=None,
                    cron=cron,
                    tz=plan_tz,
                    enabled=True,
                )
                created_alerts.append(alert)

        # Планируем каждую задачу
        for a in created_alerts:
            scheduler._add_job_for_alert(a)

        await state.clear()

        # Соберём краткую сводку и ЗАМЕНИМ текущее сообщение (как вы просили ранее)
        lines = []
        for a in created_alerts:
            lines.append(T(
                lang,
                "alert_info",
                title=a.title or f"Alert #{a.id}",
                content_type=T(lang, f"ctype_{a.content_type}") if f"ctype_{a.content_type}" in phrases[lang] else a.content_type,
                periodicity=f"CRON: <code>{a.cron}</code>",
                tz=a.tz,
                next=human_next_fire(a),
                created=str(a.created_at),
                id=a.id,
            ))
        summary = "\n\n".join(lines)
        # редактировать нужно ПОСЛЕДНЕЕ бот-сообщение в цепочке. Проще ответить в текущем апдейте:
        await m.reply(T(lang, "create_ok", summary=summary))


    @r.message(AlertCreate.waiting_time_input)
    async def time_input_once(m: Message, state: FSMContext):
        lang = get_lang(m)
        now_local = _dt.now(ZoneInfo("Europe/Moscow"))  # базовый now, парсер сам уточнит tz из текста если сможет
        res = parse_human_datetime(m.text or "", now_local, lang)

        if res.need_day and res.hour_min:
            await state.update_data(pending_time=res.hour_min, tz=res.tz)
            await state.set_state(AlertCreate.waiting_day)
            # уточняем день
            hint = "Укажи день: «сегодня», «завтра», день недели или дату (ДД.ММ / ГГГГ-ММ-ДД)" if lang == "ru" \
                else "Specify the day: “today”, “tomorrow”, weekday or a date (DD.MM / YYYY-MM-DD)"
            await m.answer(hint)
            return

        if not res.dt:
            await m.answer(T(lang, "errors_invalid_dt"))
            return

        # подтверждение
        human = format_dt_local(res.dt, res.tz, lang)
        await state.update_data(dt_local_iso=res.dt.isoformat(), tz=res.tz)
        await state.set_state(AlertCreate.waiting_time_confirm)
        await m.answer(human, reply_markup=confirm_time_kb(lang))


    @r.message(AlertCreate.waiting_day)
    async def time_day_disambig(m: Message, state: FSMContext):
        lang = get_lang(m)
        data = await state.get_data()
        hour_min = data.get("pending_time")  # (h, m)
        tz = data.get("tz") or "Europe/Moscow"
        now_local = _dt.now(ZoneInfo(tz))
        res = combine_day_with_time(m.text or "", hour_min, now_local, lang)

        if not res.dt:
            await m.answer(T(lang, "errors_invalid_dt"))
            return

        human = format_dt_local(res.dt, res.tz, lang)
        await state.update_data(dt_local_iso=res.dt.isoformat(), tz=res.tz)
        await state.set_state(AlertCreate.waiting_time_confirm)
        await m.answer(human, reply_markup=confirm_time_kb(lang))


    @r.callback_query(F.data.in_({"confirm:ok", "confirm:edit"}))
    async def time_confirm(cb: CallbackQuery, state: FSMContext, bot: Bot):
        lang = get_lang(cb)
        if cb.data.endswith("edit"):
            await state.set_state(AlertCreate.waiting_time_input)
            # редактируем текущее сообщение, убираем inline
            try:
                await cb.message.edit_text(T(lang, "create_enter_dt"))
            except Exception:
                # на случай, если текст уже совпадает — просто обновим без reply_markup
                await cb.message.edit_reply_markup()
            return

        # confirm ok → сохраняем one-shot alert
        data = await state.get_data()
        from datetime import timezone as _utc_tz, datetime as _dt
        dt_local = _dt.fromisoformat(data["dt_local_iso"])
        tz = data.get("tz") or "Europe/Moscow"
        run_at_utc = dt_local.astimezone(_utc_tz.utc)

        async with db.session() as s:
            alert = await create_alert(
                s,
                owner_user_id=cb.from_user.id,
                title=data.get("title"),
                content_type=data["content_type"],
                content_json=data["content_json"],
                kind="one",
                run_at_utc=run_at_utc,
                cron=None,
                tz=tz,
                enabled=True,
            )

        scheduler._add_job_for_alert(alert)
        await state.clear()

        # формируем сводку и ИМЕННО РЕДАКТИРУЕМ текущее сообщение
        summary = T(
            lang,
            "alert_info",
            title=alert.title or f"Alert #{alert.id}",
            content_type=T(lang, f"ctype_{alert.content_type}") if f"ctype_{alert.content_type}" in phrases[lang] else alert.content_type,
            periodicity=T(lang, "repeat_once"),
            tz=alert.tz,
            next=human_next_fire(alert),
            created=str(alert.created_at),
            id=alert.id,
        )
        await cb.message.edit_text(T(lang, "create_ok", summary=summary))


    @r.callback_query(F.data.startswith("repeat:"))
    async def step_repeat_cycle(cb: CallbackQuery, state: FSMContext):
        lang = get_lang(cb)
        choice = cb.data.split(":", 1)[1]
        if choice == "cron":
            await state.set_state(AlertCreate.waiting_cron)
            await cb.message.edit_text(T(lang, "create_enter_cron"), reply_markup=None)
            return

        # daily/weekly/monthly → просим день/время первого срабатывания в свободной форме
        await state.update_data(repeat_kind=choice)
        await state.set_state(AlertCreate.waiting_cycle_dt)
        ask = "Укажи день и время первого срабатывания (например: «в четверг в 17:40», «завтра в 9», «25.08 10:30»)" \
            if lang == "ru" else \
            "Provide the first run day & time (e.g., “on Thu at 17:40”, “tomorrow at 9”, “25.08 10:30”)."
        await cb.message.edit_text(ask, reply_markup=None)


    @r.message(AlertCreate.waiting_cycle_dt)
    async def step_cycle_dt(m: Message, state: FSMContext):
        lang = get_lang(m)
        data = await state.get_data()
        kind = data.get("repeat_kind")
        now_local = _dt.now(ZoneInfo("Europe/Moscow"))
        res = parse_human_datetime(m.text or "", now_local, lang)

        if res.need_day and res.hour_min:
            # для циклов тоже требуем день, чтобы определить dow/dom
            await state.update_data(pending_time=res.hour_min, tz=res.tz, repeat_kind=kind)
            await state.set_state(AlertCreate.waiting_day)
            hint = "Укажи день: «сегодня», «завтра», день недели или дату (ДД.ММ / ГГГГ-ММ-ДД)" if lang == "ru" \
                else "Specify the day: “today”, “tomorrow”, weekday or a date (DD.MM / YYYY-MM-DD)"
            await m.answer(hint)
            return

        if not res.dt:
            await m.answer(T(lang, "errors_invalid_dt"))
            return

        # строим cron по первой локальной дате
        dt_local = res.dt
        try:
            cron = build_cron_from_repeat(kind, dt_local)
        except Exception:
            await m.answer(T(lang, "errors_invalid_cron"))
            return

        async with db.session() as s:
            alert = await create_alert(
                s,
                owner_user_id=m.from_user.id,
                title=data.get("title"),
                content_type=data["content_type"],
                content_json=data["content_json"],
                kind="cron",
                run_at_utc=None,
                cron=cron,
                tz=res.tz,
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


    @r.message(AlertCreate.waiting_cron)
    async def step_cron_new(m: Message, state: FSMContext):
        lang = get_lang(m)
        cron = (m.text or "").strip()
        tz = "Europe/Moscow"
        try:
            cron_trigger(cron, tz)
        except Exception:
            await m.answer(T(lang, "errors_invalid_cron"))
            return

        data = await state.get_data()
        async with db.session() as s:
            alert = await create_alert(
                s,
                owner_user_id=m.from_user.id,
                title=data.get("title"),
                content_type=data["content_type"],
                content_json=data["content_json"],
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
        await state.set_state(AlertCreate.waiting_time_input)
        await m.answer(T(lang, "create_enter_dt"))


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
