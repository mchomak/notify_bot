# ai_interval.py
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional, List, Tuple

from loguru import logger
from openai import OpenAI

# ---- Конфиг клиента ----
from config import *
settings: Settings = load_env()
DEEPSEEK_KEY = settings.deepseek_key
DEEPSEEK_API_URL = settings.deepseek_api_url
DEEPSEEK_MODEL = settings.deepseek_model


ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url=DEEPSEEK_API_URL, timeout=30)

# ---- JSON-спека ответа ----
INTERVAL_JSON_SPEC = {
    "kind": "daily | weekly | window_interval | cron | one_time",
    "times": ["HH:MM", "..."],          # для daily/weekly (может быть несколько)
    "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],  # для weekly
    "tz": "IANA timezone (e.g. Europe/Moscow) | null",
    "cron": "m h dom mon dow  # если выбран 'cron'",
    "window": {               # для window_interval
        "start": "HH:MM",
        "end": "HH:MM"
    },
    "interval_minutes": 15    # шаг для window_interval
}

def build_interval_system_prompt(lang: str = "ru") -> str:
    spec = json.dumps(INTERVAL_JSON_SPEC, ensure_ascii=False, indent=2)
    ru = (
        "Ты парсер расписаний. Преобразуй русскоязычное/англ. описание периодических уведомлений в JSON строго по схеме.\n"
        "ТРЕБОВАНИЯ:\n"
        "1) Отвечай ТОЛЬКО валидным JSON без лишнего текста.\n"
        "2) Время — 24-часовой формат HH:MM локального времени пользователя.\n"
        "3) Структура:\n" + spec + "\n"
        "Правила выбора kind:\n"
        "- 'daily': каждый день в фиксированные времена.\n"
        "- 'weekly': по дням недели (пн-вс) + times[].\n"
        "- 'window_interval': окно (start..end) и шаг в минутах.\n"
        "- 'cron': если задан крон.\n"
        "- 'one_time': единичное время/дата.\n"
        "Если чего-то нет — делай безопасные допущения. Не придумывай даты «задним числом»."
    )
    en = (
        "You are a schedule parser. Convert natural language interval reminders into JSON per spec.\n"
        "REQUIREMENTS:\n"
        "1) Reply ONLY with valid JSON, no extra text.\n"
        "2) Use 24h format HH:MM in user's local time.\n"
        "3) Schema:\n" + spec + "\n"
        "kind rules: daily / weekly / window_interval / cron / one_time. Be conservative; avoid hallucinations."
    )
    return ru if lang == "ru" else en


# ---- Нормализация и сборка CRON ----

DOW_NAME_TO_CRON = {
    "mon": "mon", "monday": "mon", "понедельник": "mon", "пн": "mon",
    "tue": "tue", "tuesday": "tue", "вторник": "tue", "вт": "tue",
    "wed": "wed", "wednesday": "wed", "среда": "wed", "ср": "wed",
    "thu": "thu", "thursday": "thu", "четверг": "thu", "чт": "thu",
    "fri": "fri", "friday": "fri", "пятница": "fri", "пт": "fri",
    "sat": "sat", "saturday": "sat", "суббота": "sat", "сб": "sat",
    "sun": "sun", "sunday": "sun", "воскресенье": "sun", "вс": "sun",
    "weekend": "sat,sun", "выходные": "sat,sun",
    "будни": "mon,tue,wed,thu,fri", "weekdays": "mon,tue,wed,thu,fri",
}

def _hhmm_to_pair(s: str) -> Tuple[int, int]:
    m = re.match(r"^\s*(\d{1,2})[:.](\d{2})\s*$", s)
    if not m:
        raise ValueError(f"Bad HH:MM: {s!r}")
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"Bad time bounds: {s!r}")
    return hh, mm

def _dow_csv(days: List[str]) -> str:
    out: List[str] = []
    for d in days or []:
        key = str(d).strip().lower()
        mapped = DOW_NAME_TO_CRON.get(key)
        if not mapped:
            continue
        # mapped может быть "sat,sun"
        out.extend(mapped.split(","))
    # uniq order
    seen, uniq = set(), []
    for x in out:
        if x not in seen:
            uniq.append(x); seen.add(x)
    return ",".join(uniq)

@dataclass
class IntervalPlan:
    kind: str
    times: List[str]
    days_of_week: List[str]
    tz: str
    cron: Optional[str]
    window_start: Optional[str]
    window_end: Optional[str]
    interval_minutes: Optional[int]

def _normalize_plan(raw: dict, default_tz: str) -> IntervalPlan:
    kind = (raw.get("kind") or "").strip().lower()
    times = [t.strip() for t in (raw.get("times") or []) if isinstance(t, str)]
    days = raw.get("days_of_week") or []
    tz = (raw.get("tz") or default_tz).strip() or default_tz
    cron = raw.get("cron")
    win = raw.get("window") or {}
    return IntervalPlan(
        kind=kind,
        times=times,
        days_of_week=days,
        tz=tz,
        cron=cron.strip() if isinstance(cron, str) else None,
        window_start=win.get("start"),
        window_end=win.get("end"),
        interval_minutes=raw.get("interval_minutes"),
    )

def plan_to_crons(plan: IntervalPlan) -> List[str]:
    crons: List[str] = []
    if plan.kind == "cron" and plan.cron:
        return [plan.cron]

    if plan.kind == "daily":
        for t in plan.times:
            h, m = _hhmm_to_pair(t)
            crons.append(f"{m} {h} * * *")
        return crons

    if plan.kind == "weekly":
        dow = _dow_csv(plan.days_of_week)
        if not dow:
            # по умолчанию — будни
            dow = "mon,tue,wed,thu,fri"
        for t in plan.times:
            h, m = _hhmm_to_pair(t)
            crons.append(f"{m} {h} * * {dow}")
        return crons

    if plan.kind == "window_interval":
        # упрощённо: */n по минутам в диапазоне часов start..end
        if not plan.window_start or not plan.window_end or not plan.interval_minutes:
            raise ValueError("Incomplete window_interval")
        sh, sm = _hhmm_to_pair(plan.window_start)
        eh, em = _hhmm_to_pair(plan.window_end)
        # часы как диапазон (включая конец)
        hours = f"{sh}-{eh}"
        # минуты — общий шаг (упрощённо, без выравнивания от start)
        mins = f"*/{int(plan.interval_minutes)}"
        dow = _dow_csv(plan.days_of_week) or "*"
        crons.append(f"{mins} {hours} * * {dow}")
        return crons

    if plan.kind == "one_time":
        # отдаём как daily на указанное время — дальше в бизнес-логике можете превратить в one-shot
        for t in plan.times:
            h, m = _hhmm_to_pair(t)
            crons.append(f"{m} {h} * * *")
        return crons

    # по умолчанию попробуем daily
    for t in plan.times or ["09:00"]:
        h, m = _hhmm_to_pair(t)
        crons.append(f"{m} {h} * * *")
    return crons


# ---- Вызов LLM ----

def ai_parse_interval_phrase(text: str, *, lang: str, default_tz: str) -> Tuple[IntervalPlan, List[str]]:
    """
    Возвращает (нормализованный план, список cron-строк).
    Бросает исключение при неуспехе.
    """
    sys_prompt = build_interval_system_prompt(lang)
    user_prompt = text.strip()

    logger.debug("AI interval parse request", extra={"lang": lang, "text": user_prompt})

    resp = ai_client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content
    # попытка распарсить JSON
    try:
        data = json.loads(content)
    except Exception:
        # вырезаем самый длинный {...}
        m = re.search(r"\{.*\}", content, flags=re.S)
        if not m:
            raise
        data = json.loads(m.group(0))

    plan = _normalize_plan(data, default_tz=default_tz)
    crons = plan_to_crons(plan)
    logger.debug("AI interval plan", extra={"plan": plan.__dict__, "crons": crons})
    return plan, crons
