"""«🛍 Продажа» in the bot DM (Заход 6; several items since 07.10) —
short steps ending in the same document the Mini App's sale form creates
(core.sales.create_sale):

  что продаём (IMEI или часть названия) → цена → «В чеке» (➕ ещё товар
  — the same two steps again — or дальше) → телефон клиента (обязателен;
  имя спросим только у нового номера) → куда оплата → «Проверьте и
  продайте» → карточка в рабочую группу.

Payment here is one tap: the whole total onto one гривневый счёт of the
точка, or «В долг» — the whole total onto the client's balance
(core.settlements). A split across accounts or a foreign-currency
payment is the Mini App's form — the payment step links straight to it
with the client already filled in.

Reuses bot.quick_actions' Clean-Chat helpers and shift gate; the FSM
states (SaleFlow) are declared there so its shared ❌ Отмена and fallback
handlers cover this flow too. Registered ahead of that router — its
catch-all would otherwise take this flow's steps.
"""
from __future__ import annotations

import asyncio
import html
from urllib.parse import quote

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot import quick_actions as qa
from bot.miniapp_links import crm_link
from core import accounts as core_accounts
from core import auth as core_auth
from core import cash as core_cash
from core import channel_posts
from core import clients as core_clients
from core import documents as core_documents
from core import inventory as core_inventory
from core import notify as core_notify
from core import sales as core_sales
from core.accounts import money
from core.storage import get_conn
from core.stores import get_store

router = Router()

_PICK_LIMIT = 8
_DEBT = "debt"


def _find(store, query: str) -> list[dict]:
    """What this точка can sell right now that matches: an exact IMEI, or
    a piece of a name / IMEI. A serial unit is its own option; a
    non-serial product is one option whatever партии it sits in."""
    needle = query.strip().lower()
    with get_conn(store.db_path) as conn:
        exact = core_inventory.find_unit_by_imei(conn, query)
        options: dict[tuple[str, int], dict] = {}
        for line in core_inventory.stock_lines(conn, location_id=store.location_id):
            free = line["qty"] - line["reserved"]
            if line["warehouse_kind"] != "point" or free <= 0:
                continue
            if exact:
                if line["batch_id"] != exact["id"]:
                    continue
            elif needle not in line["product_name"].lower() and needle not in (line["imei"] or ""):
                continue
            price = core_inventory.get_product(conn, line["product_id"])["price"]
            if line["is_serial"]:
                options[("u", line["batch_id"])] = {
                    "kind": "u", "id": line["batch_id"], "product_id": line["product_id"], "price": price,
                    "label": f"{line['product_name']} · IMEI {line['imei']}", "imei": line["imei"],
                }
            else:
                option = options.setdefault(("p", line["product_id"]), {
                    "kind": "p", "id": line["product_id"], "product_id": line["product_id"], "price": price,
                    "name": line["product_name"], "free": 0, "imei": None,
                })
                option["free"] += free
                option["label"] = f"{option['name']} · {option['free']} {line['unit']}"
    return list(options.values())


async def _context(callback: CallbackQuery, state: FSMContext):
    """(store, staff, data) behind a flow button, or None having answered."""
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Диалог устарел — нажмите «Продажа» ещё раз.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff:
        await state.clear()
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return store, staff, data


@router.message(F.text == qa.BTN_SALE, F.chat.type == "private")
async def sale_start(message: Message, state: FSMContext) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if not await qa._shift_gate(message, state, store, staff):
        return
    await qa._reset(message, state)
    await state.set_state(qa.SaleFlow.item)
    await state.update_data(store_id=store.id, store_name=store.name, items=[])
    await qa._send_prompt(
        message, state,
        "Что продаём? Введите IMEI или часть названия.",
    )


def _price_question(option: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    if option.get("price"):
        return "Цена продажи, грн?", InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"{option['price']} грн — как в карточке", callback_data="sl_price_default"),
        ]])
    return "Цена продажи, грн?", None


def _in_cart(data: dict, option: dict) -> int:
    """How many of this option the чек already holds."""
    return sum(i["qty"] for i in data.get("items", []) if (i["kind"], i["id"]) == (option["kind"], option["id"]))


def _takeable(data: dict, options: list[dict]) -> list[dict]:
    """Options that can still go into the чек: a unit only once, a
    product only up to its free quantity."""
    return [o for o in options if _in_cart(data, o) < (1 if o["kind"] == "u" else o["free"])]


async def _choose(state: FSMContext, option: dict) -> tuple[str, InlineKeyboardMarkup | None]:
    await state.update_data(
        item_kind=option["kind"], item_id=option["id"], product_id=option["product_id"],
        item_label=option["label"] if option["kind"] == "u" else option["label"].rsplit(" · ", 1)[0],
        default_price=option.get("price"),
    )
    await state.set_state(qa.SaleFlow.price)
    return _price_question(option)


def _total(data: dict):
    return money(sum(i["qty"] * i["price"] for i in data.get("items", [])))


def _item_lines(data: dict) -> list[str]:
    return [
        f"• {html.escape(i['label'])}" + (f" × {i['qty']}" if i["qty"] > 1 else "") + f" — {money(i['qty'] * i['price'])} грн"
        for i in data.get("items", [])
    ]


async def _to_cart(state: FSMContext, price) -> tuple[str, InlineKeyboardMarkup]:
    """The item just priced goes into the чек (one more of a product
    already there at the same price is the same line), and the «В чеке»
    screen is what comes next."""
    data = await state.get_data()
    items = [dict(i) for i in data.get("items", [])]
    same = next((i for i in items if (i["kind"], i["id"], i["price"]) == (data["item_kind"], data["item_id"], price)), None)
    if same:
        same["qty"] += 1
    else:
        items.append({
            "kind": data["item_kind"], "id": data["item_id"], "product_id": data["product_id"],
            "label": data["item_label"], "price": price, "qty": 1,
        })
    data = await state.update_data(items=items, item_kind=None, item_id=None, item_label=None, default_price=None)
    await state.set_state(qa.SaleFlow.cart)
    text = "\n".join(["🛍 <b>В чеке</b>", *_item_lines(data), f"Итого: <b>{_total(data)} грн</b>"])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё товар", callback_data="sl_more")],
        [InlineKeyboardButton(text="➡️ Дальше: клиент", callback_data="sl_next")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sl_cancel")],
    ])
    return text, keyboard


@router.callback_query(F.data == "sl_more", qa.SaleFlow.cart)
async def sale_more(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(qa.SaleFlow.item)
    await qa._advance_callback(callback, state, "Ещё товар: введите IMEI или часть названия.")
    await callback.answer()


@router.callback_query(F.data == "sl_next", qa.SaleFlow.cart)
async def sale_next(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(qa.SaleFlow.phone)
    await qa._advance_callback(callback, state, _PHONE_QUESTION)
    await callback.answer()


@router.message(qa.SaleFlow.item, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def sale_got_item(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    query = message.text.strip()
    found = await asyncio.to_thread(_find, store, query) if len(query) >= 2 else []
    options = _takeable(data, found)
    if not options:
        hint = "уже в чеке — больше свободного нет" if found else "на складе точки ничего не нашлось (или всё в резерве)"
        await qa._nudge(message, state, f"«{html.escape(query)}»: {hint}.")
        return
    if len(options) == 1:
        question, keyboard = await _choose(state, options[0])
        await qa._advance(message, state, question, keyboard)
        return
    shown = options[:_PICK_LIMIT]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=o["label"][:60], callback_data=f"sl_pick:{o['kind']}:{o['id']}")] for o in shown
    ])
    more = f" Показаны первые {len(shown)} из {len(options)} — уточните запрос." if len(options) > len(shown) else ""
    await qa._advance(message, state, f"Нашлось {len(options)} — выберите.{more}", keyboard)


@router.callback_query(F.data.startswith("sl_pick:"), qa.SaleFlow.item)
async def sale_pick(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, data = ctx
    _prefix, kind, raw_id = callback.data.split(":")
    with get_conn(store.db_path) as conn:
        if kind == "u":
            row = conn.execute("SELECT imei FROM batches WHERE id = ?", (int(raw_id),)).fetchone()
            query = row["imei"] if row else ""
        else:
            row = core_inventory.get_product(conn, int(raw_id))
            query = row["name"] if row else ""
    options = [o for o in await asyncio.to_thread(_find, store, query) if (o["kind"], o["id"]) == (kind, int(raw_id))] if query else []
    options = _takeable(data, options)
    if not options:
        await callback.answer("Этого товара уже нет в свободном остатке.", show_alert=True)
        return
    question, keyboard = await _choose(state, options[0])
    await qa._advance_callback(callback, state, question, keyboard)
    await callback.answer()


_PHONE_QUESTION = "Номер телефона клиента?\n<i>Обязательно · продажа попадёт в его историю.</i>"


@router.message(qa.SaleFlow.price, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def sale_got_price(message: Message, state: FSMContext) -> None:
    price = core_accounts.parse_amount(message.text)
    if not price:
        await qa._nudge(message, state, "Введите цену числом, например 6500.")
        return
    text, keyboard = await _to_cart(state, price)
    await qa._advance(message, state, text, keyboard)


@router.callback_query(F.data == "sl_price_default", qa.SaleFlow.price)
async def sale_price_default(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("default_price"):
        await callback.answer("Введите цену числом.", show_alert=True)
        return
    text, keyboard = await _to_cart(state, data["default_price"])
    await qa._advance_callback(callback, state, text, keyboard)
    await callback.answer()


def _pay_screen(store, staff_id: int, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    with get_conn(store.db_path) as conn:
        accounts = [a for a in core_accounts.list_accounts(conn, store.location_id) if a["currency"] == core_accounts.BASE_CURRENCY]
    rows = [[InlineKeyboardButton(text=f"💵 {a['name']}", callback_data=f"sl_pay:{a['id']}")] for a in accounts]
    rows.append([InlineKeyboardButton(text="📒 В долг (на баланс клиента)", callback_data=f"sl_pay:{_DEBT}")])
    form = f"/sales?phone={quote(data['client_phone'])}&name={quote(data.get('client_name') or '')}"
    rows.append([InlineKeyboardButton(
        text="🧮 Несколько счетов / валюта — в приложении", web_app=WebAppInfo(url=crm_link(form, staff_id, store.id)),
    )])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sl_cancel")])
    return f"Куда оплата — {_total(data)} грн?", InlineKeyboardMarkup(inline_keyboard=rows)


async def _to_pay(message: Message, state: FSMContext, store) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    staff_id = resolved[1]["id"] if resolved else 0
    await state.set_state(qa.SaleFlow.pay)
    question, keyboard = _pay_screen(store, staff_id, await state.get_data())
    await qa._advance(message, state, question, keyboard)


@router.message(qa.SaleFlow.phone, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def sale_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await qa._nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    with get_conn(store.db_path) as conn:
        known = core_clients.get_by_phone(conn, phone)
    if known:
        await state.update_data(client_phone=phone, client_name=known["name"], client_known=True)
        await _to_pay(message, state, store)
        return
    await state.update_data(client_phone=phone, client_known=False)
    await state.set_state(qa.SaleFlow.name)
    await qa._advance(message, state, "Новый клиент. Как его зовут?")


@router.message(qa.SaleFlow.name, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def sale_got_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await qa._nudge(message, state, "Введите имя клиента.")
        return
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    await state.update_data(client_name=name)
    await _to_pay(message, state, store)


def _confirm_text(data: dict) -> str:
    return "\n".join([
        "🛍 <b>Проверьте и продайте</b>",
        *_item_lines(data),
        f"Итого: <b>{_total(data)} грн</b>",
        f"Оплата: {html.escape(data['pay_label'])}",
        f"Клиент: {html.escape(data.get('client_name') or '')} · {html.escape(data['client_phone'])}",
    ])


@router.callback_query(F.data.startswith("sl_pay:"), qa.SaleFlow.pay)
async def sale_pick_pay(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, _data = ctx
    choice = callback.data.split(":", 1)[1]
    if choice == _DEBT:
        await state.update_data(pay_account=None, pay_label="в долг — на баланс клиента")
    else:
        with get_conn(store.db_path) as conn:
            account = core_accounts.get_account(conn, int(choice))
        if not account or not account["active"] or account["location_id"] != store.location_id:
            await callback.answer("Этого счёта нет у точки.", show_alert=True)
            return
        await state.update_data(pay_account=account["id"], pay_label=account["name"])
    await state.set_state(qa.SaleFlow.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Продать", callback_data="sl_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="sl_cancel")],
    ])
    await qa._advance_callback(callback, state, _confirm_text(await state.get_data()), keyboard)
    await callback.answer()


def _create(store, staff, data: dict, key: str) -> tuple[int, str]:
    """The sale itself (blocking — run in a thread). Returns (sale id,
    the card for the staff group)."""
    in_debt = data.get("pay_account") is None
    with get_conn(store.db_path) as conn:
        client_id = core_clients.get_or_create_by_phone(conn, data.get("client_name") or "", data["client_phone"], source="offline")
        sale_id = core_sales.create_sale(
            conn, client_id, "offline", staff["id"],
            [(i["product_id"], i["qty"], i["price"], i["id"] if i["kind"] == "u" else None) for i in data["items"]],
            location_id=store.location_id, key=key,
            payments=[] if in_debt else [(data["pay_account"], _total(data), None)], allow_debt=in_debt,
        )
    card = "\n".join([
        f"🛍 <b>{core_documents.label('sale', sale_id)} • Продажа</b>",
        *_item_lines(data),
        f"Итого: {_total(data)} грн",
        f"Оплата: {html.escape(data['pay_label'])}",
        f"Клиент: {html.escape(data['client_phone'])}",
        f"Продавец: {html.escape(staff['name'])}",
    ])
    return sale_id, card


def _after_sale(store, product_ids: list[int], card: str) -> None:
    """Off the event loop: the card into the staff group («Продажи»
    topic if the точка has one) and the sales channel brought in line."""
    if store.staff_group_chat_id:
        core_notify.notify_staff_group(
            card, message_thread_id=store.sales_topic_id, staff_group_chat_id=store.staff_group_chat_id,
        )
    channel_posts.sync_products(product_ids, store.id, store.db_path)


@router.callback_query(F.data == "sl_confirm", qa.SaleFlow.confirm)
async def sale_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, staff, data = ctx
    if await qa._already_done(callback, store.db_path):
        return
    try:
        sale_id, card = await asyncio.to_thread(_create, store, staff, data, qa._tap_key(callback))
    except (core_inventory.InsufficientStockError, core_sales.SaleError, core_cash.PaymentError) as exc:
        await callback.answer(str(exc)[:190], show_alert=True)
        return
    ids = list(data.get("user_message_ids", [])) + [callback.message.message_id]
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, ids)
    await callback.message.answer(
        card + "\n\n✅ Продано.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Открыть продажу", web_app=WebAppInfo(url=crm_link(f"/sales/{sale_id}", staff["id"], store.id)),
        )]]),
    )
    await callback.answer("Продано")
    asyncio.create_task(asyncio.to_thread(_after_sale, store, [i["product_id"] for i in data["items"]], card))
