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

# --- Настройка бота и планировщика -------------------------------------------
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
        dtype={"user_id": "int64", "file_id": "string", "media_type": "string", "alert_id": "string"},
        parse_dates=["created_at", "send_at"]
    )
    for col in ["user_id", "created_at", "send_at", "file_id", "media_type", "alert_id"]:
        if col not in df.columns:
            df[col] = pd.Series(dtype="object")
    return df


def save_alerts(df: pd.DataFrame) -> None:
    df.to_csv(CSV_FILE, index=False)


def add_alert(user_id: int, send_at: datetime, file_id: str, media_type: str) -> str:
    df = load_alerts()
    alert_id = str(uuid.uuid4())
    new_row = pd.DataFrame([{
        "user_id": user_id,
        "created_at": datetime.now(),
        "send_at": send_at,
        "file_id": file_id,
        "media_type": media_type,
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
    await state.update_data(file_id=file.file_id, media_type=media_type, pending_run_at=None)
    await msg.answer(phrases["saved_video"] + "\n" + phrases["ask_time"])


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


# --- Разбор времени + подтверждение ------------------------------------------
@dp.message()
async def handle_time(msg: types.Message, state: FSMContext):
    """
    Умный разбор времени:
    - поддержка: "22 10", "в 22 10", "22:10", "22-10", "22.10",
                 "в 9 вечера в 53 минуты", "в 9 часов 53 минуты",
                 "в 7", "в 7 вечера", "завтра в 7 05", "через 15 минут" и т.п.
    - если указаны только часы/минуты и получилось время "в прошлом" — перенос на завтра.
    - если текущий час уже после полудня и указан час < 12 без периода — предполагаем вечер (PM).
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
    text = re.sub(r"[,\u00A0]+", " ", raw)  # запятые/неразрывные пробелы -> обычные пробелы
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
            # если без периода и уже после полудня — предполагаем PM для 1..11
            if now.hour >= 12 and 1 <= h <= 11:
                return (h % 12) + 12
            return h
        if period == "утра":
            return 0 if h == 12 else h % 24  # 12 утра -> 00, остальное как есть (0..11)
        if period == "дня":
            # 1..11 -> 13..23, 12 дня -> 12
            return 12 if h == 12 else (h % 12) + 12
        if period == "вечера":
            # 1..11 -> 13..23, 12 вечера трактуем как 00 следующего дня — редкий случай, оставим 12->0 ниже общим правилом
            return 12 if h == 12 else (h % 12) + 12
        if period == "ночи":
            # обычно 0..5 — ночь; 12 ночи -> 00
            return 0 if h == 12 else h % 24
        return h


    def build_dt(h: int, m: int, period: Optional[str]) -> Optional[datetime]:
        nonlocal matched_explicit_hm
        if not in_range(h, m):
            return None
        h24 = apply_period(h, period)
        dt = now.replace(hour=h24, minute=m, second=0, microsecond=0)
        # первичный сдвиг по ключевым словам (сегодня/завтра/послезавтра)
        if day_shift:
            dt = dt + pd.Timedelta(days=day_shift)
        # если явный формат часов/минут и всё равно получилось "в прошлом" — переносим на следующие сутки
        if matched_explicit_hm and day_shift == 0 and dt <= now:
            dt = dt + pd.Timedelta(days=1)
        return dt


    # --- 1) явные "часы минуты" с любым разделителем и с/без "в" ---
    # примеры: "22 10", "в 22 10", "22:10", "22-10", "22.10"
    m = re.match(r"^(?:в\s*)?(\d{1,2})\s*[:.\-\s]\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
    if m:
        h = int(m.group(1))
        minute = int(m.group(2))
        period = m.group(3)
        if in_range(h, minute):
            matched_explicit_hm = True
            run_at = build_dt(h, minute, period)


    # --- 2) "в X [утра|дня|вечера|ночи] в Y минут(ы)" или "... Y" ---
    if not run_at:
        m2 = re.match(
            r"^в\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?(?:\s*в\s*(\d{1,2})\s*(?:мин(?:ут[ы])?|м)?)?$",
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

    # --- 5) свободные формулировки — отдаём dateparser полностью исходный текст ---
    if not run_at:
        parsed = dateparser.parse(
            raw,
            languages=["ru"],
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now}
        )
        if parsed:
            run_at = parsed

    # итоговая проверка
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
    file_id = data.get("file_id")
    media_type = data.get("media_type") or "video"
    pending_iso = data.get("pending_run_at")
    if not (file_id and pending_iso):
        await cb.message.edit_reply_markup(reply_markup=None)
        return await cb.answer("Нечего подтверждать", show_alert=True)

    run_at = datetime.fromisoformat(pending_iso)

    # создаём запись и планируем
    alert_id = add_alert(cb.from_user.id, run_at, file_id, media_type)
    schedule_job(alert_id, cb.from_user.id, run_at)

    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer(phrases["scheduled"].format(when=run_at.strftime("%Y-%m-%d %H:%M")))
    # очищаем только pending_run_at (медиа оставим? — можно очистить всё)
    await state.clear()
    await cb.answer()


@dp.callback_query(F.data == "time_redo")
async def cb_time_redo(cb: types.CallbackQuery, state: FSMContext):
    # удаляем только pending_run_at, чтобы пользователь заново ввёл время
    data = await state.get_data()
    await state.update_data(pending_run_at=None, file_id=data.get("file_id"), media_type=data.get("media_type"))
    await cb.message.edit_reply_markup(reply_markup=None)
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
