
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

from config import *
from text import phrases


ai_client = OpenAI(api_key=DEEPSEEK_KEY, base_url=API_URL, timeout=30)

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
    """
    Возвращает первый сбалансированный JSON-объект {...} из строки s.
    Учитывает строковые литералы и экранирование.
    """
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

    return None  # не нашли закрывающую скобку


def _extract_json_payload(content: str) -> Optional[str]:
    """
    1) Вырезает содержимое из блока ```json ... ```
    2) Если блока нет — пытается вытащить первый сбалансированный {...}
    3) Возвращает строку с JSON или None
    """
    if not content:
        return None

    text = content.strip()

    # Случай с код-блоком ```json ... ```
    fence = re.search(r"```(?:json|JSON)?\s*(.*?)\s*```", text, flags=re.DOTALL)
    if fence:
        payload = fence.group(1).strip()
        return payload

    # Иногда ИИ присылает несколько блоков — попробуем любой ```...```
    fence_any = re.search(r"```(.*?)```", text, flags=re.DOTALL)
    if fence_any:
        payload = fence_any.group(1).strip()
        # Часто внутри первого блока действительно JSON
        if "{" in payload and "}" in payload:
            return payload

    # Без код-блоков: достанем первый сбалансированный {...}
    payload = _extract_balanced_json_block(text)
    if payload:
        return payload.strip()

    return None


async def parse_interval_with_ai(text: str) -> Optional[Dict[str, Any]]:
    """
    Делает запрос к ИИ, чистит ответ от оградительных блоков и лишнего текста,
    затем превращает в dict. Возвращает None при неудаче.
    """
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

    # 1) Пытаемся аккуратно извлечь JSON
    payload = _extract_json_payload(content)
    if not payload:
        logger.debug("Failed to extract JSON payload from AI response.")
        return None

    logger.debug("Extracted JSON payload (truncated to 500 chars): {}", payload[:500])

    # 2) Пробуем загрузить как JSON как есть
    try:
        data = json.loads(payload)
        if isinstance(data, dict):
            logger.debug("JSON parsed OK as dict.")
            return data
        
        logger.debug("JSON parsed but not a dict, type={}", type(data))
        return None
    
    except json.JSONDecodeError as e:
        logger.debug("json.loads failed on payload: {} at pos {}", e.msg, getattr(e, "pos", None))

    # 3) Иногда весь JSON приходит в кавычках как одна строка.
    #    Попробуем распаковать строку (unescape) и снова загрузить.
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

    # 4) Последняя попытка: убрать возможные префиксы/суффиксы до/после JSON,
    #    оставив только первый сбалансированный блок ещё раз (на случай вложенных кавычек).
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


parsed = asyncio.run(parse_interval_with_ai("Каждый день в 10:00 и 15:00, кроме выходных"))
print(parsed)