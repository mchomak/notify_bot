# setup_redis.py
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message
from redis.asyncio import Redis

from fsm import (
    expect_any,
    set_waiting_payment,
    cancel_waiting_payment,
    PaymentGuard,
)


def build_fsm_diag_router(redis_url: str) -> Router:
    """Diagnostic router to verify Redis connectivity and FSM persistence."""
    r = Router(name="fsm_diag")
    r.message.middleware(PaymentGuard())

    @r.message(Command("redis_ping"))
    async def cmd_redis_ping(m: Message):
        redis = Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
        try:
            pong = await redis.ping()
        finally:
            await redis.close()
        await m.answer(f"Redis PING: {'OK' if pong else 'NO RESP'} @ {redis_url}")

    @r.message(Command("fsm_state"))
    async def cmd_fsm_state(m: Message, state: FSMContext):
        s = await state.get_state()
        await m.answer(f"FSM state: <code>{s or 'None'}</code>")

    @r.message(Command("fsm_data"))
    async def cmd_fsm_data(m: Message, state: FSMContext):
        data = await state.get_data()
        await m.answer(f"FSM data: <code>{data}</code>")

    @r.message(Command("fsm_clear"))
    async def cmd_fsm_clear(m: Message, state: FSMContext):
        await state.clear()
        await m.answer("FSM cleared.")

    @r.message(Command("fsm_set_any"))
    async def cmd_fsm_set_any(m: Message, state: FSMContext):
        await expect_any(state)
        await m.answer("FSM set: waiting_any. Send any message.")

    @r.message(Command("fsm_set_payment_dummy"))
    async def cmd_fsm_set_payment_dummy(m: Message, state: FSMContext):
        sent = await m.answer(
            "🔔 [TEST] Payment waiting set. Any other message/command will cancel and delete this message."
        )
        await set_waiting_payment(
            state,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            payload="test:dummy",
            amount="1",
            currency="XTR",
        )
        await m.answer("OK, state stored in Redis. Try /help or any text -> this message will be deleted.")

    @r.message(Command("fsm_cancel_payment"))
    async def cmd_fsm_cancel_payment(m: Message, state: FSMContext):
        await cancel_waiting_payment(state, m.bot, delete_invoice=True)
        await m.answer("Payment waiting canceled and invoice message deleted (if existed).")

    return r
