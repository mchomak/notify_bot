# main.py
from __future__ import annotations

import asyncio
import contextlib

from aiogram import Bot, Dispatcher
from loguru import logger

from config import load_env, get_runtime_env, Settings
from db import Database
from fsm import create_fsm_storage
from handlers import build_router, install_bot_commands
from setup_log import (
    setup_logging,
    start_telegram_alerts_dispatcher,
    report_exception,
)
from alerts import AlertScheduler


async def main() -> None:
    # 1) Load settings from ENV/.env
    settings: Settings = load_env()

    # 2) Database (SQLite via async SQLAlchemy). Creates file/tables if missing.
    db = await Database.create(settings.database_url)

    # 3) FSM storage (Redis if available, otherwise in-memory)
    storage = await create_fsm_storage(settings.redis_url)

    # 4) Telegram Bot and Dispatcher
    bot = Bot(token=settings.telegram_bot_token)
    dp = Dispatcher(storage=storage)

    # 5) Optional Telegram alerts dispatcher for CRITICAL logs
    alerts_queue_put = None
    alerts_task = None
    if settings.telegram_alerts_chat_id:
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

    # 7) Install bot commands (menu)
    await install_bot_commands(bot, lang="ru")  # or "en"

    # 8) APScheduler for alerts
    alert_scheduler = AlertScheduler.create(bot, db)
    alert_scheduler.start()
    await alert_scheduler.rebuild_from_db()

    # 9) Routers: core handlers
    dp.include_router(build_router(db, alert_scheduler, settings.default_timezone))

    # 10) Polling loop
    try:
        await dp.start_polling(bot)

    except Exception as exc:
        report_exception(exc, ctx={"phase": "polling"})
        raise

    finally:
        # Stop alerts dispatcher
        if alerts_task:
            alerts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await alerts_task

        # Stop scheduler
        alert_scheduler.shutdown()

        # Close DB engine
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
