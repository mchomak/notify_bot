# handlers.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import (
    Message,
    BotCommand,
)
from loguru import logger
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from db import (
    Database,
    User,
    upsert_user_basic,
)
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
    ]
    await bot.set_my_commands(cmds)
    logger.info("Bot commands installed", extra={"lang": lang})


# ---------- profile ----------

@dataclass
class Profile:
    user: Optional[User]
    currency: str
    balance_xtr: Decimal


async def get_profile(session: AsyncSession, *, tg_user_id: int) -> Profile:
    """Return user profile with succeeded tx stats and XTR balance."""
    user = (await session.execute(
        select(User).where(User.user_id == tg_user_id)
    )).scalar_one_or_none()
    currency = "XTR"
    balance = user.balance if (user and user.balance is not None) else Decimal("0")

    return Profile(user=user, currency=currency, balance_xtr=balance)


def build_router(db: Database) -> Router:
    """Primary router: start/help/profile/buy + payments flow."""
    r = Router()

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
                T(lang, "profile_line_balance", balance=prof.balance_xtr),
            ]
        )
        await m.answer(text)

