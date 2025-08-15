from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

import pandas as pd
import dateparser
from aiogram import types
from aiogram.fsm.context import FSMContext
from loguru import logger

from keyboards import confirm_kb
from utils import set_expected
from tools import log_time_parse


async def handle_time_parse_and_confirm(
    msg: types.Message,
    state: FSMContext,
    phrases: dict,
) -> None:
    data = await state.get_data()
    raw = (msg.text or "").strip().lower()
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
        
        if period in ("дня", "вечера"):
            return 12 if h == 12 else (h % 12) + 12
        
        if period in ("утра", "ночи"):
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

    # (1) HH[:.- ]MM [period]
    m = re.match(r"^(?:в\s*)?(\d{1,2})\s*[:.\-\s]\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
    if m:
        h = int(m.group(1)); minute = int(m.group(2)); period = m.group(3)
        logger.debug("Pattern #1 matched: h={}, m={}, period={}", h, minute, period)
        if in_range(h, minute):
            matched_explicit_hm = True
            run_at = build_dt(h, minute, period)

    # (2) "в X [period] (в Y минут)"
    if not run_at:
        m2 = re.match(
            r"^в\s*(\d{1,2})\s*(утра|дня|вечера|ночи)?(?:\s*в\s*(\d{1,2})(?:\s*мин(?:ут[ы])?|м)?)?$",
            text,
        )
        if m2:
            h = int(m2.group(1)); period = m2.group(2); minute = int(m2.group(3) or 0)
            logger.debug("Pattern #2 matched: h={}, m={}, period={}", h, minute, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # (3) "в X час(ов) [Y минут]"
    if not run_at:
        m3 = re.match(
            r"^в\s*(\d{1,2})\s*(?:час(?:а|ов)?|ч)\s*(?:и\s*)?(\d{1,2})?\s*(?:мин(?:ут[ы])?|м)?\s*(утра|дня|вечера|ночи)?$",
            text,
        )
        if m3:
            h = int(m3.group(1)); minute = int(m3.group(2) or 0); period = m3.group(3)
            logger.debug("Pattern #3 matched: h={}, m={}, period={}", h, minute, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # (4) hours only
    if not run_at:
        m4 = re.match(r"^(?:в\s*)?(\d{1,2})\s*(утра|дня|вечера|ночи)?$", text)
        if m4:
            h = int(m4.group(1)); period = m4.group(2); minute = 0
            logger.debug("Pattern #4 matched: h={}, m=0, period={}", h, period)
            if in_range(h, minute):
                matched_explicit_hm = True
                run_at = build_dt(h, minute, period)

    # (5) fallback: dateparser
    if not run_at:
        logger.debug("No explicit pattern matched. Using dateparser fallback.")
        parsed = dateparser.parse(
            raw,
            languages=["ru"],
            settings={"PREFER_DATES_FROM": "future", "RELATIVE_BASE": now},
        )
        if parsed:
            logger.debug("dateparser recognized: {}", parsed)
            run_at = parsed

    log_time_parse(msg.from_user.id, raw, run_at if run_at and run_at > now else None)

    if not run_at or run_at <= now:
        logger.debug("Failed to parse future time for user_id={}", msg.from_user.id)
        await msg.answer(phrases["time_error"])
        return

    await state.update_data(pending_kind="one_time", pending_run_at=run_at.isoformat())
    await set_expected(state, "await_time_confirm")
    logger.debug("Time parsed OK for user_id={}, run_at={}", msg.from_user.id, run_at)
    await msg.answer(
        phrases["confirm_prompt"].format(when=run_at.strftime("%Y-%m-%d %H:%M")),
        reply_markup=confirm_kb(phrases),
    )
