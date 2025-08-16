# alerts.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from loguru import logger
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from db import Database, Alert, list_alerts, record_alert_run, get_alert

JOB_ID_PREFIX = "alert:"


def job_id_for(alert_id: int) -> str:
    return f"{JOB_ID_PREFIX}{alert_id}"


def weekday_name(dt: datetime) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]


def build_cron_from_repeat(kind: str, dt_local: datetime) -> str:
    """
    Build a crontab 'm h dom mon dow' string from repeat kind and local datetime.
    Supports: daily, weekly, monthly.
    """
    m = dt_local.minute
    h = dt_local.hour
    if kind == "daily":
        return f"{m} {h} * * *"
    if kind == "weekly":
        dow = weekday_name(dt_local)
        return f"{m} {h} * * {dow}"
    if kind == "monthly":
        dom = dt_local.day
        return f"{m} {h} {dom} * *"
    raise ValueError(f"Unsupported repeat kind: {kind}")


def cron_trigger(cron: str, tz: str) -> CronTrigger:
    return CronTrigger.from_crontab(cron, timezone=ZoneInfo(tz))


@dataclass
class AlertScheduler:
    bot: Bot
    db: Database
    scheduler: AsyncIOScheduler

    @classmethod
    def create(cls, bot: Bot, db: Database) -> "AlertScheduler":
        return cls(bot=bot, db=db, scheduler=AsyncIOScheduler())

    def start(self) -> None:
        self.scheduler.start()
        logger.info("APScheduler started")

    def shutdown(self) -> None:
        try:
            self.scheduler.shutdown(wait=False)
        except Exception:
            pass

    async def rebuild_from_db(self) -> None:
        """
        Load enabled alerts from DB and (re-)schedule jobs.
        Cleans up existing jobs with the same IDs.
        """
        async with self.db.session() as s:
            # All users, all enabled alerts
            alerts = await list_alerts(s, owner_user_id=0, active_only=True)  # placeholder won't work
        # ^ we can't query "all users" with existing helper; do direct query:
        async with self.db.session() as s:
            from sqlalchemy import select
            from db import Alert  # local import to avoid cycles
            rows = list((await s.execute(select(Alert).where(Alert.enabled.is_(True)))).scalars().all())

        # Clear existing alert jobs
        for j in list(self.scheduler.get_jobs()):
            if j.id.startswith(JOB_ID_PREFIX):
                try:
                    self.scheduler.remove_job(j.id)
                except Exception:
                    pass

        # Add jobs
        now_utc = datetime.now(timezone.utc)
        for a in rows:
            try:
                self._add_job_for_alert(a, now_utc=now_utc)
            except Exception as e:
                logger.error(f"Failed to schedule alert id={a.id}: {e!r}")

        logger.info("Rebuilt alert schedule", extra={"count": len(rows)})

    def _add_job_for_alert(self, alert: Alert, *, now_utc: Optional[datetime] = None) -> None:
        if not alert.enabled:
            return

        job_id = job_id_for(alert.id)

        # remove existing with same id
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass

        if alert.kind == "one":
            if not alert.run_at_utc:
                logger.warning(f"Alert {alert.id} has no run_at_utc")
                return
            if now_utc is None:
                now_utc = datetime.now(timezone.utc)
            if alert.run_at_utc <= now_utc:
                logger.info(f"Skip past one-shot alert {alert.id} at {alert.run_at_utc.isoformat()}")
                return
            trigger = DateTrigger(run_date=alert.run_at_utc)
        else:
            if not alert.cron:
                logger.warning(f"Alert {alert.id} has no cron expression")
                return
            try:
                trigger = cron_trigger(alert.cron, alert.tz)
            except Exception as e:
                logger.error(f"Bad cron for alert {alert.id}: {alert.cron!r} -> {e!r}")
                return

        self.scheduler.add_job(
            self._run_alert_job,
            trigger=trigger,
            id=job_id,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
            kwargs={"alert_id": alert.id},
        )
        logger.debug(f"Scheduled alert #{alert.id}", extra={"kind": alert.kind, "cron": alert.cron, "run_at_utc": str(alert.run_at_utc)})

    async def _run_alert_job(self, alert_id: int) -> None:
        """
        The actual job that sends content to the owner chat.
        """
        try:
            async with self.db.session() as s:
                alert = await get_alert(s, alert_id)
                if not alert or not alert.enabled:
                    return

                await self._send_alert_content(alert)

                now = datetime.now(timezone.utc)
                await record_alert_run(s, alert_id, now)

            # For one-shot, job can be removed (will be absent on rebuild anyway)
            # Safe remove: ignore if already gone.
            try:
                if self.scheduler.get_job(job_id_for(alert_id)):
                    self.scheduler.remove_job(job_id_for(alert_id))
            except Exception:
                pass

        except Exception as e:
            logger.exception(f"Alert job #{alert_id} failed: {e!r}")

    async def _send_alert_content(self, alert: Alert) -> None:
        chat_id = alert.owner_user_id
        kind = alert.content_type
        data = alert.content_json or {}

        # Common extras
        disable_web_page_preview = True
        protect = False

        if kind == "text":
            text = data.get("text") or ""
            parse_mode = data.get("parse_mode") or "HTML"
            await self.bot.send_message(
                chat_id, text, parse_mode=parse_mode, disable_web_page_preview=disable_web_page_preview, protect_content=protect
            )

        elif kind == "photo":
            await self.bot.send_photo(
                chat_id, data["file_id"], caption=data.get("caption"), parse_mode=data.get("parse_mode") or "HTML", protect_content=protect
            )

        elif kind == "video":
            await self.bot.send_video(
                chat_id, data["file_id"], caption=data.get("caption"), parse_mode=data.get("parse_mode") or "HTML", protect_content=protect
            )

        elif kind == "voice":
            await self.bot.send_voice(
                chat_id, data["file_id"], caption=data.get("caption"), parse_mode=data.get("parse_mode") or "HTML", protect_content=protect
            )

        elif kind == "audio":
            await self.bot.send_audio(
                chat_id, data["file_id"], caption=data.get("caption"), parse_mode=data.get("parse_mode") or "HTML", protect_content=protect
            )

        elif kind == "document":
            await self.bot.send_document(
                chat_id, data["file_id"], caption=data.get("caption"), parse_mode=data.get("parse_mode") or "HTML", protect_content=protect
            )

        elif kind == "video_note":
            await self.bot.send_video_note(chat_id, data["file_id"])

        else:
            await self.bot.send_message(chat_id, f"Unsupported content type: {kind}")
