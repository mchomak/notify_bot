# handlers.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    Message,
    BotCommand,
    LabeledPrice,
    PreCheckoutQuery,
)
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db import (
    Database,
    User,
    Transaction,
    TransactionKind,
    TransactionStatus,
    upsert_user_basic,
    record_transaction,
)
from fsm import PaymentFlow, set_waiting_payment, PaymentGuard
from text import phrases


# ---------- i18n helpers ----------

def get_lang(m: Message) -> str:
    code = (m.from_user and m.from_user.language_code) or "en"
    return "ru" if code and code.startswith("ru") else "en"


def T(locale: str, key: str, **fmt) -> str:
    """Get string from `phrases` with fallback to English."""
    val = phrases.get(locale, {}).get(key) or phrases["en"].get(key) or key
    return val.format(**fmt)


def T_item(locale: str, key: str, subkey: str) -> str:
    """Get nested item e.g. phrases[locale]['help_items']['start']."""
    return phrases.get(locale, {}).get(key, {}).get(subkey) \
        or phrases["en"].get(key, {}).get(subkey, subkey)


# ---------- commands install ----------

async def install_bot_commands(bot: Bot, lang: str = "en") -> None:
    items = phrases[lang]["help_items"]
    cmds = [
        BotCommand(command="start", description=items["start"]),
        BotCommand(command="help", description=items["help"]),
        BotCommand(command="profile", description=items["profile"]),
        BotCommand(command="buy", description=items["buy"]),
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot commands installed", extra={"lang": lang})


# ---------- profile ----------

@dataclass
class Profile:
    user: Optional[User]
    txn_count: int
    txn_sum: Decimal
    currency: str
    balance_xtr: Decimal


async def get_profile(session: AsyncSession, *, tg_user_id: int) -> Profile:
    """Return user profile with succeeded tx stats and XTR balance."""
    user = (await session.execute(
        select(User).where(User.user_id == tg_user_id)
    )).scalar_one_or_none()

    stats = (await session.execute(
        select(func.count(Transaction.id), func.coalesce(func.sum(Transaction.amount), 0))
        .where((Transaction.user_id == tg_user_id) & (Transaction.status == TransactionStatus.succeeded))
    )).first()

    txn_count = int(stats[0] or 0)
    txn_sum = Decimal(str(stats[1] or 0))
    currency = "XTR"
    balance = user.balance if (user and user.balance is not None) else Decimal("0")

    return Profile(user=user, txn_count=txn_count, txn_sum=txn_sum, currency=currency, balance_xtr=balance)


# ---------- payments (Stars) ----------

class StarsPay:
    """Telegram Stars payment helper."""

    def __init__(self, db: Database):
        self.db = db

    async def send_invoice(
        self,
        m: Message,
        state: FSMContext,
        *,
        title: str,
        desc: str,
        stars: int,
        payload: str,
    ) -> None:
        """Send XTR invoice and store waiting state in FSM."""
        prices = [LabeledPrice(label=title, amount=stars)]
        sent = await m.answer_invoice(
            title=title,
            description=desc,
            payload=payload,
            provider_token="",  # Stars for digital goods
            currency="XTR",
            prices=prices,
        )
        await set_waiting_payment(
            state,
            chat_id=sent.chat.id,
            message_id=sent.message_id,
            payload=payload,
            amount=str(stars),
            currency="XTR",
        )

    async def pre_checkout_handler(self, query: PreCheckoutQuery) -> None:
        """Confirm pre-checkout (place to check stock/limits if needed)."""
        await query.answer(ok=True)

    async def on_successful_payment(self, mes: Message, state: FSMContext) -> None:
        """
        Handle successful payment:
          - delete invoice message (Bot API cannot edit invoices)
          - upsert user, record transaction, increase balance
          - clear FSM state
        """
        sp = mes.successful_payment
        if not sp:
            return

        user_id = mes.from_user.id if mes.from_user else None
        amount_xtr = Decimal(str(sp.total_amount))
        payload = sp.invoice_payload
        charge_id = getattr(sp, "telegram_payment_charge_id", None)

        # Try to delete the stored invoice message (visual "edit" effect)
        data = await state.get_data()
        inv_chat_id = data.get("invoice_chat_id")
        inv_msg_id = data.get("invoice_message_id")
        if inv_chat_id and inv_msg_id:
            try:
                await mes.bot.delete_message(chat_id=inv_chat_id, message_id=inv_msg_id)
            except Exception:
                pass

        async with self.db.session() as s:
            await upsert_user_basic(
                s,
                user_id=user_id,
                tg_username=mes.from_user.username if mes.from_user else None,
                lang=(mes.from_user.language_code if mes.from_user else None),
                last_seen_at=datetime.now(timezone.utc),
                is_premium=getattr(mes.from_user, "is_premium", False),
                is_bot=mes.from_user.is_bot if mes.from_user else False,
            )

            await record_transaction(
                s,
                user_id=user_id,
                kind=TransactionKind.purchase,
                amount=amount_xtr,
                currency="XTR",
                provider="telegram_stars",
                status=TransactionStatus.succeeded,
                title="Stars purchase",
                external_id=charge_id or payload,
                meta={"payload": payload},
            )

            db_user = (await s.execute(select(User).where(User.user_id == user_id))).scalar_one_or_none()
            cur = db_user.balance if (db_user and db_user.balance is not None) else Decimal("0")
            db_user.balance = cur + amount_xtr

        await state.clear()

        lang = get_lang(mes)
        await mes.answer(T(lang, "payment_ok", charge_id=charge_id or "-", amount=str(amount_xtr)))
        logger.info(
            "Stars payment succeeded",
            extra={"user_id": user_id, "invoice_payload": payload, "charge_id": charge_id},
        )


def build_router(db: Database) -> Router:
    """Primary router: start/help/profile/buy + payments flow."""
    r = Router()
    r.message.middleware(PaymentGuard())
    pay = StarsPay(db)

    @r.message(Command("start"))
    async def cmd_start(m: Message):
        lang = get_lang(m)
        async with db.session() as s:
            await upsert_user_basic(
                s,
                user_id=m.from_user.id,
                tg_username=m.from_user.username,
                lang=m.from_user.language_code,
                last_seen_at=datetime.now(timezone.utc),
                is_premium=getattr(m.from_user, "is_premium", False),
                is_bot=m.from_user.is_bot,
            )
        await m.answer(f"<b>{T(lang, 'start_title')}</b>\n{T(lang, 'start_desc')}")

    @r.message(Command("help"))
    async def cmd_help(m: Message):
        lang = get_lang(m)
        items = phrases[lang]["help_items"]
        lines = [f"<b>{T(lang, 'help_header')}</b>"]
        for cmd, desc in items.items():
            lines.append(f"/{cmd} — {desc}")
        await m.answer("\n".join(lines))

    @r.message(Command("profile"))
    async def cmd_profile(m: Message):
        lang = get_lang(m)
        async with db.session() as s:
            prof = await get_profile(s, tg_user_id=m.from_user.id)

        if not prof.user:
            await m.answer(T(lang, "profile_not_found"))
            return

        u = prof.user
        text = "\n".join(
            [
                f"<b>{T(lang, 'profile_title')}</b>",
                T(lang, "profile_line_id", user_id=u.user_id),
                T(lang, "profile_line_user", username=u.tg_username or "-"),
                T(lang, "profile_line_lang", lang=u.lang or "-"),
                T(lang, "profile_line_created", created=str(u.created_at) if u.created_at else "-"),
                T(lang, "profile_line_last_seen", last_seen=str(u.last_seen_at) if u.last_seen_at else "-"),
                T(lang, "profile_line_txn", count=prof.txn_count, sum=prof.txn_sum, cur=prof.currency),
                T(lang, "profile_line_balance", balance=prof.balance_xtr),
            ]
        )
        await m.answer(text)

    @r.message(Command("buy"))
    async def cmd_buy(m: Message, state: FSMContext):
        lang = get_lang(m)
        await pay.send_invoice(
            m,
            state=state,
            title=T(lang, "invoice_title"),
            desc=T(lang, "invoice_desc"),
            stars=1,
            payload=f"demo:{m.from_user.id}",
        )

    @r.pre_checkout_query()
    async def on_pre_checkout(q: PreCheckoutQuery):
        await pay.pre_checkout_handler(q)

    @r.message(F.successful_payment)
    async def on_success_payment(m: Message, state: FSMContext):
        await pay.on_successful_payment(m, state)

    return r
