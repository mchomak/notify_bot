# fsm.py
from __future__ import annotations

from typing import Optional

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from loguru import logger
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError, TimeoutError as RedisTimeoutError


# ---------- storage ----------

def create_redis_storage(redis_dsn: Optional[str]) -> RedisStorage:
    """Create RedisStorage with a sane key builder."""
    dsn = redis_dsn or "redis://localhost:6379/0"
    redis = Redis.from_url(dsn, encoding="utf-8", decode_responses=True)
    return RedisStorage(
        redis=redis,
        key_builder=DefaultKeyBuilder(with_bot_id=True, with_destiny=True),
    )


async def create_fsm_storage(redis_dsn: Optional[str]) -> BaseStorage:
    """
    Try Redis for FSM storage; fall back to in-memory if Redis is unavailable.
    """
    dsn = redis_dsn or "redis://localhost:6379/0"
    try:
        redis = Redis.from_url(dsn, encoding="utf-8", decode_responses=True)
        pong = await redis.ping()
        if not pong:
            raise RedisConnectionError("PING returned falsy")
        logger.info(f"FSM storage: RedisStorage @ {dsn}")
        return RedisStorage(
            redis=redis,
            key_builder=DefaultKeyBuilder(with_bot_id=True, with_destiny=True),
        )
    except (RedisConnectionError, RedisTimeoutError, OSError) as e:
        logger.warning(f"Redis unavailable ({e!r}). Falling back to MemoryStorage.")
        return MemoryStorage()


# ---------- generic AnyInput (kept for reuse) ----------

class AnyInput(StatesGroup):
    waiting_any = State()
    waiting_number = State()
    waiting_photo = State()
    waiting_video = State()
    waiting_document = State()
    waiting_audio = State()      # content_type == audio
    waiting_voice = State()      # content_type == voice
    waiting_video_note = State() # content_type == video_note


# ---------- alert creation FSM ----------

class AlertCreate(StatesGroup):
    waiting_title = State()
    waiting_content_type = State()
    waiting_content = State()
    waiting_datetime = State()
    waiting_timezone = State()
    waiting_repeat = State()
    waiting_cron = State()


# ---------- expectations for inputs (legacy helpers) ----------

async def expect_any(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_any)
