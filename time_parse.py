# time_parse.py
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Europe/Moscow"

RU_DOW = {
    "понедельник": 0, "вторник": 1, "среда": 2, "четверг": 3, "пятница": 4, "суббота": 5, "воскресенье": 6,
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}
EN_DOW = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

def _guess_tz(text: str) -> str:
    low = (text or "").lower()
    if "мск" in low or "москва" in low or "msk" in low or "moscow" in low:
        return "Europe/Moscow"
    m = re.search(r"\b(europe/[a-z_]+)\b", low)
    if m:
        tz = m.group(1).title().replace("_", "_")
        try:
            ZoneInfo(tz)
            return tz
        except Exception:
            pass
    # простые utc±N
    um = re.search(r"\butc\s*([+-])\s*(\d{1,2})\b", low)
    if um:
        sign = -1 if um.group(1) == "+" else 1  # Etc/GMT имеет инвертированный знак
        off = int(um.group(2)) * sign
        tz = f"Etc/GMT{off:+d}".replace("+", "")  # например Etc/GMT-3 (для UTC+3)
        try:
            ZoneInfo(tz)
            return tz
        except Exception:
            pass
    return DEFAULT_TZ

@dataclass
class ParseResult:
    dt: Optional[datetime]
    hour_min: Optional[Tuple[int, int]]
    need_day: bool
    tz: str

def _extract_time(text: str) -> Optional[Tuple[int, int, Optional[str]]]:
    t = text.lower()
    # hh:mm or h:mm
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ampm = "pm" if re.search(r"\b(pm|p\.m\.)\b", t) else ("am" if re.search(r"\b(am|a\.m\.)\b", t) else None)
        return h, mi, ampm
    # "в 15 30", "at 7 05"
    m = re.search(r"\b(?:в|во|at)\s*(\d{1,2})\s+(\d{2})\b", t)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ampm = "pm" if re.search(r"\b(pm|p\.m\.)\b", t) else ("am" if re.search(r"\b(am|a\.m\.)\b", t) else None)
        return h, mi, ampm
    # only hour: "в 6", "at 6"
    m = re.search(r"\b(?:в|во|at)\s*(\d{1,2})\b", t)
    if m:
        h = int(m.group(1))
        ampm = "pm" if re.search(r"\b(pm|p\.m\.)\b", t) else ("am" if re.search(r"\b(am|a\.m\.)\b", t) else None)
        return h, 0, ampm
    # plain "17:40" without "в"
    m = re.search(r"\b(\d{1,2})[:.](\d{2})\b", t)
    if m:
        return int(m.group(1)), int(m.group(2)), None
    # plain "1740"
    m = re.search(r"\b(\d{2})(\d{2})\b", t)
    if m:
        return int(m.group(1)), int(m.group(2)), None
    return None

def _extract_date(text: str, now: datetime, lang: str) -> Optional[datetime]:
    t = text.lower().strip()
    # today / tomorrow
    if ("сегодня" in t) or ("today" in t):
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if ("завтра" in t) or ("tomorrow" in t):
        d = now + timedelta(days=1)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    if ("послезавтра" in t):
        d = now + timedelta(days=2)
        return d.replace(hour=0, minute=0, second=0, microsecond=0)
    # dd.mm or dd/mm
    m = re.search(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b", t)
    if m:
        dd, mm = int(m.group(1)), int(m.group(2))
        yy = int(m.group(3)) if m.group(3) else now.year
        try:
            return now.replace(year=yy, month=mm, day=dd, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
    # yyyy-mm-dd
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        yy, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return now.replace(year=yy, month=mm, day=dd, hour=0, minute=0, second=0, microsecond=0)
        except ValueError:
            return None
    # weekday
    table = RU_DOW if lang == "ru" else EN_DOW
    for key, idx in table.items():
        if re.search(rf"\b{re.escape(key)}\b", t):
            delta = (idx - now.weekday()) % 7
            base = now + timedelta(days=delta or 7) if delta == 0 else now + timedelta(days=delta)
            return base.replace(hour=0, minute=0, second=0, microsecond=0)
    return None

def _part_of_day_bias(text: str) -> Optional[str]:
    t = text.lower()
    if any(x in t for x in ["утром", "morning"]): return "am"
    if any(x in t for x in ["днём", "днем", "afternoon"]): return "pm"
    if any(x in t for x in ["вечером", "evening"]): return "pm"
    if any(x in t for x in ["ночью", "night"]): return "night"
    return None

def parse_human_datetime(text: str, now_local: datetime, lang: str) -> ParseResult:
    """
    Пытается распарсить текст в локальное datetime.
    Если указан только час/минута — вернёт need_day=True и hour_min=(h,m).
    ТЗ: если «сегодня в 6», а на часах 15:00 — считать 18:00.
    TZ: из текста если возможно, иначе Europe/Moscow.
    """
    tz = _guess_tz(text)
    now = now_local.astimezone(ZoneInfo(tz))
    t = _extract_time(text)
    if not t:
        return ParseResult(dt=None, hour_min=None, need_day=False, tz=tz)
    h, mi, ampm = t
    pod = _part_of_day_bias(text)

    # нормализуем час по подсказкам / am/pm
    if ampm == "pm" and h < 12:
        h += 12
    elif ampm == "am" and h == 12:
        h = 0
    elif pod == "pm" and h < 12:
        h += 12
    elif pod == "night" and h < 6:
        h = h  # оставляем как есть (ночь)
    # day detection
    date_only = _extract_date(text, now, lang)
    if date_only is None:
        # нет дня — нужно уточнить
        return ParseResult(dt=None, hour_min=(h, mi), need_day=True, tz=tz)

    dt = date_only.replace(hour=h, minute=mi)

    # правило «сегодня в 6» после 15:00 → 18:00
    if ("сегодня" in text.lower() or "today" in text.lower()) and ampm is None and pod is None:
        if dt <= now and h < 12 and h + 12 <= 23:
            dt = dt.replace(hour=h + 12)

    return ParseResult(dt=dt, hour_min=None, need_day=False, tz=tz)

def combine_day_with_time(day_text: str, hour_min: Tuple[int, int], now_local: datetime, lang: str) -> ParseResult:
    tz = _guess_tz(day_text)
    now = now_local.astimezone(ZoneInfo(tz))
    base = _extract_date(day_text, now, lang)
    if not base:
        return ParseResult(dt=None, hour_min=None, need_day=False, tz=tz)
    h, mi = hour_min
    dt = base.replace(hour=h, minute=mi)
    # если для "сегодня" время уже прошло и не указан pm/am — переносим на вечер, если возможно
    if ("сегодня" in day_text.lower() or "today" in day_text.lower()) and dt <= now and h < 12 and h + 12 <= 23:
        dt = dt.replace(hour=h + 12)
    return ParseResult(dt=dt, hour_min=None, need_day=False, tz=tz)

def format_dt_local(dt: datetime, tz: str, lang: str) -> str:
    local = dt.astimezone(ZoneInfo(tz))
    if lang == "ru":
        return f"Запланировано на: <b>{local:%d.%m.%Y %H:%M}</b> ({tz})"
    return f"Scheduled for: <b>{local:%Y-%m-%d %H:%M}</b> ({tz})"
