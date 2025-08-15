from __future__ import annotations

import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from loguru import logger

from config import load_env, get_runtime_env, Settings
from db import Database
from fsm import create_fsm_storage
from handlers import build_router, install_bot_commands
from setup_redis import build_fsm_diag_router
from setup_log import (
    setup_logging,
    start_telegram_alerts_dispatcher,
    report_exception,
    timed,
    timed_decorator,
)

import asyncio

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from loguru import logger
from openai import OpenAI

from setup_log import setup_logging
from handlers import register_handlers

from config import *
from text import phrases
from tools import restore_jobs_from_csv


async def main() -> None:
    # 1) Load settings from ENV/.env
    settings: Settings = load_env()

    # 2) Database (SQLite via async SQLAlchemy). Creates file/tables if missing.
    db = await Database.create(settings.database_url)

    # 3) FSM storage (Redis if available, otherwise in-memory)
    storage = await create_fsm_storage(settings.redis_url)

    ai_client = OpenAI(api_key=settings.DEEPSEEK_KEY, base_url=settings.API_URL, timeout=30)

    # 4) Telegram Bot and Dispatcher
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher(storage=storage)

    # 5) Optional Telegram alerts dispatcher for CRITICAL logs
    alerts_queue_put = None
    alerts_task = None
    if settings.telegram_alerts_chat_id:
        # Start alerts dispatcher first to reuse its queue in logging sinks
        queue, task = await start_telegram_alerts_dispatcher(
            bot, chat_id=settings.telegram_alerts_chat_id
        )
        alerts_queue_put = queue.put_nowait
        alerts_task = task

    # 6) Logging (console + rotating files + optional Telegram sink)
    setup_logging(
        app_name=settings.app_name,
        log_dir="logs",
        log_level=settings.log_level,
        rotation="10 MB",
        retention="7 days",
        compression="zip",
        telegram_queue_put=alerts_queue_put,
        telegram_min_level="CRITICAL",
        telegram_dedupe_seconds=60,
    )

    logger.info("Starting bot…", extra={"runtime": get_runtime_env(settings)})

    scheduler = AsyncIOScheduler()

    logger.debug("Bot, Dispatcher, Scheduler, OpenAI client created.")

    register_handlers(
        dp,
        phrases=phrases,
        scheduler=scheduler,
        bot=bot,
        ai_client=ai_client,
    )

    logger.debug("Starting scheduler...")
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
