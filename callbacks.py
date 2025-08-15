from __future__ import annotations

import pandas as pd
from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from aiogram import Bot
from loguru import logger

from core.keyboards import (
    alerts_list_kb,
    alert_actions_kb,
    title_kb,
)
from core.utils import set_expected
from services.reminders import finalize_and_save_alert
from tools import load_alerts, unschedule_alert, remove_alert


def build_router_callbacks(
    phrases: dict,
    scheduler: AsyncIOScheduler,
    bot: Bot,
) -> Router:
    router = Router()

    # ==== list navigation & alert view/delete ====
    @router.callback_query(F.data == "alerts_back")
    async def cb_alerts_back(cb: types.CallbackQuery) -> None:
        uid = cb.from_user.id
        df = load_alerts()
        user_df = df[df["user_id"] == uid]
        if user_df.empty:
            await cb.message.edit_text(phrases["no_alerts"])
            await cb.answer()
            return
        await cb.message.edit_text(phrases["alerts_header"], reply_markup=alerts_list_kb(user_df))
        await cb.answer()

    @router.callback_query(F.data.startswith("alert:"))
    async def cb_alert_view(cb: types.CallbackQuery) -> None:
        alert_id = cb.data.split(":", 1)[1]
        df = load_alerts()
        row = df[df["alert_id"] == alert_id]
        if row.empty:
            await cb.answer("Не найдено", show_alert=True)
            return
        r = row.iloc[0]
        title = (r.get("title") or str(alert_id)[:8]).strip() or str(alert_id)[:8]
        kind = (r.get("kind") or "one_time").lower()
        details = []
        if kind in ("", "one_time"):
            when = r["send_at"]
            details.append(f"Тип: одноразовое\nКогда: {pd.to_datetime(when).strftime('%Y-%m-%d %H:%M')}")
        elif kind == "daily":
            details.append(f"Тип: ежедневно\nВремя: {(r.get('times') or '').replace(';', ', ')}")
        elif kind == "weekly":
            details.append(
                "Тип: по дням недели\n"
                f"Дни: {(r.get('days_of_week') or '').replace(';', ', ')}\n"
                f"Время: {(r.get('times') or '').replace(';', ', ')}"
            )
        elif kind == "window_interval":
            details.append(
                f"Тип: окно\nОкно: {r.get('window_start')}-{r.get('window_end')}\n"
                f"Шаг: {int(float(r.get('interval_minutes') or 0))} мин"
            )
        elif kind == "cron":
            details.append(f"Тип: CRON\nExpr: {r.get('cron_expr')}")
        text = f"«{title}»\n" + "\n".join(details)
        await cb.message.edit_text(text, reply_markup=alert_actions_kb(alert_id, phrases))
        await cb.answer()

    @router.callback_query(F.data.startswith("alert_del:"))
    async def cb_alert_delete(cb: types.CallbackQuery) -> None:
        alert_id = cb.data.split(":", 1)[1]
        unschedule_alert(alert_id, scheduler)
        remove_alert(alert_id)
        uid = cb.from_user.id
        df = load_alerts()
        user_df = df[df["user_id"] == uid]
        if user_df.empty:
            await cb.message.edit_text(phrases["no_alerts"])
        else:
            await cb.message.edit_text(phrases["alerts_header"], reply_markup=alerts_list_kb(user_df))
        await cb.answer("Удалено")

    # ==== choose kind callbacks ====
    @router.callback_query(F.data == "kind_one_time")
    async def cb_kind_one_time(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("User {} chose ONE_TIME", cb.from_user.id)
        await state.update_data(pending_kind="one_time")
        await set_expected(state, "await_time")
        await cb.message.edit_text(phrases["ask_time"])
        await cb.answer()

    @router.callback_query(F.data == "kind_interval")
    async def cb_kind_interval(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("User {} chose INTERVAL", cb.from_user.id)
        await state.update_data(pending_kind="interval", pending_interval=None)
        await set_expected(state, "await_interval")
        await cb.message.edit_text(phrases["ask_interval_desc"])
        await cb.answer()

    # ==== time confirm ====
    @router.callback_query(F.data == "time_confirm")
    async def cb_time_confirm(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("time_confirm by user_id={}", cb.from_user.id)
        await set_expected(state, "await_title_choice")
        await cb.message.edit_text(phrases["ask_title"], reply_markup=title_kb(phrases))
        await cb.answer()

    @router.callback_query(F.data == "time_redo")
    async def cb_time_redo(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("time_redo by user_id={}", cb.from_user.id)
        await state.update_data(pending_run_at=None)
        await set_expected(state, "await_time")
        await cb.message.edit_text(phrases["ask_time"])
        await cb.answer()

    # ==== interval confirm ====
    @router.callback_query(F.data == "interval_confirm")
    async def cb_interval_confirm(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("interval_confirm by user_id={}", cb.from_user.id)
        await set_expected(state, "await_title_choice")
        await cb.message.edit_text(phrases["ask_title"], reply_markup=title_kb(phrases))
        await cb.answer()

    @router.callback_query(F.data == "interval_redo")
    async def cb_interval_redo(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("interval_redo by user_id={}", cb.from_user.id)
        await state.update_data(pending_interval=None)
        await set_expected(state, "await_interval")
        await cb.message.edit_text(phrases["ask_interval_desc"])
        await cb.answer()

    # ==== Title callbacks ====
    @router.callback_query(F.data == "title_enter")
    async def cb_title_enter(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("title_enter by user_id={}", cb.from_user.id)
        await set_expected(state, "await_title_input")
        await cb.message.edit_text(phrases["title_prompt"])
        await cb.answer()

    @router.callback_query(F.data == "title_skip")
    async def cb_title_skip(cb: types.CallbackQuery, state: FSMContext) -> None:
        logger.debug("title_skip by user_id={}", cb.from_user.id)
        await cb.message.edit_text(phrases["saving"])
        await finalize_and_save_alert(cb, state, title="", scheduler=scheduler, bot=bot, phrases=phrases)
        await cb.answer()

    return router
