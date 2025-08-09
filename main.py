import os
import sys
import asyncio
import csv
import uuid
import json
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

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

from loguru import logger  # ЛОГИ

from config import *  # ожидаем API_TOKEN, MODEL_NAME, DEEPSEEK_KEY, API_URL, CSV_FILE, TIME_PARSE_LOG_FILE, CSV_FIELDS
from text import phrases


# ====================== LOGGING SETUP ======================
logger.remove()
# В консоль — DEBUG
logger.add(sys.stderr, level="DEBUG", enqueue=True, backtrace=True, diagnose=True)
# В файл — DEBUG (опционально)
logger.add("bot_debug.log", level="DEBUG", rotation="10 MB", retention="10 days", enqueue=True)

logger.debug("Logger initialized. Starting bot setup...")


# ====================== BOT & CLIENTS ======================
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url=API_URL, timeout=30)
logger.debug("Bot, Dispatcher, Scheduler, OpenAI client created.")


# ====================== CSV INIT ======================
def ensure_csv():
    logger.debug("Ensuring CSV exists at: {}", CSV_FILE)
    try:
        open(CSV_FILE, newline="", encoding="utf-8").close()
        logger.debug("CSV file exists.")
    except FileNotFoundError:
        logger.debug("CSV file not found. Creating new with headers: {}", CSV_FIELDS)
        with open(CSV_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()

ensure_csv()


# ====================== UTILS: CSV ======================
def load_alerts() -> pd.DataFrame:
    logger.debug("Loading alerts from CSV...")
    df = pd.read_csv(
        CSV_FILE,
        dtype={
            "user_id": "int64",
            "file_id": "string",
            "media_type": "string",
            "alert_id": "string",
            "title": "string",
            "kind": "string",
            "times": "string",
            "days_of_week": "string",
            "window_start": "string",
            "window_end": "string",
            "interval_minutes": "float64",
            "cron_expr": "string",
        },
        parse_dates=["created_at", "send_at"]
    )
    for col in CSV_FIELDS:
        if col not in df.columns:
            logger.debug("Column '{}' not found in CSV. Adding empty column.", col)
            df[col] = pd.Series(dtype="object")
    logger.debug("Loaded {} alerts.", len(df))
    return df


def save_alerts(df: pd.DataFrame) -> None:
    logger.debug("Saving {} alerts to CSV...", len(df))
    df.to_csv(CSV_FILE, index=False)
    logger.debug("CSV saved.")


def add_alert_row(row: Dict[str, Any]) -> str:
    logger.debug("Adding alert row: {}", row)
    df = load_alerts()
    alert_id = str(uuid.uuid4())
    row["alert_id"] = alert_id
    row_df = pd.DataFrame([row])
    df = pd.concat([df, row_df], ignore_index=True)
    save_alerts(df)
    logger.debug("Alert added with id={}", alert_id)
    return alert_id


def remove_alert(alert_id: str) -> None:
    logger.debug("Removing alert id={}", alert_id)
    df = load_alerts()
    before = len(df)
    df = df[df["alert_id"] != alert_id]
    save_alerts(df)
    after = len(df)
    logger.debug("Removed. Before={}, After={}", before, after)


# ====================== SCHEDULING HELPERS ======================
def schedule_one_time(alert_id: str, user_id: int, run_at: datetime):
    logger.debug("Scheduling one-time alert_id={} at {}", alert_id, run_at.isoformat())
    scheduler.add_job(
        send_video_back, "date",
        run_date=run_at,
        args=(user_id, alert_id),
        id=alert_id,
        replace_existing=True
    )


def schedule_cron(alert_id: str, user_id: int, hour: int, minute: int, suffix: str = ""):
    job_id = f"{alert_id}__cron{suffix}"
    logger.debug("Scheduling CRON alert_id={} job_id={} for {:02d}:{:02d}", alert_id, job_id, hour, minute)
    scheduler.add_job(
        send_video_back, CronTrigger(hour=hour, minute=minute),
        args=(user_id, alert_id),
        id=job_id,
        replace_existing=True
    )


def schedule_many_cron_times(alert_id: str, user_id: int, times_hhmm: List[str]):
    logger.debug("Scheduling many CRON times for alert_id={}, times={}", alert_id, times_hhmm)
    for i, t in enumerate(times_hhmm):
        h, m = map(int, t.split(":"))
        schedule_cron(alert_id, user_id, h, m, suffix=f"__{i}")


def schedule_weekly(alert_id: str, user_id: int, days: List[str], times_hhmm: List[str]):
    logger.debug("Scheduling WEEKLY alert_id={}, days={}, times={}", alert_id, days, times_hhmm)
    for i, t in enumerate(times_hhmm):
        h, m = map(int, t.split(":"))
        job_id = f"{alert_id}__weekly__{i}"
        logger.debug(" -> job_id={} day_of_week={}, time={}", job_id, ",".join(days), t)
        scheduler.add_job(
            send_video_back,
            CronTrigger(day_of_week=",".join(days), hour=h, minute=m),
            args=(user_id, alert_id),
            id=job_id,
            replace_existing=True
        )


def compute_times_in_window(window_start: str, window_end: str, step_minutes: int) -> List[str]:
    logger.debug("Computing times in window {}-{} step={}m", window_start, window_end, step_minutes)
    t0 = datetime.strptime(window_start, "%H:%M")
    t1 = datetime.strptime(window_end, "%H:%M")
    out = []
    cur = t0
    while cur <= t1:
        out.append(cur.strftime("%H:%M"))
        cur += timedelta(minutes=step_minutes)
    logger.debug("Window times: {}", out)
    return out


def schedule_window_daily(alert_id: str, user_id: int, window_start: str, window_end: str, step_minutes: int):
    times = compute_times_in_window(window_start, window_end, step_minutes)
    schedule_many_cron_times(alert_id, user_id, times)


def restore_jobs_from_csv():
    logger.debug("Restoring jobs from CSV ...")
    now = datetime.now()
    df = load_alerts()
    for _, row in df.iterrows():
        uid = int(row["user_id"])
        alert_id = str(row["alert_id"])
        kind = (row.get("kind") or "").lower()
        logger.debug("Restoring alert_id={} kind={}", alert_id, kind)

        if kind in ("", "one_time", "one-time"):
            send_at = row["send_at"]
            if pd.isna(send_at):
                logger.debug(" -> one_time has no send_at, skip")
                continue
            run_at = pd.to_datetime(send_at).to_pydatetime()
            if run_at > now:
                schedule_one_time(alert_id, uid, run_at)
            else:
                logger.debug(" -> expired one_time, removing")
                remove_alert(alert_id)
        elif kind == "daily":
            times = (row.get("times") or "").split(";")
            times = [t for t in times if t]
            if times:
                schedule_many_cron_times(alert_id, uid, times)
        elif kind == "weekly":
            days_str = (row.get("days_of_week") or "")
            days = [d for d in days_str.split(";") if d]
            times = [t for t in (row.get("times") or "").split(";") if t]
            if days and times:
                schedule_weekly(alert_id, uid, days, times)
        elif kind == "window_interval":
            ws = row.get("window_start") or ""
            we = row.get("window_end") or ""
            step = int(float(row.get("interval_minutes") or 0) or 0)
            if ws and we and step > 0:
                schedule_window_daily(alert_id, uid, ws, we, step)
        elif kind == "cron":
            cron_expr = row.get("cron_expr") or ""
            if cron_expr:
                try:
                    job_id = f"{alert_id}__cronexpr"
                    logger.debug(" -> restoring CRON expr job_id={} expr={}", job_id, cron_expr)
                    scheduler.add_job(
                        send_video_back,
                        CronTrigger.from_crontab(cron_expr),
                        args=(uid, alert_id),
                        id=job_id,
                        replace_existing=True
                    )
                except Exception as e:
                    logger.exception("Failed to restore cron for alert_id={}: {}", alert_id, e)
                    continue
    logger.debug("Restore finished.")


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


# ====================== LOGGING TIME PARSE ======================
def log_time_parse(user_id: int, input_text: str, recognized: Optional[datetime], file_path: str = TIME_PARSE_LOG_FILE) -> None:
    logger.debug("Logging time parse: user_id={}, input='{}', recognized={}", user_id, input_text, recognized)
    fields = ["user_id", "input_text", "recognized"]
    try:
        open(file_path, newline="", encoding="utf-8").close()
    except FileNotFoundError:
        with open(file_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()

    with open(file_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({
            "user_id": user_id,
            "input_text": input_text,
            "recognized": recognized.isoformat() if isinstance(recognized, datetime) else ""
        })


# ====================== AI INTERVAL PARSER ======================
INTERVAL_JSON_SPEC = {
    "kind": "one_time|daily|weekly|window_interval|cron",
    "times": "list of 'HH:MM' strings in 24h (for daily/weekly)",
    "days_of_week": "list of ['mon','tue','wed','thu','fri','sat','sun'] (weekly only)",
    "window": {"start": "HH:MM", "end": "HH:MM"},
    "interval_minutes": "integer minutes for window_interval",
    "cron_expr": "string crontab expression (optional)",
    "timezone": "IANA tz, optional (ignore if absent)",
    "name": "optional short title (<=100 chars)"
}

def build_interval_system_prompt() -> str:
    spec = json.dumps(INTERVAL_JSON_SPEC, ensure_ascii=False, indent=2)
    return (
        "Ты парсер расписаний. Твоя задача — преобразовать русскоязычное описание периодических уведомлений в JSON-СХЕМУ.\n"
        "Требования:\n"
        "1) Отвечай ТОЛЬКО валидным JSON БЕЗ текста.\n"
        "2) Время — 24-часовой формат HH:MM local.\n"
        "3) Структура ключей:\n" + spec + "\n"
        "Правила выбора kind:\n"
        "- 'daily': если каждый день в фиксированные времена.\n"
        "- 'weekly': если по дням недели (пн, ср, ...), укажи days_of_week=['mon','wed',...].\n"
        "- 'window_interval': если есть окно (start..end) и шаг в минутах, укажи window.start, window.end, interval_minutes.\n"
        "- 'cron': если явно задан крон-выражение.\n"
        "- 'one_time': если это единичное время/дата (но такие мы обычно обрабатываем отдельно).\n"
        "Если чего-то не хватает, делай безопасные допущения. Не придумывай лишнего."
    )

async def parse_interval_with_ai(text: str) -> Optional[Dict[str, Any]]:
    logger.debug("AI interval parse request: {}", text)
    try:
        resp = ai_client.chat.completions.create(
            model=MODEL_NAME,
            temperature=0,
            messages=[
                {"role": "system", "content": build_interval_system_prompt()},
                {"role": "user", "content": text}
            ]
        )
        content = resp.choices[0].message.content.strip()
        logger.debug("AI interval parse raw response: {}", content)
        data = json.loads(content)
        if not isinstance(data, dict):
            logger.debug("AI parse returned non-dict JSON.")
            return None
        logger.debug("AI interval parse JSON OK: {}", data)
        return data
    except Exception as e:
        logger.exception("AI parse error: {}", e)
        return None


# ====================== HELPERS ======================
def summarize_interval(d: Dict[str, Any]) -> str:
    k = (d.get("kind") or "").lower()
    if k == "daily":
        times = ", ".join(d.get("times", []))
        return f"Ежедневно: {times}"
    if k == "weekly":
        days = ", ".join(d.get("days_of_week", []))
        times = ", ".join(d.get("times", []))
        return f"По дням недели ({days}): {times}"
    if k == "window_interval":
        w = d.get("window") or {}
        step = d.get("interval_minutes")
        return f"Ежедневно, окно {w.get('start')}–{w.get('end')} каждые {step} мин"
    if k == "cron":
        return f"CRON: {d.get('cron_expr')}"
    if k == "one_time":
        return "Одноразовое (вероятно, задано временем/датой)"
    return "Неизвестный сценарий"

def norm_title(s: Optional[str]) -> str:
    s = (s or "").strip()
    return s[:100]

def human_details(kind: str, run_at: Optional[datetime], interval_def: Optional[Dict[str, Any]]) -> str:
    if kind == "one_time" and run_at:
        return f"Когда: {run_at.strftime('%Y-%m-%d %H:%M')}"
    if kind != "one_time" and interval_def:
        return summarize_interval(interval_def)
    return ""

def norm_hhmm(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    s = s.strip().replace(".", ":").replace("-", ":").replace(" ", ":")
    m = re.match(r"^(\d{1,2}):(\d{1,2})$", s)
    if not m:
        return None
    h, mnt = int(m.group(1)), int(m.group(2))
    if 0 <= h <= 23 and 0 <= mnt <= 59:
        return f"{h:02d}:{mnt:02d}"
    return None

def norm_day(s: str) -> Optional[str]:
    s = (s or "").strip().lower()
    mapping = {
        "пн": "mon", "понедельник": "mon", "пон": "mon",
        "вт": "tue", "вторник": "tue",
        "ср": "wed", "среда": "wed",
        "чт": "thu", "четверг": "thu",
        "пт": "fri", "пятница": "fri",
        "сб": "sat", "суббота": "sat",
        "вс": "sun", "воскресенье": "sun",
        "mon": "mon", "tue": "tue", "wed": "wed", "thu": "thu", "fri": "fri", "sat": "sat", "sun": "sun"
    }
    return mapping.get(s)

def normalize_interval_def(d: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    logger.debug("Normalizing interval def: {}", d)
    try:
        k = (d.get("kind") or "").lower()
        if k not in {"one_time", "daily", "weekly", "window_interval", "cron"}:
            if k == "one-time":
                k = "one_time"
            else:
                logger.debug("Unsupported kind: {}", k)
                return None
        out: Dict[str, Any] = {"kind": k}

        if k == "daily":
            times = d.get("times") or []
            out["times"] = [norm_hhmm(t) for t in times if norm_hhmm(t)]
        elif k == "weekly":
            times = d.get("times") or []
            out["times"] = [norm_hhmm(t) for t in times if norm_hhmm(t)]
            days = d.get("days_of_week") or []
            days = [norm_day(s) for s in days if norm_day(s)]
            if not days:
                logger.debug("Weekly without valid days.")
                return None
            out["days_of_week"] = days
        elif k == "window_interval":
            w = d.get("window") or {}
            ws, we = norm_hhmm(w.get("start")), norm_hhmm(w.get("end"))
            step = int(d.get("interval_minutes") or 0)
            if not (ws and we and step > 0):
                logger.debug("Window interval missing parts: ws={}, we={}, step={}", ws, we, step)
                return None
            out["window"] = {"start": ws, "end": we}
            out["interval_minutes"] = step
        elif k == "cron":
            expr = d.get("cron_expr")
            if not expr:
                logger.debug("Cron kind without expr.")
                return None
            out["cron_expr"] = str(expr)
        elif k == "one_time":
            pass

        logger.debug("Normalized interval def OK: {}", out)
        return out
    except Exception as e:
        logger.exception("normalize_interval_def failed: {}", e)
        return None


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

# ==== choose kind callbacks ====
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

# ==== list alerts ====
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


# ==== Generic message router ====
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
        await finalize_and_save_alert(msg, state, title=title)
        return

    # 2) interval description awaited
    if (data.get("pending_kind") == "interval") and (data.get("pending_interval") is None):
        logger.debug("Parsing interval description via AI for user_id={}", msg.from_user.id)
        parsed = await parse_interval_with_ai(msg.text)
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

    # log attempt
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


# ==== Confirm one-time ====
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


# ==== Confirm interval ====
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


# ==== Title callbacks ====
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
    await finalize_and_save_alert(cb, state, title="")
    await cb.answer()


# ==== Final save ====
async def finalize_and_save_alert(msg_or_cb: types.Message | types.CallbackQuery, state: FSMContext, title: str):
    is_cb = isinstance(msg_or_cb, types.CallbackQuery)
    chat = msg_or_cb.message if is_cb else msg_or_cb
    user_id = chat.from_user.id
    data = await state.get_data()
    file_id = data.get("file_id")
    media_type = (data.get("media_type") or "video").lower()
    pending_kind = (data.get("pending_kind") or "one_time").lower()
    logger.debug("finalize_and_save_alert user_id={} kind={} title='{}'", user_id, pending_kind, title)

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
        schedule_one_time(alert_id, user_id, run_at)
        details = human_details("one_time", run_at, None)
        logger.debug("Saved ONE_TIME alert id={} for user_id={}", alert_id, user_id)

    elif pending_kind == "interval":
        interval_def = data.get("pending_interval") or {}
        kind = (interval_def.get("kind") or "").lower()
        logger.debug("Saving INTERVAL kind='{}' def={}", kind, interval_def)
        if not kind:
            await chat.answer(phrases["interval_confirm_error"])
            return

        alert_id = None
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
            schedule_many_cron_times(alert_id, user_id, times)

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
            schedule_weekly(alert_id, user_id, days, times)

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
            schedule_window_daily(alert_id, user_id, ws, we, step)

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
                    send_video_back,
                    CronTrigger.from_crontab(expr),
                    args=(user_id, alert_id),
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


# ====================== SENDER ======================
async def send_video_back(user_id: int, alert_id: str):
    logger.debug("send_video_back called for user_id={} alert_id={}", user_id, alert_id)
    df = load_alerts()
    row = df[df["alert_id"] == alert_id]
    if row.empty:
        logger.debug("Alert not found in CSV for id={}, maybe already removed.", alert_id)
        return

    file_id = (row.iloc[0]["file_id"] or "").strip()
    media_type = (row.iloc[0]["media_type"] or "video").lower()
    logger.debug("Sending media_type={} file_id={} to user_id={}", media_type, file_id, user_id)
    try:
        if media_type == "video_note":
            await bot.send_video_note(chat_id=user_id, video_note=file_id)
        else:
            await bot.send_video(chat_id=user_id, video=file_id)
    except Exception as e:
        logger.exception("send_video_back failed for alert_id={}: {}", alert_id, e)
    # ВАЖНО: этот alert не удаляется автоматически для интервалов; удалять нужно только одноразовые при желании.
    # Если хочешь удалять одноразовые — можно проверить kind и удалить.
    kind = (row.iloc[0].get("kind") or "one_time").lower()
    if kind in ("", "one_time"):
        logger.debug("One-time alert sent, removing alert_id={}", alert_id)
        remove_alert(alert_id)


# ====================== MAIN ======================
async def main():
    logger.debug("Starting scheduler...")
    scheduler.start()
    logger.debug("Restoring scheduled jobs from CSV...")
    restore_jobs_from_csv()
    logger.debug("Start polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bot stopped by user.")
