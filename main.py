import asyncio
import csv
import uuid
from datetime import datetime
from typing import Optional
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

import pandas as pd
import dateparser
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import API_TOKEN, CSV_FILE, CSV_FIELDS
from text import phrases
import json
from openai import OpenAI
from apscheduler.triggers.cron import CronTrigger


# --- Настройка бота и планировщика -------------------------------------------
client = OpenAI()
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# --- CSV-файл как база уведомлений --------------------------------------------
# Инициализация CSV, если не существует
try:
    open(CSV_FILE, newline="").close()
except FileNotFoundError:
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()

# --- Вспомогательные функции --------------------------------------------------
def load_alerts() -> pd.DataFrame:
    df = pd.read_csv(
        CSV_FILE,
        dtype={
            "user_id": "int64",
            "file_id": "string",
            "media_type": "string",
            "name": "string",
            "kind": "string",
            "recurrence_json": "string",
            "alert_id": "string",
        },
        parse_dates=["created_at", "send_at"]
    )
    for col in ["user_id","created_at","send_at","file_id","media_type","name","kind","recurrence_json","alert_id"]:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df



def save_alerts(df: pd.DataFrame) -> None:
    df.to_csv(CSV_FILE, index=False)


def add_alert(
    user_id: int,
    send_at: Optional[datetime],
    file_id: str,
    media_type: str,
    name: Optional[str],
    kind: str,  # "once" | "recurring"
    recurrence_json: Optional[str] = None
) -> str:
    df = load_alerts()
    alert_id = str(uuid.uuid4())
    new_row = pd.DataFrame([{
        "user_id": user_id,
        "created_at": datetime.now(),
        "send_at": send_at,
        "file_id": file_id,
        "media_type": media_type,
        "name": (name or None),
        "kind": kind,
        "recurrence_json": (recurrence_json or None),
        "alert_id": alert_id
    }])
    df = pd.concat([df, new_row], ignore_index=True)
    save_alerts(df)
    return alert_id



def remove_alert(alert_id: str) -> None:
    df = load_alerts()
    df = df[df["alert_id"] != alert_id]
    save_alerts(df)


def schedule_job(alert_id: str, user_id: int, send_at: datetime):
    scheduler.add_job(
        send_video_back,
        "date",
        run_date=send_at,
        args=(user_id, alert_id),
        id=alert_id,
        replace_existing=True
    )


def schedule_daily_time(alert_id: str, user_id: int, hh: int, mm: int):
    scheduler.add_job(
        send_video_back,
        CronTrigger(hour=hh, minute=mm),
        args=(user_id, alert_id),
        id=f"daily::{alert_id}",
        replace_existing=True
    )

def schedule_daily_window_setup(alert_id: str, user_id: int, hh: int, mm: int, interval_minutes: float, end_h: int, end_m: int):
    """
    Ежедневно в hh:mm ставим "сетап" на день: создаём серию единичных задач
    по шагу interval_minutes до end_h:end_m (только на текущий день).
    """
    def _setup_for_today():
        now = datetime.now()
        start = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
        if end <= start:
            end = end + pd.Timedelta(days=1)  # если окно пересекает полночь

        t = start
        i = 0
        while t <= end:
            job_id = f"{alert_id}::{t.strftime('%Y%m%d%H%M')}"
            if t >= now:
                scheduler.add_job(
                    send_video_back,
                    "date",
                    run_date=t,
                    args=(user_id, alert_id),
                    id=job_id,
                    replace_existing=True
                )
            t = t + pd.Timedelta(minutes=interval_minutes)
            i += 1

    # сам ежедневный "setup" (Cron)
    scheduler.add_job(
        _setup_for_today,
        CronTrigger(hour=hh, minute=mm),
        id=f"dailywin::{alert_id}",
        replace_existing=True
    )
    # сразу на сегодня тоже проставим (чтобы не ждать до завтра)
    _setup_for_today()


# --- Кнопки -------------------------------------------------------------------
main_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=txt) for txt in row] for row in phrases["buttons"]],
    resize_keyboard=True
)

# инлайн-кнопки подтверждения времени
def confirm_kb() -> InlineKeyboardMarkup:
    # добавьте в text.py:
    # phrases["confirm_btn"] = "Подтвердить"
    # phrases["redo_btn"] = "Задать заново"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=phrases["confirm_btn"], callback_data="time_confirm"),
                InlineKeyboardButton(text=phrases["redo_btn"], callback_data="time_redo")
            ]
        ]
    )


def mode_kb() -> InlineKeyboardMarkup:
    # phrases["once_btn"] = "Разовое"
    # phrases["recurring_btn"] = "Повторяющееся"
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["once_btn"], callback_data="mode_once"),
            InlineKeyboardButton(text=phrases["recurring_btn"], callback_data="mode_recurring"),
        ]]
    )

def skip_name_kb() -> InlineKeyboardMarkup:
    # phrases["skip_name_btn"] = "Пропустить"
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=phrases["skip_name_btn"], callback_data="skip_name")]]
    )


# --- Хендлеры ----------------------------------------------------------------
@dp.message(Command("start"))
async def cmd_start(msg: types.Message):
    await msg.answer(phrases["start"], reply_markup=main_kb)


@dp.message(F.text == "Новая нотификация")
async def new_alert(msg: types.Message, state: FSMContext):
    await state.clear()
    await msg.answer(phrases["ask_video"])


@dp.message(F.content_type.in_({types.ContentType.VIDEO, types.ContentType.VIDEO_NOTE}))
async def handle_video(msg: types.Message, state: FSMContext):
    file = msg.video or msg.video_note
    media_type = "video_note" if msg.video_note else "video"
    await state.clear()
    await state.update_data(file_id=file.file_id, media_type=media_type, pending_run_at=None, mode=None)
    # phrases["choose_mode"] = "Это разовая нотификация или повторяющаяся?"
    await msg.answer(phrases["choose_mode"], reply_markup=mode_kb())


@dp.callback_query(F.data == "mode_once")
async def cb_mode_once(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(mode="once")
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["ask_time"])
    await cb.answer()

@dp.callback_query(F.data == "mode_recurring")
async def cb_mode_recurring(cb: types.CallbackQuery, state: FSMContext):
    await state.update_data(mode="recurring")
    await cb.message.edit_reply_markup(reply_markup=None)
    # phrases["ask_recur_text"] = "Опиши условия в одном сообщении (пример: «каждый день в 21:00» или «ежедневно с 15:00 до 21:00 каждые 1.5 часа»)."
    await cb.message.answer(phrases["ask_recur_text"])
    await cb.answer()


@dp.message(F.text == "Мои уведомления")
async def list_alerts(msg: types.Message):
    user_id = msg.from_user.id
    df = load_alerts()
    user_df = df[df["user_id"] == user_id]
    if user_df.empty:
        await msg.answer(phrases["no_alerts"])
    else:
        lines = []
        for _, row in user_df.iterrows():
            when = row["send_at"].strftime("%Y-%m-%d %H:%M")
            lines.append(f"{row['alert_id'][:8]} → {when}")
        text = "\n".join(lines)
        await msg.answer(phrases["your_alerts"].format(list=text))



def ai_parse_recurrence(text: str) -> tuple[Optional[dict], Optional[str]]:
    """
    Просим ИИ распознать сценарий повторения и вернуть JSON.
    Схема JSON (минимум одна из поддерживаемых):
    - {"type":"daily_time","time":"HH:MM"}
    - {"type":"interval_window_daily","start_time":"HH:MM","end_time":"HH:MM","interval_minutes":90}
    Дополнительно допускаются ключи:
      "days_of_week": [1..7]  # 1=Пн ... 7=Вс (пока можно игнорировать)
    Никакого текста вокруг — только валидный JSON.
    """
    system = (
        "Ты парсер расписаний. Верни ТОЛЬКО валидный JSON по одной из схем:\n"
        "{\"type\":\"daily_time\",\"time\":\"HH:MM\"}\n"
        "{\"type\":\"interval_window_daily\",\"start_time\":\"HH:MM\",\"end_time\":\"HH:MM\",\"interval_minutes\":NUMBER}\n"
        "Если ничего не подошло — верни {}."
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {"role":"system","content":system},
                {"role":"user","content":text}
            ],
            temperature=0
        )
        content = resp.choices[0].message.content.strip()
        # выдернем JSON (если вдруг в код-блок завернули)
        m = re.search(r"\{.*\}", content, re.DOTALL)
        if not m:
            return None, "no-json"
        
        data = json.loads(m.group(0))
        if not isinstance(data, dict):
            return None, "bad-json"
        
        return data, None
    
    except Exception as e:
        return None, str(e)

def _hhmm(s: str) -> tuple[int,int]:
    h, m = s.strip().split(":")
    return int(h), int(m)

def schedule_recurring(alert_id: str, user_id: int, rec: dict):
    t = (rec.get("type") or "").lower()
    if t == "daily_time":
        hh, mm = _hhmm(rec["time"])
        schedule_daily_time(alert_id, user_id, hh, mm)
    elif t == "interval_window_daily":
        sh, sm = _hhmm(rec["start_time"])
        eh, em = _hhmm(rec["end_time"])
        interval = float(rec["interval_minutes"])
        schedule_daily_window_setup(alert_id, user_id, sh, sm, interval, eh, em)
    else:
        raise ValueError("Unsupported recurrence type")


@dp.message(F.text, ~F.via_bot)
async def handle_recurring_text(msg: types.Message, state: FSMContext):
    data = await state.get_data()
    # если это не режим recurring — отдаём управление следующему хендлеру (например, handle_time)
    if data.get("mode") != "recurring":
        return

    file_id = data.get("file_id")
    media_type = data.get("media_type")
    if not file_id:
        return await msg.answer(phrases["no_video"])

    rec, err = ai_parse_recurrence(msg.text.strip())
    if not rec:
        # phrases["recur_parse_error"] = "Не смог распознать расписание. Попробуй иначе сформулировать."
        return await msg.answer(phrases["recur_parse_error"])

    # покажем краткую сводку и попросим подтвердить
    summary = json.dumps(rec, ensure_ascii=False)
    await state.update_data(pending_recur=summary)  # сохраним JSON строкой
    # phrases["recur_confirm_prompt"] = "Понял так: {summary}\nПодтвердить или задать заново?"
    await msg.answer(
        phrases["recur_confirm_prompt"].format(summary=summary),
        reply_markup=confirm_kb()
    )


# --- Разбор времени + подтверждение ------------------------------------------
@dp.message()
async def handle_time(msg: types.Message, state: FSMContext):
    """
    Умный разбор времени + логирование результата в CSV:
    - поддержка: "22 10", "в 22 10", "22:10", "22-10", "22.10",
                 "в 9 вечера в 53 минуты", "в 9 часов 53 минуты",
                 "в 7", "в 7 вечера", "завтра в 7 05", "через 15 минут" и т.п.
    - если указаны только часы/минуты и получилось время "в прошлом" — перенос на завтра.
    - если текущий час уже после полудня и указан час < 12 без периода — предполагаем вечер (PM).
    - каждый ввод логируется в time_parse_log.csv (user_id, текст, распознанное время).
    """
    data = await state.get_data()
    file_id = data.get("file_id")
    media_type = data.get("media_type")
    if not file_id:
        return await msg.answer(phrases["no_video"])

    raw = msg.text.strip().lower()
    now = datetime.now()
    run_at: Optional[datetime] = None
    matched_explicit_hm = False

    # --- нормализация и извлечение сдвига дня ---
    text = re.sub(r"[,\u00A0]+", " ", raw)  # запятые/неразрывные пробелы -> пробел
    text = re.sub(r"\s+", " ", text).strip()

    day_shift = 0
    for token, shift in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0)):
        if token in text:
            day_shift = max(day_shift, shift)
            text = text.replace(token, "").strip()

    # --- помощники ---
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

    # --- 1) явные "часы минуты" с любым разделителем и с/без "в" ---
    m = re.match(r"^(?:в\s*)?(\d{1,2})\s*[:.\-\s]\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2))
        period = m.group(3)
        if in_range(h, minute):
            matched_explicit_hm = True
            run_at = build_dt(h, minute, period)

    # --- 2) "в X [утра|дня|вечера|ночи] (в Y минут[ы])" ---
    if not run_at:
        m2 = re.match(
            r"^в\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?(?:\s*в\s*(\d{1,2})(?:\s*мин(?:ут[ы])?|м)?)?$",
            text
        )
        if m2:
            h = int(m2.group(1))
            period = m2.group(2)
            minute = int(m2.group(3) or 0)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # --- 3) "в X час(а/ов) [Y минут(ы)]" / "в X ч [Y м]" ---
    if not run_at:
        m3 = re.match(
            r"^в\s*(\d{1,2})\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?(\d{1,2})?\s*(?:мин(?:ут[ы])?|м)?\s*(утра|дня|вечера|ночи)?$",
            text
        )
        if m3:
            h = int(m3.group(1))
            minute = int(m3.group(2) or 0)
            period = m3.group(3)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # --- 4) только часы: "в 7", "7", "в 7 вечера" ---
    if not run_at:
        m4 = re.match(r"^(?:в\s*)?(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
        if m4:
            h = int(m4.group(1))
            period = m4.group(2)
            minute = 0
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # --- 5) dateparser как fallback ---
    if not run_at:
        parsed = dateparser.parse(
            raw,
            languages=["ru"],
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now}
        )
        if parsed:
            run_at = parsed

    # логируем попытку распознавания (успех/неуспех)
    log_time_parse(msg.from_user.id, raw, run_at if run_at and run_at > now else None)

    if not run_at or run_at <= now:
        return await msg.answer(phrases["time_error"])

    # сохраняем предварительное время и предлагаем подтверждение
    await state.update_data(pending_run_at=run_at.isoformat())
    await msg.answer(
        phrases["confirm_prompt"].format(when=run_at.strftime("%Y-%m-%d %H:%M")),
        reply_markup=confirm_kb()
    )


def log_time_parse(user_id: int, input_text: str, recognized: Optional[datetime], file_path: str = "time_parse_log.csv") -> None:
    """
    Логирует попытку распознавания времени:
    - user_id
    - исходный текст пользователя
    - распознанное время в ISO (или пусто, если не распознано)
    """
    fields = ["user_id", "input_text", "recognized"]
    # создаём файл с заголовком, если его ещё нет
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


# --- Обработка inline-кнопок --------------------------------------------------
@dp.callback_query(F.data == "time_confirm")
async def cb_time_confirm(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode") or "once"
    await cb.message.edit_reply_markup(reply_markup=None)

    # дальше в обоих режимах попросим ИМЯ и дадим кнопку "Пропустить"
    await state.update_data(awaiting_name=True)
    # phrases["ask_name"] = "Как назвать уведомление? (до 100 символов). Или нажми «Пропустить»."
    await cb.message.answer(phrases["ask_name"], reply_markup=skip_name_kb())
    await cb.answer()

@dp.callback_query(F.data == "time_redo")
async def cb_time_redo(cb: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mode = data.get("mode") or "once"
    await cb.message.edit_reply_markup(reply_markup=None)
    if mode == "recurring":
        await cb.message.answer(phrases["ask_recur_text"])
    else:
        await cb.message.answer(phrases["ask_time"])
    await cb.answer()



# --- Отправка медиа и очистка записи -----------------------------------------
async def send_video_back(user_id: int, alert_id: str):
    df = load_alerts()
    row = df[df["alert_id"] == alert_id]
    if row.empty:
        return

    file_id = row.iloc[0]["file_id"]
    media_type = (row.iloc[0]["media_type"] or "video").lower()

    try:
        if media_type == "video_note":
            await bot.send_video_note(chat_id=user_id, video_note=file_id)
        else:
            await bot.send_video(chat_id=user_id, video=file_id)
    except Exception:
        # тут можно логировать
        pass
    finally:
        remove_alert(alert_id)


# --- Запуск -------------------------------------------------------------------
async def main():
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
