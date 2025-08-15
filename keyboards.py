from __future__ import annotations

import pandas as pd
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)


def build_main_kb(phrases: dict) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=txt) for txt in row] for row in phrases["buttons"]],
        resize_keyboard=True,
    )


def choose_kind_kb(phrases: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["one_time_btn"], callback_data="kind_one_time"),
            InlineKeyboardButton(text=phrases["interval_btn"], callback_data="kind_interval"),
        ]]
    )


def confirm_kb(phrases: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["confirm_btn"], callback_data="time_confirm"),
            InlineKeyboardButton(text=phrases["redo_btn"], callback_data="time_redo"),
        ]]
    )


def confirm_interval_kb(phrases: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["confirm_btn"], callback_data="interval_confirm"),
            InlineKeyboardButton(text=phrases["redo_btn"], callback_data="interval_redo"),
        ]]
    )


def title_kb(phrases: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=phrases["title_btn_enter"], callback_data="title_enter"),
            InlineKeyboardButton(text=phrases["title_btn_skip"], callback_data="title_skip"),
        ]]
    )


def alerts_list_kb(df_user: pd.DataFrame) -> InlineKeyboardMarkup:
    rows = []
    for _, row in df_user.iterrows():
        title = (row.get("title") or str(row["alert_id"])[:8]).strip() or str(row["alert_id"])[:8]
        rows.append([InlineKeyboardButton(text=title, callback_data=f"alert:{row['alert_id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def alert_actions_kb(alert_id: str, phrases: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=phrases["back_btn"], callback_data="alerts_back")],
            [InlineKeyboardButton(text=phrases["delete_btn"], callback_data=f"alert_del:{alert_id}")],
        ]
    )
