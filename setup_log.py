# setup_log.py
from __future__ import annotations

import asyncio
import os
import sys
import time
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Union

from aiogram import Bot
from loguru import logger


def _level_no(name: str) -> int:
    order = {
        "TRACE": 5,
        "DEBUG": 10,
        "INFO": 20,
        "SUCCESS": 25,
        "WARNING": 30,
        "ERROR": 40,
        "CRITICAL": 50,
    }
    return order.get(name.upper(), 0)


def _escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _format_record_html(record: dict, include_exc: bool = True) -> str:
    """Format log record to compact HTML for Telegram alerts."""
    ts = datetime.fromtimestamp(
        record["time"].timestamp(), tz=timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S UTC")
    lvl = record["level"].name
    mod = record["module"]
    func = record["function"]
    line = record["line"]
    msg = _escape_html(str(record["message"]).strip())

    lines = [
        f"<b>{_escape_html(lvl)}</b> · <code>{ts}</code>",
        f"<b>At:</b> <code>{mod}.{func}:{line}</code>",
        f"<b>Msg:</b> {msg}",
    ]

    extra = record.get("extra") or {}
    if extra:
        try:
            sanitized = {
                k: (str(v)[:300] + "…") if isinstance(v, str) and len(str(v)) > 300 else v
                for k, v in extra.items()
            }
            lines.append(
                f"<b>Extra:</b> <code>{_escape_html(str(sanitized))}</code>"
            )
        except Exception:
            pass

    if include_exc and record.get("exception"):
        try:
            exc_text = "".join(
                traceback.format_exception(
                    record["exception"].type,
                    record["exception"].value,
                    record["exception"].traceback,
                )
            )
            exc_text = _escape_html(exc_text)
            lines.append("<b>Traceback:</b>\n<pre>" + exc_text[:3000] + "</pre>")
        except Exception:
            pass

    return "\n".join(lines)


class _TelegramSink:
    """Non-blocking sink: pushes formatted alert text into a queue processed by an async dispatcher."""

    def __init__(
        self,
        put: Callable[[str], None],
        min_level: str = "CRITICAL",
        dedupe_seconds: int = 60,
        max_message_len: int = 3800,
        include_exc: bool = True,
    ):
        self.put = put
        self.min_level = min_level
        self.dedupe_seconds = dedupe_seconds
        self.max_message_len = max_message_len
        self.include_exc = include_exc
        self._last_by_key: dict[str, float] = {}

    def __call__(self, message):
        record = message.record
        lvl_name: str = record["level"].name
        if _level_no(lvl_name) < _level_no(self.min_level):
            return

        text = _format_record_html(record, include_exc=self.include_exc)
        key = f"{lvl_name}:{record['message']}"
        now = time.monotonic()
        last = self._last_by_key.get(key, 0.0)
        if now - last < self.dedupe_seconds:
            return
        self._last_by_key[key] = now

        if len(text) > self.max_message_len:
            text = text[: self.max_message_len] + "\n\n<i>(truncated)</i>"

        try:
            self.put(text)
        except Exception:
            # Do not break logging pipeline
            pass


def setup_logging(
    *,
    app_name: str = "mybot",
    log_dir: str = "logs",
    log_level: str = "DEBUG",
    rotation: str = "10 MB",
    retention: str = "7 days",
    compression: str = "zip",
    telegram_queue_put: Optional[Callable[[str], None]] = None,
    telegram_min_level: str = "CRITICAL",
    telegram_dedupe_seconds: int = 60,
) -> None:
    """
    Configure loguru:
      - console sink
      - rotating file and error file
      - optional Telegram sink via queue (non-blocking)
    Call once at process start.
    """
    os.makedirs(log_dir, exist_ok=True)
    logger.remove()

    console_fmt = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> "
        "| <level>{level: <8}</level> "
        "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
        "- <level>{message}</level>"
    )
    logger.add(
        sys.stdout,
        level=log_level,
        format=console_fmt,
        enqueue=True,
        backtrace=False,
        diagnose=False,
    )

    file_fmt = (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} "
        "| {process.name}:{thread.name} "
        "| {name}:{function}:{line} - {message}"
    )

    debug_path = os.path.join(log_dir, f"{app_name}.debug.log")
    logger.add(
        debug_path,
        level=log_level,
        format=file_fmt,
        rotation=rotation,
        retention=retention,
        compression=compression,
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )

    err_path = os.path.join(log_dir, f"{app_name}.error.log")
    logger.add(
        err_path,
        level="ERROR",
        format=file_fmt,
        rotation=rotation,
        retention=retention,
        compression=compression,
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )

    if telegram_queue_put is not None:
        sink = _TelegramSink(
            put=telegram_queue_put,
            min_level=telegram_min_level,
            dedupe_seconds=telegram_dedupe_seconds,
        )
        logger.add(sink, level=telegram_min_level, enqueue=True, catch=True)

    logger.debug(
        "Logging initialized",
        extra={
            "app_name": app_name,
            "dir": log_dir,
            "rotation": rotation,
            "retention": retention,
            "compression": compression,
            "telegram_sink": bool(telegram_queue_put),
        },
    )


def report_exception(
    e: BaseException, ctx: Optional[dict[str, Any]] = None, extra_text: Optional[str] = None
) -> None:
    """Unified exception reporting into logs (Telegram sink picks CRITICAL automatically)."""
    ctx = ctx or {}
    msg = "Unhandled exception"
    if extra_text:
        msg += f": {extra_text}"
    logger.opt(exception=e).critical(msg, extra={"ctx": ctx})


@contextmanager
def timed(name: str, *, level: str = "DEBUG", warn_over_ms: Optional[int] = None):
    """Measure a code block execution time; warn if threshold exceeded."""
    t0 = time.perf_counter()
    try:
        yield
    finally:
        dt_ms = int((time.perf_counter() - t0) * 1000)
        if warn_over_ms is not None and dt_ms >= warn_over_ms:
            logger.warning(f"[timed] {name} took {dt_ms} ms (>= {warn_over_ms} ms)")
        else:
            logger.log(level.upper(), f"[timed] {name} took {dt_ms} ms")


def timed_decorator(
    name: Optional[str] = None, *, level: str = "DEBUG", warn_over_ms: Optional[int] = None
):
    """Decorator variant of `timed` for functions/coroutines."""
    def wrapper(func):
        nm = name or func.__name__
        if asyncio.iscoroutinefunction(func):
            async def inner(*args, **kwargs):
                with timed(nm, level=level, warn_over_ms=warn_over_ms):
                    return await func(*args, **kwargs)
        else:
            def inner(*args, **kwargs):
                with timed(nm, level=level, warn_over_ms=warn_over_ms):
                    return func(*args, **kwargs)
        return inner
    return wrapper


async def start_telegram_alerts_dispatcher(
    bot: Bot,
    chat_id: Union[int, str],
    queue: Optional[asyncio.Queue[str]] = None,
    *,
    parse_mode: str = "HTML",
    min_interval_sec: float = 1.0,
) -> tuple[asyncio.Queue[str], asyncio.Task]:
    """
    Spawn async task that sends texts from queue to a Telegram chat.
    The sink only enqueues; this dispatcher performs the I/O.
    """
    q: asyncio.Queue[str] = queue or asyncio.Queue(maxsize=100)

    async def _runner():
        last_sent = 0.0
        while True:
            text = await q.get()
            now = time.monotonic()
            delay = last_sent + min_interval_sec - now
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                last_sent = time.monotonic()
            except Exception as ex:
                logger.warning(f"Failed to send alert to Telegram: {ex!r}")
                await asyncio.sleep(5.0)
            q.task_done()

    task = asyncio.create_task(_runner(), name="telegram_alerts_dispatcher")
    return q, task
