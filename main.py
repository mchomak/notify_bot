# main.py
import sys
import asyncio
import re
from datetime import datetime
from typing import Optional

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

from config import *  # API_TOKEN, MODEL_NAME, DEEPSEEK_KEY, API_URL, CSV_FILE, TIME_PARSE_LOG_FILE, CSV_FIELDS, INTERVAL_JSON_SPEC
from text import phrases as _phrases
from tools import (  # <-- убедись, что файл называется tools.py
    ensure_csv, load_alerts, log_time_parse,
    normalize_interval_def, summarize_interval, norm_title,
    parse_interval_with_ai, restore_jobs_from_csv,
    schedule_one_time, schedule_many_cron_times, schedule_weekly, schedule_window_daily
)

# --- локаль фраз (в text.py: phrases = {"ru": {...}})
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

# ==== One-time time parsing core ====
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
    logger.debug("Time parsed OK for user_id={}, run_at={}", msg.from_user.id, run_at)
    await msg.answer(
        phrases["confirm_prompt"].format(when=run_at.strftime("%Y-%m-%d %H:%M")),
        reply_markup=confirm_kb()
    )

# ==== Final save ====
async def finalize_and_save_alert(msg_or_cb: types.Message | types.CallbackQuery, state: FSMContext, title: str, scheduler: AsyncIOScheduler = None):
    """
    Finalizes and saves the alert configuration.
    """
    is_cb = isinstance(msg_or_cb, types.CallbackQuery)
    chat = msg_or_cb.message if is_cb else msg_or_cb
    user_id = chat.from_user.id
    data = await state.get_data()
    file_id = data.get("file_id")
    media_type = (data.get("media_type") or "video").lower()
    pending_kind = (data.get("pending_kind") or "one_time").lower()
    logger.debug("finalize_and_save_alert user_id={} kind={} title='{}'", user_id, pending_kind, title)

    # страховка: берём глобальный планировщик, если не передали явно
    if scheduler is None:
        scheduler = globals().get("scheduler")

    if pending_kind == "one_time":
        run_at = datetime.fromisoformat(data.get("pending_run_at"))
        row = {
            "user_id": user_id,
            "created_at": datetime.now(),
            "send_at": run_at,
            "file_id": file_id,
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
        schedule_one_time(alert_id, user_id, run_at, scheduler=scheduler, bot=bot)
        details = human_details("one_time", run_at, None)
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
                "file_id": file_id, "media_type": media_type, "title": norm_title(title),
                "kind": "daily",
                "times": ";".join(times),
                "days_of_week": "", "window_start": "", "window_end": "",
                "interval_minutes": "", "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_many_cron_times(alert_id, user_id, times, scheduler=scheduler, bot=bot)

        elif kind == "weekly":
            days = interval_def.get("days_of_week") or []
            times = interval_def.get("times") or []
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id, "media_type": media_type, "title": norm_title(title),
                "kind": "weekly",
                "times": ";".join(times),
                "days_of_week": ";".join(days),
                "window_start": "", "window_end": "",
                "interval_minutes": "", "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_weekly(alert_id, user_id, days, times, scheduler=scheduler, bot=bot)

        elif kind == "window_interval":
            w = interval_def.get("window") or {}
            ws, we = w.get("start"), w.get("end")
            step = int(interval_def.get("interval_minutes") or 0)
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id, "media_type": media_type, "title": norm_title(title),
                "kind": "window_interval",
                "times": "",
                "days_of_week": "",
                "window_start": ws, "window_end": we,
                "interval_minutes": step, "cron_expr": ""
            }
            alert_id = add_alert_row(row)
            schedule_window_daily(alert_id, user_id, ws, we, step, scheduler=scheduler, bot=bot)

        elif kind == "cron":
            expr = interval_def.get("cron_expr") or ""
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id, "media_type": media_type, "title": norm_title(title),
                "kind": "cron",
                "times": "", "days_of_week": "",
                "window_start": "", "window_end": "",
                "interval_minutes": "", "cron_expr": expr
            }
            alert_id = add_alert_row(row)
            try:
                job_id = f"{alert_id}__cronexpr"
                logger.debug("Scheduling CRON expr alert id={} job_id={} expr={}", alert_id, job_id, expr)
                scheduler.add_job(
                    send_video_back,  # из tools
                    CronTrigger.from_crontab(expr),
                    args=(user_id, alert_id, bot),
                    id=job_id,
                    replace_existing=True
                )
            except Exception as e:
                logger.exception("Failed to schedule CRON expr for alert_id={}: {}", alert_id, e)
                await chat.answer(phrases["interval_confirm_error"])
                remove_alert(alert_id)
                return
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

# ====================== HANDLERS ======================
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    logger.debug("Command /start from user_id={}", msg.from_user.id)
    await msg.answer(phrases["start"], reply_markup=main_kb)

@dp.message(F.text == "Новая нотификация")
async def new_alert(msg: types.Message, state: FSMContext):
    logger.debug("New notification requested by user_id={}", msg.from_user.id)
    await state.clear()
    await msg.answer(phrases["ask_video"])

@dp.message(F.content_type.in_({types.ContentType.VIDEO, types.ContentType.VIDEO_NOTE}))
async def handle_video(msg: types.Message, state: FSMContext):
    file = msg.video or msg.video_note
    media_type = "video_note" if msg.video_note else "video"
    logger.debug("Received media from user_id={}, media_type={}, file_id={}", msg.from_user.id, media_type, file.file_id)
    await state.update_data(
        file_id=file.file_id,
        media_type=media_type,
        pending_kind=None,
        pending_run_at=None,
        pending_interval=None,
        awaiting_title=False
    )
    await msg.answer(phrases["saved_video"])
    await msg.answer(phrases["choose_kind"], reply_markup=choose_kind_kb())

@dp.callback_query(F.data == "kind_one_time")
async def cb_kind_one_time(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("User {} chose ONE_TIME", cb.from_user.id)
    await state.update_data(pending_kind="one_time")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_time"])
    await cb.answer()

@dp.callback_query(F.data == "kind_interval")
async def cb_kind_interval(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("User {} chose INTERVAL", cb.from_user.id)
    await state.update_data(pending_kind="interval", pending_interval=None)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_interval_desc"])
    await cb.answer()

@dp.message(F.text == "Мои уведомления")
async def list_alerts(msg: types.Message):
    uid = msg.from_user.id
    logger.debug("Listing alerts for user_id={}", uid)
    df = load_alerts()
    user_df = df[df["user_id"] == uid]
    if user_df.empty:
        logger.debug("No alerts for user_id={}", uid)
        return await msg.answer(phrases["no_alerts"])

    lines = []
    for _, row in user_df.iterrows():
        title = (row.get("title") or str(row["alert_id"])[:8]).strip()
        kind = (row.get("kind") or "one_time").lower()
        if kind in ("", "one_time"):
            when = row["send_at"]
            details = pd.to_datetime(when).strftime("%Y-%m-%d %H:%M") if not pd.isna(when) else ""
            lines.append(f"• {title} — {details}")
        elif kind == "daily":
            times = row.get("times") or ""
            lines.append(f"• {title} — ежедневно: {times.replace(';', ', ')}")
        elif kind == "weekly":
            days = (row.get("days_of_week") or "").replace(";", ", ")
            times = (row.get("times") or "").replace(";", ", ")
            lines.append(f"• {title} — {days}: {times}")
        elif kind == "window_interval":
            ws = row.get("window_start") or ""
            we = row.get("window_end") or ""
            step = int(float(row.get("interval_minutes") or 0) or 0)
            lines.append(f"• {title} — окно {ws}-{we} каждые {step} мин (ежедневно)")
        elif kind == "cron":
            lines.append(f"• {title} — CRON: {row.get('cron_expr')}")
    await msg.answer(phrases["your_alerts"].format(list="\n".join(lines)))

@dp.message()
async def handle_message(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    file_id = data.get("file_id")
    logger.debug("handle_message user_id={} text='{}' state_keys={}", msg.from_user.id, msg.text, list(data.keys()))
    if not file_id:
        logger.debug("No media in state for user_id={}", msg.from_user.id)
        return await msg.answer(phrases["no_video"])

    # 1) awaiting title
    if data.get("awaiting_title"):
        title = norm_title(msg.text)
        logger.debug("Received title from user_id={}, title='{}'", msg.from_user.id, title)
        if len(title) == 0:
            title = ""
        elif len(title) > 100:
            logger.debug("Title too long from user_id={}", msg.from_user.id)
            return await msg.answer(phrases["title_too_long"])
        await finalize_and_save_alert(msg, state, title=title, scheduler=scheduler)
        return

    # 2) interval description awaited
    if (data.get("pending_kind") == "interval") and (data.get("pending_interval") is None):
        logger.debug("Parsing interval description via AI for user_id={}", msg.from_user.id)
        parsed = await parse_interval_with_ai(msg.text, ai_client)
        if not parsed:
            logger.debug("AI failed to parse interval for user_id={}", msg.from_user.id)
            return await msg.answer(phrases["interval_confirm_error"])
        interval_def = normalize_interval_def(parsed)
        if not interval_def:
            logger.debug("normalize_interval_def failed for user_id={}", msg.from_user.id)
            return await msg.answer(phrases["interval_confirm_error"])

        await state.update_data(pending_interval=interval_def)
        preview = summarize_interval(interval_def)
        logger.debug("Interval preview for user_id={}: {}", msg.from_user.id, preview)
        return await msg.answer(phrases["interval_preview"].format(preview=preview), reply_markup=confirm_interval_kb())

    # 3) otherwise — one-time parse
    await handle_time_parse_and_confirm(msg, state)

@dp.callback_query(F.data == "time_confirm")
async def cb_time_confirm(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("time_confirm by user_id={}", cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(awaiting_title=False)
    await cb.message.answer(phrases["ask_title"], reply_markup=title_kb())
    await cb.answer()

@dp.callback_query(F.data == "time_redo")
async def cb_time_redo(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("time_redo by user_id={}", cb.from_user.id)
    await state.update_data(pending_run_at=None)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_time"])
    await cb.answer()

@dp.callback_query(F.data == "interval_confirm")
async def cb_interval_confirm(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("interval_confirm by user_id={}", cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_title"], reply_markup=title_kb())
    await cb.answer()

@dp.callback_query(F.data == "interval_redo")
async def cb_interval_redo(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("interval_redo by user_id={}", cb.from_user.id)
    await state.update_data(pending_interval=None)
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_interval_desc"])
    await cb.answer()

@dp.callback_query(F.data == "title_enter")
async def cb_title_enter(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("title_enter by user_id={}", cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await state.update_data(awaiting_title=True)
    await cb.message.answer(phrases["title_prompt"])
    await cb.answer()

@dp.callback_query(F.data == "title_skip")
async def cb_title_skip(cb: types.CallbackQuery, state: FSMContext):
    logger.debug("title_skip by user_id={}", cb.from_user.id)
    await cb.message.edit_reply_markup(reply_markup=None)
    await finalize_and_save_alert(cb, state, title="", scheduler=scheduler)
    await cb.answer()

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
