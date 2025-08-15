from __future__ import annotations

from datetime import datetime
from typing import Any

from aiogram import types, Bot
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from core.keyboards import title_kb
from core.utils import set_expected
from tools import (
    add_alert_row,
    schedule_one_time,
    schedule_many_cron_times,
    schedule_weekly,
    schedule_window_daily,
    summarize_interval,
    norm_title,
)


async def send_alert_back_job(user_id: int, alert_id: str, bot: Bot) -> None:
    """Thin wrapper to avoid import cycles."""
    from tools import send_alert_back  # lazy import
    await send_alert_back(user_id, alert_id, bot)


async def finalize_and_save_alert(
    msg_or_cb: types.Message | types.CallbackQuery,
    state: FSMContext,
    *,
    title: str,
    scheduler: AsyncIOScheduler,
    bot: Bot,
    phrases: dict,
) -> None:
    """Persist alert (CSV/tools version) and schedule jobs."""
    is_cb = isinstance(msg_or_cb, types.CallbackQuery)
    chat = msg_or_cb.message if is_cb else msg_or_cb
    user_id = chat.from_user.id
    data = await state.get_data()
    media_type = (data.get("media_type") or "text").lower()
    file_id = data.get("file_id")
    payload_text = data.get("payload_text") or ""
    pending_kind = (data.get("pending_kind") or "one_time").lower()
    logger.debug(
        "finalize_and_save_alert user_id={} kind={} title='{}'",
        user_id, pending_kind, title,
    )

    if pending_kind == "one_time":
        run_at = datetime.fromisoformat(data.get("pending_run_at"))
        row = {
            "user_id": user_id,
            "created_at": datetime.now(),
            "send_at": run_at,
            "file_id": file_id or "",
            "payload_text": payload_text,
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
        schedule_one_time(alert_id, user_id, run_at, scheduler=scheduler, bot=bot)
        details = f"Когда: {run_at.strftime('%Y-%m-%d %H:%M')}"
        logger.debug("Saved ONE_TIME alert id={} for user_id={}", alert_id, user_id)

    elif pending_kind == "interval":
        interval_def: dict[str, Any] = data.get("pending_interval") or {}
        kind = (interval_def.get("kind") or "").lower()
        logger.debug("Saving INTERVAL kind='{}' def={}", kind, interval_def)
        if not kind:
            await chat.answer(phrases["interval_confirm_error"])
            return

        details = summarize_interval(interval_def)

        if kind == "daily":
            times = interval_def.get("times") or []
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text,
                "media_type": media_type, "title": norm_title(title),
                "kind": "daily", "times": ";".join(times),
                "days_of_week": "", "window_start": "", "window_end": "",
                "interval_minutes": "", "cron_expr": "",
            }
            alert_id = add_alert_row(row)
            schedule_many_cron_times(alert_id, user_id, times, scheduler=scheduler, bot=bot)

        elif kind == "weekly":
            days = interval_def.get("days_of_week") or []
            times = interval_def.get("times") or []
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text,
                "media_type": media_type, "title": norm_title(title),
                "kind": "weekly", "times": ";".join(times), "days_of_week": ";".join(days),
                "window_start": "", "window_end": "", "interval_minutes": "", "cron_expr": "",
            }
            alert_id = add_alert_row(row)
            schedule_weekly(alert_id, user_id, days, times, scheduler=scheduler, bot=bot)

        elif kind == "window_interval":
            w = interval_def.get("window") or {}
            ws, we = w.get("start"), w.get("end")
            step = int(interval_def.get("interval_minutes") or 0)
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text,
                "media_type": media_type, "title": norm_title(title),
                "kind": "window_interval", "times": "", "days_of_week": "",
                "window_start": ws, "window_end": we, "interval_minutes": step, "cron_expr": "",
            }
            alert_id = add_alert_row(row)
            schedule_window_daily(alert_id, user_id, ws, we, step, scheduler=scheduler, bot=bot)

        elif kind == "cron":
            expr = interval_def.get("cron_expr") or ""
            row = {
                "user_id": user_id, "created_at": datetime.now(), "send_at": "",
                "file_id": file_id or "", "payload_text": payload_text,
                "media_type": media_type, "title": norm_title(title),
                "kind": "cron", "times": "", "days_of_week": "",
                "window_start": "", "window_end": "", "interval_minutes": "",
                "cron_expr": expr,
            }
            alert_id = add_alert_row(row)
            job_id = f"{alert_id}__cronexpr"
            logger.debug("Scheduling CRON expr alert id={} job_id={} expr={}", alert_id, job_id, expr)
            scheduler.add_job(
                send_alert_back_job,
                CronTrigger.from_crontab(expr),
                args=(user_id, alert_id, bot),
                id=job_id,
                replace_existing=True,
            )
        else:
            logger.debug("Unsupported interval kind: {}", kind)
            await chat.answer(phrases["interval_confirm_error"])
            return
    else:
        logger.debug("Unknown pending_kind: {}", pending_kind)
        await chat.answer(phrases["interval_confirm_error"])
        return

    await state.clear()
    await chat.answer(
        phrases["scheduled"].format(
            title=(norm_title(title) or "без названия"),
            details=details,
        )
    )
