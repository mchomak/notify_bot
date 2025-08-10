# main.py
import sys
import asyncio
import re
from datetime import datetime
from typing import Optional, Tuple, Dict, Any

import pandas as pd
import dateparser
from openai import OpenAI

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from config import *
from text import phrases as _phrases
from tools import *

phrases = _phrases["ru"]

# ====================== LOGGING SETUP ======================
logger.remove()
logger.add(sys.stderr, level="DEBUG", enqueue=True, backtrace=True, diagnose=True)
logger.add("bot_debug.log", level="DEBUG", rotation="10 MB", retention="10 days", enqueue=True)
logger.debug("Logger initialized. Starting bot setup...")

# ====================== BOT & CLIENTS ======================
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url=API_URL, timeout=30)
logger.debug("Bot, Dispatcher, Scheduler, OpenAI client created.")

# ====================== KEYBOARDS ======================
main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=txt) for txt in row] for row in phrases["buttons"]],
    resize_keyboard=True
)

def choose_kind_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["one_time_btn"], callback_data="kind_one_time"),
            InlineKeyboardButton(text=phrases["interval_btn"], callback_data="kind_interval")
        ]]
    )

def confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["confirm_btn"], callback_data="time_confirm"),
            InlineKeyboardButton(text=phrases["redo_btn"], callback_data="time_redo")
        ]]
    )

def confirm_interval_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["confirm_btn"], callback_data="interval_confirm"),
            InlineKeyboardButton(text=phrases["redo_btn"], callback_data="interval_redo")
        ]]
    )

def title_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["title_btn_enter"], callback_data="title_enter"),
            InlineKeyboardButton(text=phrases["title_btn_skip"], callback_data="title_skip")
        ]]
    )

def alerts_list_kb(df_user: pd.DataFrame) -> InlineKeyboardMarkup:
    rows = []
    for _, row in df_user.iterrows():
        title = (row.get("title") or str(row["alert_id"])[:8]).strip() or str(row["alert_id"])[:8]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"alert:{row['alert_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def alert_actions_kb(alert_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=phrases["back_btn"], callback_data="alerts_back")],
            [InlineKeyboardButton(text=phrases["delete_btn"], callback_data=f"alert_del:{alert_id}")]
        ]
    )

# ====================== HELPERS: expected state & payload ======================
def set_expected(state: FSMContext, value: str):
    return state.update_data(expected=value)

def get_expected(data: Dict[str, Any]) -> str:
    return data.get("expected") or ""

def detect_payload(msg: types.Message) -> Optional[Tuple[str, Optional[str], Optional[str]]]:
    """
    Возвращает (media_type, file_id, text_payload) или None.
    text_payload используется для media_type == 'text'.
    """
    if msg.text and not msg.entities:  # простой текст
        return ("text", None, msg.text)

    ct = msg.content_type
    if ct == types.ContentType.PHOTO and msg.photo:
        file_id = msg.photo[-1].file_id  # самое большое фото
        return ("photo", file_id, None)
    if ct == types.ContentType.VIDEO and msg.video:
        return ("video", msg.video.file_id, None)
    if ct == types.ContentType.VIDEO_NOTE and msg.video_note:
        return ("video_note", msg.video_note.file_id, None)
    if ct == types.ContentType.ANIMATION and msg.animation:
        return ("animation", msg.animation.file_id, None)
    if ct == types.ContentType.AUDIO and msg.audio:
        return ("audio", msg.audio.file_id, None)
    if ct == types.ContentType.VOICE and msg.voice:
        return ("voice", msg.voice.file_id, None)
    if ct == types.ContentType.DOCUMENT and msg.document:
        return ("document", msg.document.file_id, None)
    if ct == types.ContentType.STICKER and msg.sticker:
        return ("sticker", msg.sticker.file_id, None)

    # Текст со ссылками/командами считаем тоже как текст
    if msg.text:
        return ("text", None, msg.text)

    return None

async def ensure_expected_or_error(msg: types.Message, state: FSMContext, want: str) -> bool:
    data = await state.get_data()
    curr = get_expected(data)
    if curr and curr != want:
        # выдаём соответствующую подсказку
        mapping = {
            "await_media": phrases["expect_media"],
            "await_kind": phrases["expect_kind"],
            "await_time": phrases["expect_time"],
            "await_interval": phrases["expect_interval_desc_need"],
            "await_title_choice": phrases["expect_title_choice"],
            "await_title_input": phrases["expect_title_input_need"],
        }
        tip = mapping.get(want) or phrases["generic_expect"]
        await msg.answer(tip)
        return False
    return True

# ====================== PARSE ONE-TIME ======================
async def handle_time_parse_and_confirm(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    raw = msg.text.strip().lower()
    logger.debug("handle_time_parse_and_confirm user_id={} raw='{}'", msg.from_user.id, raw)

    now = datetime.now()
    run_at: Optional[datetime] = None
    matched_explicit_hm = False

    text = re.sub(r"[,\u00A0]+", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()

    day_shift = 0
    for token, shift in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        if token in text:
            day_shift = max(day_shift, shift)
            text = text.replace(token, "").strip()
    logger.debug("Normalized text='{}' day_shift={}", text, day_shift)

    def in_range(h: int, m: int) -> bool:
        return 0 <= h <= 23 and 0 <= m <= 59

    def apply_period(h: int, period: Optional[str]) -> int:
        if not period:
            if now.hour >= 12 and 1 <= h <= 11:
                return (h % 12) + 12
            return h
        if period == "утра":
            return 0 if h == 12 else h % 24
        if period == "дня":
            return 12 if h == 12 else (h % 12) + 12
        if period == "вечера":
            return 12 if h == 12 else (h % 12) + 12
        if period == "ночи":
            return 0 if h == 12 else h % 24
        return h

    def build_dt(h: int, m: int, period: Optional[str]) -> Optional[datetime]:
        nonlocal matched_explicit_hm
        if not in_range(h, m):
            return None
        h24 = apply_period(h, period)
        dt = now.replace(hour=h24, minute=m, second=0, microsecond=0)
        if day_shift:
            dt = dt + pd.Timedelta(days=day_shift)
        if matched_explicit_hm and day_shift == 0 and dt <= now:
            dt = dt + pd.Timedelta(days=1)
        return dt

    # 1) HH sep MM
    m = re.match(r"^(?:в\s*)?(\d{1,2})\s*[:.\-\s]\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
    if m:
        h = int(m.group(1)); minute = int(m.group(2)); period = m.group(3)
        logger.debug("Pattern #1 matched: h={}, m={}, period={}", h, minute, period)
        if in_range(h, minute):
            matched_explicit_hm = True
            run_at = build_dt(h, minute, period)

    # 2) "в X [period] (в Y минут)"
    if not run_at:
        m2 = re.match(
            r"^в\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?(?:\s*в\s*(\d{1,2})(?:\s*мин(?:ут[ы])?|м)?)?$",
            text
        )
        if m2:
            h = int(m2.group(1)); period = m2.group(2); minute = int(m2.group(3) or 0)
            logger.debug("Pattern #2 matched: h={}, m={}, period={}", h, minute, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # 3) "в X час(ов) [Y минут]"
    if not run_at:
        m3 = re.match(
            r"^в\s*(\d{1,2})\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?(\d{1,2})?\s*(?:мин(?:ут[ы])?|м)?\s*(утра|дня|вечера|ночи)?$",
            text
        )
        if m3:
            h = int(m3.group(1)); minute = int(m3.group(2) or 0); period = m3.group(3)
            logger.debug("Pattern #3 matched: h={}, m={}, period={}", h, minute, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # 4) hours only
    if not run_at:
        m4 = re.match(r"^(?:в\s*)?(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
        if m4:
            h = int(m4.group(1)); period = m4.group(2); minute = 0
            logger.debug("Pattern #4 matched: h={}, m=0, period={}", h, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # 5) dateparser fallback
    if not run_at:
        logger.debug("No explicit pattern matched. Using dateparser fallback.")
        parsed = dateparser.parse(
            raw,
            languages=["ru"],
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now}
        )
        if parsed:
            logger.debug("dateparser recognized: {}", parsed)
            run_at = parsed

    log_time_parse(msg.from_user.id, raw, run_at if run_at and run_at > now else None)

    if not run_at or run_at <= now:
        logger.debug("Failed to parse future time for user_id={}", msg.from_user.id)
        return await msg.answer(phrases["time_error"])

    await state.update_data(pending_kind="one_time", pending_run_at=run_at.isoformat())
    await set_expected(state, "await_time_confirm")
    logger.debug("Time parsed OK for user_id={}, run_at={}", msg.from_user.id, run_at)
    # показываем КНОПКИ подтверждения (новым сообщением)
    await msg.answer(
        phrases["confirm_prompt"].format(when=run_at.strftime("%Y-%m-%d %H:%M")),
        reply_markup=confirm_kb()
    )

# ==== Final save ====
async def finalize_and_save_alert(msg_or_cb: types.Message | types.CallbackQuery, state: FSMContext, title: str):
    is_cb = isinstance(msg_or_cb, types.CallbackQuery)
    chat = msg_or_cb.message if is_cb else msg_or_cb
    user_id = chat.from_user.id
    data = await state.get_data()
    media_type = (data.get("media_type") or "text").lower()
    file_id = data.get("file_id")
    payload_text = data.get("payload_text") or ""
    pending_kind = (data.get("pending_kind") or "one_time").lower()
    logger.debug("finalize_and_save_alert user_id={} kind={} title='{}'", user_id, pending_kind, title)

    # страховка
    run_scheduler = scheduler

    if pending_kind == "one_time":
        run_at = datetime.fromisoformat(data.get("pending_run_at"))
        row = {
            "user_id": user_id,
            "created_at": datetime.now(),
            "send_at": run_at,
            "file_id": file_id or "",
            "payload_text": payload_text,
            "media_type": media_type,
            "title": norm_title(title),
            "kind": "one_time",
            "times": "",
            "days_of_week": "",
            "window_start": "",
            "window_end": "",
            "interval_minutes": "",
            "cron_expr": "",
        }
        alert_id = add_alert_row(row)
        schedule_one_time(alert_id, user_id, run_at, scheduler=run_scheduler, bot=bot)
        details = f"Когда: {run_at.strftime('%Y-%m-%d %H:%M')}"
        logger.debug("Saved ONE_TIME alert id={} for user_id={}", alert_id, user_id)

    elif pending_kind == "interval":
        interval_def = data.get("pending_interval") or {}
        kind = (interval_def.get("kind") or "").lower()
        logger.debug("Saving INTERVAL kind='{}' def={}", kind, interval_def)
        if not kind:
            await chat.answer(phrases["interval_confirm_error"])
            return

        details = summarize_interval(interval_def)

        if kind == "daily":
            times = interval_def.get("times") or []
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text, "media_type": media_type, "title": norm_title(title),
                "kind": "daily", "times": ";".join(times),
                "days_of_week": "", "window_start": "", "window_end": "",
                "interval_minutes": "", "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_many_cron_times(alert_id, user_id, times, scheduler=run_scheduler, bot=bot)

        elif kind == "weekly":
            days = interval_def.get("days_of_week") or []
            times = interval_def.get("times") or []
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text, "media_type": media_type, "title": norm_title(title),
                "kind": "weekly", "times": ";".join(times), "days_of_week": ";".join(days),
                "window_start": "", "window_end": "", "interval_minutes": "", "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_weekly(alert_id, user_id, days, times, scheduler=run_scheduler, bot=bot)

        elif kind == "window_interval":
            w = interval_def.get("window") or {}
            ws, we = w.get("start"), w.get("end")
            step = int(interval_def.get("interval_minutes") or 0)
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text, "media_type": media_type, "title": norm_title(title),
                "kind": "window_interval", "times": "", "days_of_week": "",
                "window_start": ws, "window_end": we, "interval_minutes": step, "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_window_daily(alert_id, user_id, ws, we, step, scheduler=run_scheduler, bot=bot)

        elif kind == "cron":
            expr = interval_def.get("cron_expr") or ""
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text, "media_type": media_type, "title": norm_title(title),
                "kind": "cron", "times": "", "days_of_week": "",
                "window_start": "", "window_end": "", "interval_minutes": "", "cron_expr": expr
            }
            alert_id = add_alert_row(row)
            job_id = f"{alert_id}__cronexpr"
            logger.debug("Scheduling CRON expr alert id={} job_id={} expr={}", alert_id, job_id, expr)
            scheduler.add_job(
                send_alert_back_job,
                CronTrigger.from_crontab(expr),
                args=(user_id, alert_id, bot),
                id=job_id,
                replace_existing=True
            )
        else:
            logger.debug("Unsupported interval kind: {}", kind)
            await chat.answer(phrases["interval_confirm_error"])
            return
    else:
        logger.debug("Unknown pending_kind: {}", pending_kind)
        await chat.answer(phrases["interval_confirm_error"])
        return

    await state.clear()
    await chat.answer(phrases["scheduled"].format(
        title=(norm_title(title) or "без названия"),
        details=details
    ))

# враппер для джобы (название нужно только тут)
async def send_alert_back_job(user_id: int, alert_id: str, bot: Bot):
    from tools import send_alert_back  # импорт тут, чтобы избежать циклов
    await send_alert_back(user_id, alert_id, bot)

# ====================== HANDLERS ======================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message, state: FSMContext):
    logger.debug("Command /start from user_id={}", msg.from_user.id)
    await state.clear()
    await set_expected(state, "await_media")
    await msg.answer(phrases["ask_content"], reply_markup=main_kb)

@dp.message(F.text == "Новая нотификация")
async def new_alert(msg: types.Message, state: FSMContext):
    logger.debug("New notification requested by user_id={}", msg.from_user.id)
    await state.clear()
    await set_expected(state, "await_media")
    await msg.answer(phrases["ask_content"])

@dp.message(F.text == "Мои уведомления")
async def list_alerts_handler(msg: types.Message):
    uid = msg.from_user.id
    logger.debug("Listing alerts for user_id={}", uid)
    df = load_alerts()
    user_df = df[df["user_id"] == uid]
    if user_df.empty:
        return await msg.answer(phrases["no_alerts"])
    await msg.answer(phrases["alerts_header"], reply_markup=alerts_list_kb(user_df))

# ==== callback: list navigation & alert view/delete ====
@dp.callback_query(F.data == "alerts_back")
async def cb_alerts_back(cb: types.CallbackQuery):
    uid = cb.from_user.id
    df = load_alerts()
    user_df = df[df["user_id"] == uid]
    if user_df.empty:
        return await cb.message.edit_text(phrases["no_alerts"])
    await cb.message.edit_text(phrases["alerts_header"], reply_markup=alerts_list_kb(user_df))
    await cb.answer()

@dp.callback_query(F.data.startswith("alert:"))
async def cb_alert_view(cb: types.CallbackQuery):
    alert_id = cb.data.split(":", 1)[1]
    df = load_alerts()
    row = df[df["alert_id"] == alert_id]
    if row.empty:
        await cb.answer("Не найдено", show_alert=True)
        return
    r = row.iloc[0]
    title = (r.get("title") or str(alert_id)[:8]).strip() or str(alert_id)[:8]
    kind = (r.get("kind") or "one_time").lower()
    details = []
    if kind in ("", "one_time"):
        when = r["send_at"]
        details.append(f"Тип: одноразовое\nКогда: {pd.to_datetime(when).strftime('%Y-%m-%d %H:%M')}")
    elif kind == "daily":
        details.append(f"Тип: ежедневно\nВремя: {(r.get('times') or '').replace(';', ', ')}")
    elif kind == "weekly":
        details.append(f"Тип: по дням недели\nДни: {(r.get('days_of_week') or '').replace(';', ', ')}\nВремя: {(r.get('times') or '').replace(';', ', ')}")
    elif kind == "window_interval":
        details.append(f"Тип: окно\nОкно: {r.get('window_start')}-{r.get('window_end')}\nШаг: {int(float(r.get('interval_minutes') or 0))} мин")
    elif kind == "cron":
        details.append(f"Тип: CRON\nExpr: {r.get('cron_expr')}")
    text = f"«{title}»\n" + "\n".join(details)
    await cb.message.edit_text(text, reply_markup=alert_actions_kb(alert_id))
    await cb.answer()

@dp.callback_query(F.data.startswith("alert_del:"))
async def cb_alert_delete(cb: types.CallbackQuery):
    alert_id = cb.data.split(":", 1)[1]
    unschedule_alert(alert_id, scheduler)
    remove_alert(alert_id)
    uid = cb.from_user.id
    df = load_alerts()
    user_df = df[df["user_id"] == uid]
    if user_df.empty:
        await cb.message.edit_text(phrases["no_alerts"])
    else:
        await cb.message.edit_text(phrases["alerts_header"], reply_markup=alerts_list_kb(user_df))
    await cb.answer("Удалено")

# ==== choose kind callbacks (edit the SAME message) ====
@dp.callback_query(F.data == "kind_one_time")
async def cb_kind_one_time(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("User {} chose ONE_TIME", cb.from_user.id)
    await state.update_data(pending_kind="one_time")
    await set_expected(state, "await_time")
    await cb.message.edit_text(phrases["ask_time"])
    await cb.answer()

@dp.callback_query(F.data == "kind_interval")
async def cb_kind_interval(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("User {} chose INTERVAL", cb.from_user.id)
    await state.update_data(pending_kind="interval", pending_interval=None)
    await set_expected(state, "await_interval")
    await cb.message.edit_text(phrases["ask_interval_desc"])
    await cb.answer()

# ==== time confirm (edit SAME message to next step) ====
@dp.callback_query(F.data == "time_confirm")
async def cb_time_confirm(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("time_confirm by user_id={}", cb.from_user.id)
    await set_expected(state, "await_title_choice")
    await cb.message.edit_text(phrases["ask_title"], reply_markup=title_kb())
    await cb.answer()

@dp.callback_query(F.data == "time_redo")
async def cb_time_redo(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("time_redo by user_id={}", cb.from_user.id)
    await state.update_data(pending_run_at=None)
    await set_expected(state, "await_time")
    await cb.message.edit_text(phrases["ask_time"])
    await cb.answer()

# ==== interval confirm (edit SAME message to next step) ====
@dp.callback_query(F.data == "interval_confirm")
async def cb_interval_confirm(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("interval_confirm by user_id={}", cb.from_user.id)
    await set_expected(state, "await_title_choice")
    await cb.message.edit_text(phrases["ask_title"], reply_markup=title_kb())
    await cb.answer()

@dp.callback_query(F.data == "interval_redo")
async def cb_interval_redo(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("interval_redo by user_id={}", cb.from_user.id)
    await state.update_data(pending_interval=None)
    await set_expected(state, "await_interval")
    await cb.message.edit_text(phrases["ask_interval_desc"])
    await cb.answer()

# ==== Title callbacks (edit SAME message where possible) ====
@dp.callback_query(F.data == "title_enter")
async def cb_title_enter(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("title_enter by user_id={}", cb.from_user.id)
    await set_expected(state, "await_title_input")
    await cb.message.edit_text(phrases["title_prompt"])
    await cb.answer()

@dp.callback_query(F.data == "title_skip")
async def cb_title_skip(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("title_skip by user_id={}", cb.from_user.id)
    await cb.message.edit_text(phrases["saving"])
    await finalize_and_save_alert(cb, state, title="")
    await cb.answer()

# ====================== GENERIC ROUTER ======================
@dp.message()
async def handle_message(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    expected = get_expected(data)
    logger.debug("handle_message user_id={} expected={} text='{}' ct={}", msg.from_user.id, expected, msg.text, msg.content_type)

    # 1) ждём медиа/текст для уведомления
    if expected in ("", "await_media"):
        payload = detect_payload(msg)
        if not payload:
            return await msg.answer(phrases["expect_media"])
        media_type, file_id, payload_text = payload
        await state.update_data(
            media_type=media_type,
            file_id=file_id,
            payload_text=payload_text or "",
            pending_kind=None,
            pending_run_at=None,
            pending_interval=None
        )
        await set_expected(state, "await_kind")
        # спрашиваем тип уведомления (новым сообщением; сам вопрос потом будет редактироваться по клику)
        await msg.answer(phrases["choose_kind"], reply_markup=choose_kind_kb())
        return

    # 2) ждём описание интервала (текст)
    if expected == "await_interval":
        if not msg.text:
            return await msg.answer(phrases["expect_interval_desc_need"])
        parsed = await parse_interval_with_ai(msg.text, ai_client)
        if not parsed:
            return await msg.answer(phrases["interval_confirm_error"])
        interval_def = normalize_interval_def(parsed)
        if not interval_def:
            return await msg.answer(phrases["interval_confirm_error"])
        await state.update_data(pending_interval=interval_def)
        preview = summarize_interval(interval_def)
        await set_expected(state, "await_interval_confirm")
        # показываем превью + кнопки
        return await msg.answer(phrases["interval_preview"].format(preview=preview), reply_markup=confirm_interval_kb())

    # 3) ждём время (текст)
    if expected == "await_time":
        if not msg.text:
            return await msg.answer(phrases["expect_time"])
        return await handle_time_parse_and_confirm(msg, state)

    # 4) ждём ввод названия (текст)
    if expected == "await_title_input":
        if not msg.text:
            return await msg.answer(phrases["expect_title_input_need"])
        title = norm_title(msg.text)
        if len(title) > 100:
            return await msg.answer(phrases["title_too_long"])
        await msg.answer(phrases["saving"])
        await finalize_and_save_alert(msg, state, title=title)
        return

    # 5) любые другие тексты не по ожиданиям
    mapping = {
        "await_kind": phrases["expect_kind"],
        "await_time_confirm": phrases["expect_time_confirm"],
        "await_title_choice": phrases["expect_title_choice"]
    }
    await msg.answer(mapping.get(expected, phrases["generic_expect"]))

# ====================== MAIN ======================
async def main():
    logger.debug("Starting scheduler...")
    ensure_csv()
    scheduler.start()
    logger.debug("Restoring scheduled jobs from CSV...")
    restore_jobs_from_csv(scheduler=scheduler, bot=bot)
    logger.debug("Start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
