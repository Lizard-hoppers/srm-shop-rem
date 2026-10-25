"""Простой режим ремонтов в боте (core.simple_flow, 23.10): кнопки старого
бота Гибрида на карточке — ✅ Готов / ❌ Возврат / 🔄 Изменить выбор,
💰 Выдача / 🧾 Закупка / 💸 Выплата с вводом суммы прямо в группе — и
«📦 Наш» в личке.

Pressing a card's button is open to anyone in the точка's group, as it was
in the old bot (the group is the точка's own; bot.bot keeps other bots out
of it). A sum is asked as a reply to the card with a force-reply aimed at
the one who pressed, so it reaches the bot even without admin rights in
the group; his next message there is the sum.
"""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.repair_actions import _sync_after_change
from core import auth as core_auth
from core import documents as core_documents
from core import repairs as core_repairs
from core import simple_flow
from core.storage import get_conn
from core.stores import get_store

router = Router()


class SimpleAmount(StatesGroup):
    waiting = State()


class OwnOrder(StatesGroup):
    photo = State()
    model = State()
    problem = State()
    master = State()


def _store_of(order_id: int):
    """The точка of a repair (its card may also sit in a DM as a copy)."""
    from core.stores import load_stores

    with get_conn(load_stores()[0].db_path) as conn:
        repair = core_repairs.get_repair(conn, order_id)
    if not repair:
        return None, None
    try:
        return get_store(str(repair["location_id"])), repair
    except KeyError:
        return None, repair


async def _resolve(callback: CallbackQuery):
    """(store, staff_or_None, who, repair) for a pressed button, or None
    having answered. In a DM only CRM staff may press."""
    order_id = int(callback.data.split(":", 1)[1])
    store, repair = _store_of(order_id)
    if not store or not repair:
        await callback.answer("Ремонт не найден.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        simple = simple_flow.is_simple(conn, store.location_id)
    if not simple:
        await callback.answer("Эта точка работает по обычной схеме — кнопки устарели.", show_alert=True)
        return None
    if callback.message and callback.message.chat.type == "private" and not staff:
        await callback.answer("Вы не подключены к CRM.", show_alert=True)
        return None
    who = staff["name"] if staff else callback.from_user.full_name
    return store, staff, who, repair


@router.callback_query(F.data.regexp(r"^gb_(done|return|change):\d+$"))
async def status_button(callback: CallbackQuery) -> None:
    resolved = await _resolve(callback)
    if not resolved:
        return
    store, staff, who, repair = resolved
    action = callback.data.split(":", 1)[0][3:]
    if action == "change" and repair["status"] in simple_flow.OPEN_STATUSES:
        await callback.answer("Статус ещё не выбран.")
        return
    if action != "change" and repair["status"] not in simple_flow.OPEN_STATUSES:
        await callback.answer("Уже выбрано — нажмите «🔄 Изменить выбор».", show_alert=True)
        return
    with get_conn(store.db_path) as conn:
        simple_flow.move(conn, repair["id"], action, staff["id"] if staff else None, who)
    _sync_after_change(repair["id"], store.db_path)
    master = repair["master_name"] or "мастер не назначен"
    await callback.answer(f"👨‍🔧 {master}", show_alert=action != "change")
    if action != "change" and callback.message:
        note = {"done": "Готов✅", "return": "Возврат❌"}[action]
        await callback.message.reply(f"{note}  👨‍🔧 {html.escape(master)}")


@router.callback_query(F.data.regexp(r"^gb_(price|parts|pay):\d+$"))
async def amount_button(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _resolve(callback)
    if not resolved:
        return
    store, _staff, _who, repair = resolved
    field = callback.data.split(":", 1)[0][3:]
    with get_conn(store.db_path) as conn:
        if field == "pay" and simple_flow.pay_is_by_rate(conn, repair["id"]):
            await callback.answer(
                "❌ Для штатного мастера у клиентского заказа выплата не редактируется.\n"
                "Укажите выдачу и закупку — всё посчитается автоматически по его ставке.",
                show_alert=True,
            )
            return
        current = simple_flow.card_numbers(conn, repair["id"])[field]
    emoji, label, what = simple_flow.FIELDS[field]
    mention = f'<a href="tg://user?id={callback.from_user.id}">{html.escape(callback.from_user.full_name)}</a>'
    prompt = await callback.message.reply(
        f"{emoji} {label} сейчас: {simple_flow.money_text(current)}\n\n{mention}, введите {what} числом:",
        reply_markup=ForceReply(selective=True),
    )
    await state.set_state(SimpleAmount.waiting)
    await state.update_data(order_id=repair["id"], field=field, prompt_id=prompt.message_id,
                            card_id=callback.message.message_id)
    await callback.answer()


@router.message(SimpleAmount.waiting, F.text)
async def amount_typed(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    value = simple_flow.parse_amount(message.text)
    if value is None:
        await message.reply("❌ Введите число, например 1500.")
        return
    store, _repair = _store_of(data["order_id"])
    if not store:
        await state.clear()
        return
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
        simple_flow.set_amount(conn, data["order_id"], data["field"], value, staff["id"] if staff else None)
    await state.clear()
    _sync_after_change(data["order_id"], store.db_path)
    try:
        await message.bot.delete_message(message.chat.id, data["prompt_id"])
    except TelegramAPIError:
        pass
    emoji, label, _what = simple_flow.FIELDS[data["field"]]
    who = message.from_user.username and f"@{message.from_user.username}" or html.escape(message.from_user.full_name)
    try:
        await message.bot.send_message(message.chat.id, f"{emoji} {label} {value} · {who}", reply_to_message_id=data["card_id"])
    except TelegramAPIError:
        await message.reply(f"{emoji} {label} {value}")


# ---- «📦 Наш» (in the DM; bot.quick_actions sends the button here for a simple-mode точка)

_OWN = "📦 <b>Наш заказ</b>\n"
_OWN_STEPS = ("photo", "model", "problem", "master")


def _own_back_keyboard(rows=()) -> InlineKeyboardMarkup:
    """25.10 (Павел): «⬅️ Назад» и в «Нашем заказе», как в приёме ремонта."""
    return InlineKeyboardMarkup(inline_keyboard=[*rows, [InlineKeyboardButton(text="⬅️ Назад", callback_data="own_back")]])


def _was(value) -> str:
    return f"\n<i>Было: {html.escape(value)}</i>" if value else ""


def _own_step(step: str, data: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    """Вопрос шага и его кнопки. Сводки у этого диалога нет — уже введённое
    показывается строкой «Было: …», когда к шагу вернулись «Назад»."""
    from bot import quick_actions as qa

    if step == "photo":
        return _OWN + "📸 Пришлите фото устройства:", None
    if step == "model":
        return _OWN + "📱 Модель устройства:" + _was(data.get("model")), _own_back_keyboard()
    if step == "problem":
        return _OWN + "🔧 Краткое описание:" + _was(data.get("problem")), _own_back_keyboard()
    store = get_store(data["store_id"])
    return _OWN + "Выберите мастера:", _own_back_keyboard(qa._master_rows(store, "own_pick", "Не назначать ➡️"))

async def own_start(message: Message, state: FSMContext, store) -> None:
    from bot import quick_actions as qa

    await qa._reset(message, state)
    await state.set_state(OwnOrder.photo)
    await state.update_data(store_id=store.id)
    await qa._send_prompt(message, state, "📦 <b>Наш заказ</b>\n📸 Пришлите фото устройства:")


@router.message(OwnOrder.photo, F.photo)
async def own_photo(message: Message, state: FSMContext) -> None:
    from bot import quick_actions as qa

    file = await message.bot.get_file(message.photo[-1].file_id)
    buf = await message.bot.download_file(file.file_path)
    data = await state.update_data(photo_bytes=buf.read())
    await state.set_state(OwnOrder.model)
    await qa._advance(message, state, *_own_step("model", data))


@router.message(OwnOrder.model, F.text)
async def own_model(message: Message, state: FSMContext) -> None:
    from bot import quick_actions as qa

    data = await state.update_data(model=message.text.strip())
    await state.set_state(OwnOrder.problem)
    await qa._advance(message, state, *_own_step("problem", data))


@router.message(OwnOrder.problem, F.text)
async def own_problem(message: Message, state: FSMContext) -> None:
    from bot import quick_actions as qa

    data = await state.update_data(problem=message.text.strip())
    await state.set_state(OwnOrder.master)
    await qa._advance(message, state, *_own_step("master", data))


@router.callback_query(F.data == "own_back")
async def own_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«⬅️ Назад»: снова прошлый вопрос, новый ответ заменит прежний."""
    from bot import quick_actions as qa

    group, _, current = (await state.get_state() or "").partition(":")
    if group != "OwnOrder" or current not in _OWN_STEPS[1:]:
        await callback.answer()
        return
    previous = _OWN_STEPS[_OWN_STEPS.index(current) - 1]
    data = await state.get_data()
    await state.set_state(getattr(OwnOrder, previous))
    await qa._advance_callback(callback, state, *_own_step(previous, data))
    await callback.answer()


@router.callback_query(F.data.startswith("own_pick:"), OwnOrder.master)
async def own_master(callback: CallbackQuery, state: FSMContext) -> None:
    from bot import quick_actions as qa

    data = await state.get_data()
    store = get_store(data["store_id"])
    master_id = int(callback.data.split(":", 1)[1]) or None
    if master_id and not qa._pickable_master(store, master_id):
        await callback.answer("Этого мастера выбрать нельзя.", show_alert=True)
        return
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in qa._REPAIR_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        order_id, text, keyboard, photo = simple_flow.create_own_order(
            conn, location_id=store.location_id, model=data["model"], problem=data["problem"],
            master_id=master_id, staff_id=staff["id"], photo=(data["photo_bytes"], ".jpg") if data.get("photo_bytes") else None,
            key=qa._tap_key(callback),
        )
    await asyncio.to_thread(core_repairs.notify_and_save, store, order_id, text, keyboard, photo)
    await state.clear()
    await qa._safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id,
                        f"✅ {core_documents.label('repair', order_id)} создан · 📦 Наш")
    await callback.answer("Готово")


@router.message(OwnOrder.photo)
async def own_photo_expected(message: Message, state: FSMContext) -> None:
    from bot import quick_actions as qa

    await qa._nudge(message, state, "Пришлите фото устройства (или ❌ Отмена).")
