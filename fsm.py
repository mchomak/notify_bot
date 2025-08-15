# fsm.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from aiogram import BaseMiddleware, Bot
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage, DefaultKeyBuilder
from aiogram.types import Message
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


# ---------- states ----------

class AnyInput(StatesGroup):
    waiting_any = State()
    waiting_number = State()
    waiting_photo = State()
    waiting_video = State()
    waiting_document = State()
    waiting_audio = State()      # content_type == audio
    waiting_voice = State()      # content_type == voice
    waiting_video_note = State() # content_type == video_note


class PaymentFlow(StatesGroup):
    waiting = State()  # waiting for successful_payment


# ---------- payment meta ----------

@dataclass
class PaymentMeta:
    chat_id: int
    message_id: int
    payload: str
    amount: str
    currency: str
    started_at: str  # ISO timestamp (UTC)


# ---------- helpers ----------

async def set_waiting_payment(
    state: FSMContext,
    *,
    chat_id: int,
    message_id: int,
    payload: str,
    amount: str,
    currency: str = "XTR",
) -> None:
    """Store 'waiting-for-payment' state and metadata in FSM."""
    await state.set_state(PaymentFlow.waiting)
    await state.update_data(
        invoice_chat_id=chat_id,
        invoice_message_id=message_id,
        payload=payload,
        amount=amount,
        currency=currency,
        started_at=datetime.now(timezone.utc).isoformat(),
    )


async def cancel_waiting_payment(
    state: FSMContext,
    bot: Bot,
    *,
    delete_invoice: bool = True,
) -> Optional[int]:
    """
    Cancel payment waiting and (optionally) delete invoice message.
    Returns deleted message_id or None.
    """
    data = await state.get_data()
    msg_id = data.get("invoice_message_id")
    chat_id = data.get("invoice_chat_id")

    if delete_invoice and chat_id and msg_id:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

    await state.clear()
    return msg_id


# ---------- middleware ----------

class PaymentGuard(BaseMiddleware):
    """
    If FSM is PaymentFlow.waiting, any incoming user message which is not
    `successful_payment` will cancel the payment and delete the invoice.
    """

    async def __call__(self, handler, event: Message, data: dict):
        if isinstance(event, Message):
            fsm: FSMContext = data.get("state")
            bot: Bot = data.get("bot")
            if fsm and bot:
                current = await fsm.get_state()
                if current == PaymentFlow.waiting.state:
                    if event.successful_payment is None:
                        await cancel_waiting_payment(fsm, bot, delete_invoice=True)
        return await handler(event, data)


# ---------- expectations for inputs ----------

async def expect_any(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_any)


async def expect_number(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_number)


async def expect_photo(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_photo)


async def expect_video(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_video)


async def expect_document(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_document)


async def expect_audio(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_audio)


async def expect_voice(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_voice)


async def expect_video_note(state: FSMContext) -> None:
    await state.set_state(AnyInput.waiting_video_note)
