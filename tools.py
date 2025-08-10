# tools.py
import csv
import uuid
import json
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import pandas as pd
from aiogram import Bot
from apscheduler.triggers.cron import CronTrigger
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger

from config import *  # CSV_FILE, CSV_FIELDS, TIME_PARSE_LOG_FILE, MODEL_NAME, INTERVAL_JSON_SPEC

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
def schedule_one_time(alert_id: str, user_id: int, run_at: datetime, *, scheduler: AsyncIOScheduler, bot: Bot):
    logger.debug("Scheduling one-time alert_id={} at {}", alert_id, run_at.isoformat())
    scheduler.add_job(
        send_video_back, "date",
        run_date=run_at,
        args=(user_id, alert_id, bot),
        id=alert_id,
        replace_existing=True
    )

def schedule_cron(alert_id: str, user_id: int, hour: int, minute: int, *, scheduler: AsyncIOScheduler, bot: Bot, suffix: str = ""):
    job_id = f"{alert_id}__cron{suffix}"
    logger.debug("Scheduling CRON alert_id={} job_id={} for {:02d}:{:02d}", alert_id, job_id, hour, minute)
    scheduler.add_job(
        send_video_back, CronTrigger(hour=hour, minute=minute),
        args=(user_id, alert_id, bot),
        id=job_id,
        replace_existing=True
    )

def schedule_many_cron_times(alert_id: str, user_id: int, times_hhmm: List[str], *, scheduler: AsyncIOScheduler, bot: Bot):
    logger.debug("Scheduling many CRON times for alert_id={}, times={}", alert_id, times_hhmm)
    for i, t in enumerate(times_hhmm):
        h, m = map(int, t.split(":"))
        schedule_cron(alert_id, user_id, h, m, scheduler=scheduler, bot=bot, suffix=f"__{i}")

def schedule_weekly(alert_id: str, user_id: int, days: List[str], times_hhmm: List[str], *, scheduler: AsyncIOScheduler, bot: Bot):
    logger.debug("Scheduling WEEKLY alert_id={}, days={}, times={}", alert_id, days, times_hhmm)
    for i, t in enumerate(times_hhmm):
        h, m = map(int, t.split(":"))
        job_id = f"{alert_id}__weekly__{i}"
        logger.debug(" -> job_id={} day_of_week={}, time={}", job_id, ",".join(days), t)
        scheduler.add_job(
            send_video_back,
            CronTrigger(day_of_week=",".join(days), hour=h, minute=m),
            args=(user_id, alert_id, bot),
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

def schedule_window_daily(alert_id: str, user_id: int, window_start: str, window_end: str, step_minutes: int, *, scheduler: AsyncIOScheduler, bot: Bot):
    times = compute_times_in_window(window_start, window_end, step_minutes)
    schedule_many_cron_times(alert_id, user_id, times, scheduler=scheduler, bot=bot)

def restore_jobs_from_csv(*, scheduler: AsyncIOScheduler, bot: Bot):
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
                schedule_one_time(alert_id, uid, run_at, scheduler=scheduler, bot=bot)
            else:
                logger.debug(" -> expired one_time, removing")
                remove_alert(alert_id)

        elif kind == "daily":
            times = [t for t in (row.get("times") or "").split(";") if t]
            if times:
                schedule_many_cron_times(alert_id, uid, times, scheduler=scheduler, bot=bot)

        elif kind == "weekly":
            days = [d for d in (row.get("days_of_week") or "").split(";") if d]
            times = [t for t in (row.get("times") or "").split(";") if t]
            if days and times:
                schedule_weekly(alert_id, uid, days, times, scheduler=scheduler, bot=bot)

        elif kind == "window_interval":
            ws = row.get("window_start") or ""
            we = row.get("window_end") or ""
            step = int(float(row.get("interval_minutes") or 0) or 0)
            if ws and we and step > 0:
                schedule_window_daily(alert_id, uid, ws, we, step, scheduler=scheduler, bot=bot)

        elif kind == "cron":
            cron_expr = row.get("cron_expr") or ""
            if cron_expr:
                try:
                    job_id = f"{alert_id}__cronexpr"
                    logger.debug(" -> restoring CRON expr job_id={} expr={}", job_id, cron_expr)
                    scheduler.add_job(
                        send_video_back,
                        CronTrigger.from_crontab(cron_expr),
                        args=(uid, alert_id, bot),
                        id=job_id,
                        replace_existing=True
                    )
                except Exception as e:
                    logger.exception("Failed to restore cron for alert_id={}: {}", alert_id, e)
                    continue
    logger.debug("Restore finished.")

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

def _extract_balanced_json_block(s: str) -> Optional[str]:
    start = s.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    buf = []
    for i in range(start, len(s)):
        ch = s[i]
        buf.append(ch)
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(buf)
    return None

def _extract_json_payload(content: str) -> Optional[str]:
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        return fence.group(1).strip()
    fence_any = re.search(r"```(.*?)```", text, flags=re.DOTALL)
    if fence_any:
        payload = fence_any.group(1).strip()
        if "{" in payload and "}" in payload:
            return payload
    payload = _extract_balanced_json_block(text)
    return payload.strip() if payload else None

async def parse_interval_with_ai(text: str, ai_client: Any) -> Optional[Dict[str, Any]]:
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
    except Exception as e:
        logger.exception("AI request failed: {}", e)
        return None

    content = (resp.choices[0].message.content or "").strip()
    logger.debug("Raw AI response (truncated to 500 chars): {}", content[:500])

    payload = _extract_json_payload(content)
    if not payload:
        logger.debug("Failed to extract JSON payload from AI response.")
        return None
    logger.debug("Extracted JSON payload (truncated to 500 chars): {}", payload[:500])

    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            logger.debug("JSON parsed OK as dict.")
            return data
        logger.debug("JSON parsed but not a dict, type={}", type(data))
        return None
    except json.JSONDecodeError as e:
        logger.debug("json.loads failed on payload: {} at pos {}", e.msg, getattr(e, "pos", None))

    if payload.startswith('"') and payload.endswith('"'):
        try:
            unquoted = bytes(payload[1:-1], "utf-8").decode("unicode_escape")
            logger.debug("Trying to parse unquoted payload...")
            data = json.loads(unquoted)
            if isinstance(data, dict):
                logger.debug("JSON parsed OK after unquoting.")
                return data
        except Exception as e:
            logger.debug("Unquoted parse failed: {}", e)

    block = _extract_balanced_json_block(payload)
    if block and block != payload:
        try:
            logger.debug("Retry parse with balanced block extracted from payload.")
            data = json.loads(block)
            if isinstance(data, dict):
                logger.debug("JSON parsed OK from balanced block.")
                return data
        except Exception as e:
            logger.debug("Balanced block parse failed: {}", e)

    logger.debug("All JSON parsing attempts failed.")
    return None

# ====================== HELPERS ======================
def summarize_interval(d: Dict[str, Any]) -> str:
    k = (d.get("kind") or "").lower()
    if k == "daily":
        return f"Ежедневно: {', '.join(d.get('times', []))}"
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

# ====================== SENDER ======================
async def send_video_back(user_id: int, alert_id: str, bot: Bot):
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
    kind = (row.iloc[0].get("kind") or "one_time").lower()
    if kind in ("", "one_time"):
        logger.debug("One-time alert sent, removing alert_id={}", alert_id)
        remove_alert(alert_id)
