"""«🧾 Услуга» in the bot DM (25.10, Павел) — on the keyboard in place of
«👤 Клиент»: что за услуга → сумма → куда оплата → «Проверьте и
проведите». The same document the Mini App's «Услуги» page makes
(core.services.sell).

The first step offers the точка's services sold before as buttons (the
frequent ones first, core.services.quick_list) — a new one is just typed
and is remembered from then on; the amount step offers the price it went
for last time. «⬅️ Назад» on every step after the first.

The FSM states (ServiceSale) are declared in bot.quick_actions so its
shared ❌ Отмена and fallback handlers cover this flow too; registered
ahead of that router like the other flows."""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot import quick_actions as qa
from bot.miniapp_links import crm_link
from core import accounts as core_accounts
from core import auth as core_auth
from core import documents as core_documents
from core import masters as core_masters
from core import notify as core_notify
from core import services as core_services
from core import simple_flow
from core.accounts import money
from core.storage import get_conn
from core.stores import get_store

router = Router()

_NAME_QUESTION = "Что за услуга? Выберите или напишите:"
_AMOUNT_QUESTION = "Сумма (грн):"
_BACK = InlineKeyboardButton(text="⬅️ Назад", callback_data="svc_back")
_CANCEL = InlineKeyboardButton(text="❌ Отмена", callback_data="svc_cancel")


def _accounts(store) -> list:
    with get_conn(store.db_path) as conn:
        return [a for a in core_accounts.list_accounts(conn, store.location_id) if a["currency"] == core_accounts.BASE_CURRENCY]


def _name_screen(store) -> tuple[str, InlineKeyboardMarkup]:
    with get_conn(store.db_path) as conn:
        known = core_services.quick_list(conn, store.location_id)
    buttons = [
        InlineKeyboardButton(text=(f"{s['name']} · {money(s['price'])}" if s["price"] else s["name"])[:40], callback_data=f"svc_pick:{s['id']}")
        for s in known
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([_CANCEL])
    return _NAME_QUESTION, InlineKeyboardMarkup(inline_keyboard=rows)


def _amount_screen(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = []
    if data.get("last_price"):
        rows.append([InlineKeyboardButton(text=f"{money(data['last_price'])} грн", callback_data="svc_amount_last")])
    rows.append([_BACK, _CANCEL])
    return _AMOUNT_QUESTION, InlineKeyboardMarkup(inline_keyboard=rows)


def _bonus_people(store) -> list:
    with get_conn(store.db_path) as conn:
        return core_services.bonus_people(conn, store.location_id)


def _bonus_screen(store, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    """Кому бонус с этой услуги — его % вверх до 10 грн (core.services)."""
    rows = []
    for person in _bonus_people(store):
        bonus = core_services.bonus_amount(data["amount"], person["pay_value"])
        rate = core_masters.rate_text("percent", person["pay_value"])
        rows.append([InlineKeyboardButton(text=f"{person['name']} · {rate} = {bonus} грн"[:60], callback_data=f"svc_bonus:{person['id']}")])
    rows.append([InlineKeyboardButton(text="Без бонуса ➡️", callback_data="svc_bonus:0")])
    rows.append([_BACK, _CANCEL])
    return "Бонус кому?", InlineKeyboardMarkup(inline_keyboard=rows)


def _pay_screen(store, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    rows = [[InlineKeyboardButton(text=f"💵 {a['name']}", callback_data=f"svc_pay:{a['id']}")] for a in _accounts(store)]
    rows.append([_BACK, _CANCEL])
    return f"Куда оплата — {money(data['amount'])} грн?", InlineKeyboardMarkup(inline_keyboard=rows)


def _confirm_screen(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    text = "\n".join([
        "🧾 <b>Проверьте и проведите</b>",
        f"Услуга: {html.escape(data['name'])}",
        f"Сумма: <b>{money(data['amount'])} грн</b>",
        *([f"Бонус: {html.escape(data['bonus_label'])}"] if data.get("bonus_label") else []),
        f"Оплата: {html.escape(data['pay_label'])}",
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Провести", callback_data="svc_confirm", style="success")],
        [_BACK, _CANCEL],
    ])


async def _store(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        return get_store(data["store_id"]), data
    except KeyError:
        await state.clear()
        await callback.answer("Диалог устарел — нажмите «🧾 Услуга» ещё раз.", show_alert=True)
        return None


@router.message(F.text == qa.BTN_SERVICE, F.chat.type == "private")
async def service_start(message: Message, state: FSMContext) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if not await qa._shift_gate(message, state, store, staff):
        return
    await qa._reset(message, state)
    await state.set_state(qa.ServiceSale.name)
    accounts = _accounts(store)
    await state.update_data(store_id=store.id, pay_needed=len(accounts) > 1, bonus_needed=bool(_bonus_people(store)))
    question, keyboard = _name_screen(store)
    await qa._consume(state, message)
    await qa._repost(message, state, qa._flow_screen(await state.get_state(), await state.get_data(), question), keyboard)


async def _named(state: FSMContext, store, name: str, last_price) -> tuple[str, InlineKeyboardMarkup]:
    data = await state.update_data(name=name, last_price=last_price)
    await state.set_state(qa.ServiceSale.amount)
    return _amount_screen(data)


@router.message(qa.ServiceSale.name, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def service_got_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    store = get_store(data["store_id"])
    try:
        name = core_services.clean_name(message.text)
    except core_services.ServiceError as exc:
        await qa._nudge(message, state, str(exc))
        return
    with get_conn(store.db_path) as conn:
        known = core_services.find(conn, store.location_id, name)
    await qa._advance(message, state, *await _named(state, store, known["name"] if known else name, known["price"] if known else None))


@router.callback_query(F.data.startswith("svc_pick:"), qa.ServiceSale.name)
async def service_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _store(callback, state)
    if not ctx:
        return
    store, _data = ctx
    with get_conn(store.db_path) as conn:
        service = core_services.get(conn, int(callback.data.split(":", 1)[1]))
    if not service or service["location_id"] != store.location_id:
        await callback.answer("Этой услуги больше нет.", show_alert=True)
        return
    await qa._advance_callback(callback, state, *await _named(state, store, service["name"], service["price"]))
    await callback.answer()


async def _after_amount(state: FSMContext, store, amount) -> tuple[str, InlineKeyboardMarkup]:
    data = await state.update_data(amount=amount)
    if data.get("bonus_needed"):
        await state.set_state(qa.ServiceSale.bonus)
        return _bonus_screen(store, data)
    return await _to_pay(state, store)


async def _to_pay(state: FSMContext, store) -> tuple[str, InlineKeyboardMarkup]:
    data = await state.get_data()
    accounts = _accounts(store)
    if len(accounts) == 1:
        data = await state.update_data(pay_account=accounts[0]["id"], pay_label=accounts[0]["name"])
        await state.set_state(qa.ServiceSale.confirm)
        return _confirm_screen(data)
    await state.set_state(qa.ServiceSale.pay)
    return _pay_screen(store, data)


@router.message(qa.ServiceSale.amount, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def service_got_amount(message: Message, state: FSMContext) -> None:
    amount = simple_flow.parse_amount(message.text)
    if not amount or amount <= 0:
        await qa._nudge(message, state, "Сумма — числом, например 300.")
        return
    store = get_store((await state.get_data())["store_id"])
    await qa._advance(message, state, *await _after_amount(state, store, amount))


@router.callback_query(F.data == "svc_amount_last", qa.ServiceSale.amount)
async def service_amount_last(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _store(callback, state)
    if not ctx:
        return
    store, data = ctx
    if not data.get("last_price"):
        await callback.answer()
        return
    await qa._advance_callback(callback, state, *await _after_amount(state, store, data["last_price"]))
    await callback.answer()


@router.callback_query(F.data.startswith("svc_bonus:"), qa.ServiceSale.bonus)
async def service_bonus(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _store(callback, state)
    if not ctx:
        return
    store, data = ctx
    person_id = int(callback.data.split(":", 1)[1])
    if person_id:
        person = next((p for p in _bonus_people(store) if p["id"] == person_id), None)
        if not person:
            await callback.answer("Этому сотруднику бонус не начислить.", show_alert=True)
            return
        bonus = core_services.bonus_amount(data["amount"], person["pay_value"])
        await state.update_data(bonus_staff_id=person_id, bonus_label=f"{person['name']} — {bonus} грн")
    else:
        await state.update_data(bonus_staff_id=None, bonus_label=None)
    await qa._advance_callback(callback, state, *await _to_pay(state, store))
    await callback.answer()


@router.callback_query(F.data.startswith("svc_pay:"), qa.ServiceSale.pay)
async def service_pay(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _store(callback, state)
    if not ctx:
        return
    store, _data = ctx
    account = next((a for a in _accounts(store) if a["id"] == int(callback.data.split(":", 1)[1])), None)
    if not account:
        await callback.answer("Этого счёта нет у точки.", show_alert=True)
        return
    data = await state.update_data(pay_account=account["id"], pay_label=account["name"])
    await state.set_state(qa.ServiceSale.confirm)
    await qa._advance_callback(callback, state, *_confirm_screen(data))
    await callback.answer()


@router.callback_query(F.data == "svc_back")
async def service_back(callback: CallbackQuery, state: FSMContext) -> None:
    """«⬅️ Назад»: the step before, what was entered still in the recap."""
    group, _, current = (await state.get_state() or "").partition(":")
    ctx = await _store(callback, state) if group == "ServiceSale" else None
    if not ctx:
        await callback.answer()
        return
    store, data = ctx
    steps = qa._steps_service(data)
    if current == "confirm":
        previous = steps[-1]
    elif current in steps[1:]:
        previous = steps[steps.index(current) - 1]
    else:
        await callback.answer()
        return
    await state.set_state(getattr(qa.ServiceSale, previous))
    screen = {"name": lambda: _name_screen(store), "amount": lambda: _amount_screen(data),
              "bonus": lambda: _bonus_screen(store, data), "pay": lambda: _pay_screen(store, data)}[previous]()
    await qa._advance_callback(callback, state, *screen)
    await callback.answer()


def _card(sale_id: int, data: dict, staff_name: str) -> str:
    return "\n".join([
        f"🧾 <b>{core_documents.label('service', sale_id)} • Услуга</b>",
        f"Услуга: {html.escape(data['name'])}",
        f"Сумма: {money(data['amount'])} грн",
        *([f"Бонус: {html.escape(data['bonus_label'])}"] if data.get("bonus_label") else []),
        f"Оплата: {html.escape(data['pay_label'])}",
        f"Провёл: {html.escape(staff_name)}",
    ])


@router.callback_query(F.data == "svc_confirm", qa.ServiceSale.confirm)
async def service_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _store(callback, state)
    if not ctx:
        return
    store, data = ctx
    if await qa._already_done(callback, store.db_path):
        return
    try:
        with get_conn(store.db_path) as conn:
            staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
            if not staff:
                raise core_services.ServiceError("Недостаточно прав.")
            sale_id = core_services.sell(conn, store.location_id, data["name"], data["amount"], data["pay_account"],
                                         staff["id"], key=qa._tap_key(callback), bonus_staff_id=data.get("bonus_staff_id"))
    except core_services.ServiceError as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    card = _card(sale_id, data, staff["name"])
    ids = list(data.get("user_message_ids", [])) + [callback.message.message_id]
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, ids)
    await callback.message.answer(card + "\n\n✅ Проведено.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть услуги", web_app=WebAppInfo(url=crm_link("/services", staff["id"], store.id))),
    ]]))
    await callback.answer("Проведено")
    if store.staff_group_chat_id:
        asyncio.create_task(asyncio.to_thread(
            core_notify.notify_staff_group, card,
            message_thread_id=store.sales_topic_id, staff_group_chat_id=store.staff_group_chat_id,
        ))
