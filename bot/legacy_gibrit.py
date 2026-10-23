"""Кнопки старого бота Гибрида (23.10, этап 5). Бот тот же — карточки,
которые он выставил до переключения, остались в «РМТ» со старыми кнопками.
Нажатие на них делает то же самое в CRM (bot.simple_card), а карточка тут
же перерисовывается в карточку CRM (она привязана к ремонту при
переключении — core.gibrit_cutover).

  executor_done:<card>:Готов|Возврат:<мастер>  → ✅ Готов / ❌ Возврат
  status_change:<card>                         → 🔄 Изменить выбор
  price_set / purchase_set / masterpay_set:<card> → 💰 / 🧾 / 💸
  card_delete, card_move, orders_mark_read     → больше нет
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery

from bot import simple_card
from core import gibrit_cutover
from core.storage import get_conn
from core.stores import load_stores

router = Router()

_AMOUNT = {"price_set": "gb_price", "purchase_set": "gb_parts", "masterpay_set": "gb_pay"}


def _repair_id(card) -> int | None:
    with get_conn(load_stores()[0].db_path) as conn:
        row = gibrit_cutover.find_by_card(conn, card)
    return row["id"] if row else None


async def _as(callback: CallbackQuery, data: str) -> CallbackQuery:
    return callback.model_copy(update={"data": data}).as_(callback.bot)


@router.callback_query(F.data.startswith("executor_done:"))
async def old_status(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    repair_id = _repair_id(parts[1]) if len(parts) > 2 else None
    if not repair_id:
        await callback.answer("Заказ не найден в CRM.", show_alert=True)
        return
    action = "done" if parts[2] in ("Готов", "ready") else "return"
    await simple_card.status_button(await _as(callback, f"gb_{action}:{repair_id}"))


@router.callback_query(F.data.startswith("status_change:"))
async def old_change(callback: CallbackQuery) -> None:
    repair_id = _repair_id(callback.data.split(":", 1)[1])
    if not repair_id:
        await callback.answer("Заказ не найден в CRM.", show_alert=True)
        return
    await simple_card.status_button(await _as(callback, f"gb_change:{repair_id}"))


@router.callback_query(F.data.regexp(r"^(price_set|purchase_set|masterpay_set):"))
async def old_amount(callback: CallbackQuery, state: FSMContext) -> None:
    kind, _, card = callback.data.partition(":")
    repair_id = _repair_id(card)
    if not repair_id:
        await callback.answer("Заказ не найден в CRM.", show_alert=True)
        return
    await simple_card.amount_button(await _as(callback, f"{_AMOUNT[kind]}:{repair_id}"), state)


@router.callback_query(F.data.regexp(r"^(card_delete|card_move):|^orders_mark_read$"))
async def old_gone(callback: CallbackQuery) -> None:
    await callback.answer("Эта кнопка старого бота больше не работает — всё теперь в CRM.", show_alert=True)
