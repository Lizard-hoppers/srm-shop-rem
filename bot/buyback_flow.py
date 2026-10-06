"""«🛒 Покупка» телефона in the bot DM (Заход 4) — the twelve steps of the
макет in one dialog, ending in the same document the Mini App form
creates (core.buyback.create_purchase):

  номер продавца → шесть фото → модель → IMEI → поломки → цена (и валюта)
  → источники оплаты → распределение → «Проверьте и купите» → карточка.

Payment is split across the точка's accounts one source at a time: pick an
account, say how much comes out of it (a foreign-currency one — how much
of that currency and at what rate), repeat until nothing is left. The
phone lands on the buying точка's склад as a unit with its IMEI and the
price as its cost; the card goes to the buyer here and, if the точка has a
«Скупка» topic configured, to the staff group.

Reuses bot.quick_actions' Clean-Chat helpers and shift gate; the FSM
states (PhoneBuy) are declared there so its shared ❌ Отмена and fallback
handlers cover this flow too. Registered ahead of that router — its
catch-all would otherwise take this flow's steps.
"""
from __future__ import annotations

import asyncio
import html
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
    WebAppInfo,
)

from bot import quick_actions as qa
from bot.miniapp_links import crm_link
from core import accounts as core_accounts
from core import auth as core_auth
from core import buyback as core_buyback
from core import cash as core_cash
from core import clients as core_clients
from core import documents as core_documents
from core.accounts import money
from core.storage import get_conn
from core.stores import get_store

router = Router()

_ROLES = ("owner", "admin", "storekeeper")
_SLOTS = core_buyback.PHOTO_SLOTS

# A six-photo album arrives as six separate updates that aiogram handles
# concurrently — each handler reads the FSM data, appends its photo and
# writes it back, so without serializing them per chat some photos would
# overwrite others (the same class of race as the lost-fields bug in
# taki_vmeste's FSM, 28.07).
_photo_locks: dict[int, asyncio.Lock] = {}

_CURRENCY_TOKENS = {
    "uah": "UAH", "грн": "UAH", "гривен": "UAH", "₴": "UAH",
    "usd": "USD", "$": "USD", "дол": "USD", "долл": "USD", "долларов": "USD",
    "eur": "EUR", "€": "EUR", "евро": "EUR",
    "usdt": "USDT", "tether": "USDT",
}


def parse_price(text: str) -> tuple[float | int, str] | None:
    """«5000» / «5 000 грн» / «120 usd» / «$120» / «12,5 USDT» -> (amount,
    currency); гривня when no currency is named. None if there's no amount."""
    cleaned = (text or "").strip().lower().replace(" ", " ")
    currency = "UAH"
    for token, code in sorted(_CURRENCY_TOKENS.items(), key=lambda kv: -len(kv[0])):
        if token in cleaned:
            currency = code
            cleaned = cleaned.replace(token, " ")
            break
    amount = core_accounts.parse_amount(re.sub(r"[^\d.,]", "", cleaned))
    return (amount, currency) if amount else None


def _photo_prompt(count: int) -> str:
    head = "📷 Загрузите 6 фотографий телефона — покажите его с разных сторон."
    if count:
        head += f"\n✅ Загружено {count} из {len(_SLOTS)}"
    return f"{head}\nСейчас: {_SLOTS[count]}"


async def _context(callback: CallbackQuery, state: FSMContext):
    """(store, staff, data) behind a flow button, or None having answered."""
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Диалог устарел — нажмите «Покупка» ещё раз.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff or staff["role"] not in _ROLES:
        await state.clear()
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return store, staff, data


@router.message(F.text == qa.BTN_BUYBACK, F.chat.type == "private")
async def buy_start(message: Message, state: FSMContext) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _ROLES:
        await message.answer("Недостаточно прав для покупки.")
        return
    if not await qa._shift_gate(message, state, store, staff):
        return
    await qa._reset(message, state)
    await state.set_state(qa.PhoneBuy.phone)
    await state.update_data(store_id=store.id, store_name=store.name, photos=[], photo_count=0, parts=[])
    await qa._send_prompt(
        message, state,
        "Введите номер телефона продавца.\n<i>Обязательно · история контрагента будет сохранена.</i>",
    )


@router.message(qa.PhoneBuy.phone, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await qa._nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    data = await state.get_data()
    seller_name = None
    try:
        with get_conn(get_store(data["store_id"]).db_path) as conn:
            known = core_clients.get_by_phone(conn, phone)
            seller_name = known["name"] if known and known["name"] != phone else None
    except KeyError:
        pass
    await state.update_data(seller_phone=phone, seller_name=seller_name)
    await state.set_state(qa.PhoneBuy.photos)
    await qa._advance(message, state, _photo_prompt(0))


@router.message(qa.PhoneBuy.photos, F.photo)
async def buy_got_photo(message: Message, state: FSMContext) -> None:
    lock = _photo_locks.setdefault(message.chat.id, asyncio.Lock())
    async with lock:
        if await state.get_state() != qa.PhoneBuy.photos.state:
            # A 7th+ photo of the same album, arriving after the step moved on.
            await qa._consume(state, message)
            return
        file = await message.bot.get_file(message.photo[-1].file_id)
        buf = await message.bot.download_file(file.file_path)
        data = await state.get_data()
        photos = list(data.get("photos", [])) + [buf.read()]
        await state.update_data(photos=photos, photo_count=len(photos))
        if len(photos) < len(_SLOTS):
            await qa._advance(message, state, _photo_prompt(len(photos)))
            return

        await state.set_state(qa.PhoneBuy.model)
        try:
            with get_conn(get_store(data["store_id"]).db_path) as conn:
                options = core_buyback.recent_models(conn)
        except KeyError:
            options = []
        await state.update_data(model_options=options)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=name[:60], callback_data=f"buy_model:{index}")]
            for index, name in enumerate(options)
        ]) if options else None
        await qa._advance(
            message, state,
            "Укажите модель телефона.\n<i>Выберите из списка или введите вручную, например: iPhone 13 · 128 GB · Black</i>",
            reply_markup=keyboard,
        )


@router.message(qa.PhoneBuy.photos, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_photo_fallback(message: Message, state: FSMContext) -> None:
    await qa._nudge(message, state, "Пришлите фото телефона (как фото, не файлом).")


_ASK_IMEI = "Введите IMEI телефона.\n<i>Наберите на нём *#06# — пример: 356000000000001</i>"


@router.callback_query(F.data.startswith("buy_model:"), qa.PhoneBuy.model)
async def buy_pick_model(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    options = data.get("model_options") or []
    index = int(callback.data.split(":", 1)[1])
    if index >= len(options):
        await callback.answer()
        return
    await state.update_data(model=options[index])
    await state.set_state(qa.PhoneBuy.imei)
    await qa._advance_callback(callback, state, _ASK_IMEI)
    await callback.answer()


@router.message(qa.PhoneBuy.model, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_model(message: Message, state: FSMContext) -> None:
    model = " ".join(message.text.split())
    if not model:
        await qa._nudge(message, state, "Укажите модель.")
        return
    await state.update_data(model=model)
    await state.set_state(qa.PhoneBuy.imei)
    await qa._advance(message, state, _ASK_IMEI)


@router.message(qa.PhoneBuy.imei, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_imei(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        with get_conn(get_store(data["store_id"]).db_path) as conn:
            imei = core_buyback.check_imei(conn, message.text)
    except core_buyback.PurchaseError as exc:
        await qa._nudge(message, state, str(exc))
        return
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    await state.update_data(imei=imei)
    await state.set_state(qa.PhoneBuy.comment)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без поломок ➡️", callback_data="buy_comment_skip"),
    ]])
    await qa._advance(message, state, "Укажите комментарий или опишите поломки.", reply_markup=keyboard)


_ASK_PRICE = "Укажите цену покупки.\n<i>В гривне — просто число. В другой валюте: «120 USD», «100 EUR», «250 USDT».</i>"


@router.message(qa.PhoneBuy.comment, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_comment(message: Message, state: FSMContext) -> None:
    await state.update_data(comment=message.text.strip() or None)
    await state.set_state(qa.PhoneBuy.price)
    await qa._advance(message, state, _ASK_PRICE)


@router.callback_query(F.data == "buy_comment_skip", qa.PhoneBuy.comment)
async def buy_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(comment=None)
    await state.set_state(qa.PhoneBuy.price)
    await qa._advance_callback(callback, state, _ASK_PRICE)
    await callback.answer()


# ---- оплата: источники и распределение ----

def _left(data: dict):
    return money(data["total_uah"] - sum(p["amount_uah"] for p in data.get("parts", [])))


def _price_line(data: dict) -> str:
    line = f"{data['price']} {core_accounts.currency_label(data['currency'])}"
    if data["currency"] != "UAH":
        line += f" = {data['total_uah']} грн (курс {money(data['rate'])})"
    return line


def _parts_lines(data: dict) -> list[str]:
    lines = []
    for part in data.get("parts", []):
        line = f"{html.escape(part['name'])} — {part['amount']} {core_accounts.currency_label(part['currency'])}"
        if part["currency"] != "UAH":
            line += f" = {part['amount_uah']} грн"
        if part.get("short"):
            line += f"  ⚠ на счёте сейчас {part['balance']}"
        lines.append(line)
    return lines


def _sources_screen(store, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    with get_conn(store.db_path) as conn:
        accounts = core_accounts.balances(conn, store.location_id)
    buttons = [
        InlineKeyboardButton(
            text=f"{a['name']} · {a['balance']} {a['currency_label']}"[:60], callback_data=f"buy_src:{a['id']}",
        )
        for a in accounts if a["active"]
    ]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    text = "\n".join([
        "Выберите источник оплаты.",
        f"Цена: {_price_line(data)}",
        *_parts_lines(data),
        f"Осталось оплатить: {_left(data)} грн",
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def _review_screen(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    left = _left(data)
    text = "\n".join([
        "Распределите оплату по источникам.", "",
        *_parts_lines(data), "",
        f"Всего: {data['total_uah']} грн · Осталось: {left} грн",
    ])
    if abs(left) <= core_cash.PAYMENT_TOLERANCE_UAH:
        rows = [[
            InlineKeyboardButton(text="➡️ Далее", callback_data="buy_pay_done", style="success"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="buy_pay_reset"),
        ]]
    else:
        rows = [[
            InlineKeyboardButton(text="➕ Добавить оплату", callback_data="buy_pay_add", style="success"),
            InlineKeyboardButton(text="✏️ Изменить", callback_data="buy_pay_reset"),
        ]]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _to_sources(message: Message, state: FSMContext, store) -> None:
    await state.set_state(qa.PhoneBuy.pay_source)
    text, keyboard = _sources_screen(store, await state.get_data())
    await qa._advance(message, state, text, reply_markup=keyboard)


@router.message(qa.PhoneBuy.price, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_price(message: Message, state: FSMContext) -> None:
    parsed = parse_price(message.text)
    if not parsed:
        await qa._nudge(message, state, "Введите цену числом, например: 5000 — или «120 USD».")
        return
    amount, currency = parsed
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    await state.update_data(price=amount, currency=currency, parts=[])
    if currency != "UAH":
        await state.set_state(qa.PhoneBuy.rate)
        await qa._advance(message, state, f"Цена: {amount} {currency}.\nКурс — сколько гривен за 1 {currency}?")
        return
    await state.update_data(rate=1, total_uah=amount)
    await _to_sources(message, state, store)


@router.message(qa.PhoneBuy.rate, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_rate(message: Message, state: FSMContext) -> None:
    rate = core_accounts.parse_amount(message.text)
    if not rate:
        await qa._nudge(message, state, "Введите курс числом, например: 41,5.")
        return
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    await state.update_data(rate=rate, total_uah=money(float(data["price"]) * float(rate)))
    await _to_sources(message, state, store)


@router.callback_query(F.data.startswith("buy_src:"), qa.PhoneBuy.pay_source)
async def buy_pick_source(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, data = ctx
    with get_conn(store.db_path) as conn:
        account = core_accounts.get_account(conn, int(callback.data.split(":", 1)[1]))
        balance = core_accounts.balance(conn, account["id"]) if account else 0
    if not account or not account["active"] or account["location_id"] != store.location_id:
        await callback.answer("Этот счёт выбрать нельзя.", show_alert=True)
        return
    left = _left(data)
    await state.update_data(pending={
        "account_id": account["id"], "name": account["name"], "currency": account["currency"], "balance": balance,
    })
    await state.set_state(qa.PhoneBuy.pay_amount)
    label = core_accounts.currency_label(account["currency"])
    if account["currency"] == "UAH":
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"Всё оставшееся — {left} грн", callback_data="buy_amt_all"),
        ]])
        question = f"Сколько платим со счёта «{html.escape(account['name'])}»?\nОсталось оплатить: {left} грн"
    else:
        keyboard = None
        question = (
            f"Сколько {label} платим со счёта «{html.escape(account['name'])}»?\n"
            f"Осталось оплатить: {left} грн (курс спрошу следующим шагом)"
        )
    await qa._advance_callback(callback, state, question, keyboard)
    await callback.answer()


async def _add_part(state: FSMContext, amount, rate) -> dict:
    data = await state.get_data()
    pending = data["pending"]
    amount_uah = money(float(amount) * float(rate))
    parts = list(data.get("parts", [])) + [{
        "account_id": pending["account_id"], "name": pending["name"], "currency": pending["currency"],
        "amount": amount, "rate": rate, "amount_uah": amount_uah,
        "short": amount > pending["balance"], "balance": pending["balance"],
    }]
    await state.set_state(qa.PhoneBuy.pay_review)
    return await state.update_data(parts=parts, pending=None)


@router.callback_query(F.data == "buy_amt_all", qa.PhoneBuy.pay_amount)
async def buy_amount_all(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("pending") or data["pending"]["currency"] != "UAH":
        await callback.answer()
        return
    data = await _add_part(state, _left(data), 1)
    text, keyboard = _review_screen(data)
    await qa._advance_callback(callback, state, text, keyboard)
    await callback.answer()


@router.message(qa.PhoneBuy.pay_amount, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_amount(message: Message, state: FSMContext) -> None:
    amount = core_accounts.parse_amount(message.text)
    data = await state.get_data()
    pending = data.get("pending")
    if not amount or not pending:
        await qa._nudge(message, state, "Введите сумму числом.")
        return
    if pending["currency"] != "UAH":
        await state.update_data(pending={**pending, "amount": amount})
        await state.set_state(qa.PhoneBuy.pay_rate)
        await qa._advance(message, state, f"Курс — сколько гривен за 1 {pending['currency']}?")
        return
    if amount > _left(data) + core_cash.PAYMENT_TOLERANCE_UAH:
        await qa._nudge(message, state, f"Осталось оплатить только {_left(data)} грн.")
        return
    data = await _add_part(state, amount, 1)
    text, keyboard = _review_screen(data)
    await qa._advance(message, state, text, reply_markup=keyboard)


@router.message(qa.PhoneBuy.pay_rate, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_got_pay_rate(message: Message, state: FSMContext) -> None:
    rate = core_accounts.parse_amount(message.text)
    data = await state.get_data()
    pending = data.get("pending")
    if not rate or not pending:
        await qa._nudge(message, state, "Введите курс числом, например: 41,5.")
        return
    if money(float(pending["amount"]) * float(rate)) > _left(data) + core_cash.PAYMENT_TOLERANCE_UAH:
        await qa._nudge(
            message, state,
            f"{pending['amount']} {pending['currency']} по курсу {rate} — это больше, чем осталось ({_left(data)} грн).",
        )
        return
    data = await _add_part(state, pending["amount"], rate)
    text, keyboard = _review_screen(data)
    await qa._advance(message, state, text, reply_markup=keyboard)


@router.callback_query(F.data.in_({"buy_pay_add", "buy_pay_reset"}), qa.PhoneBuy.pay_review)
async def buy_pay_more(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, _data = ctx
    if callback.data == "buy_pay_reset":
        await state.update_data(parts=[])
    await state.set_state(qa.PhoneBuy.pay_source)
    text, keyboard = _sources_screen(store, await state.get_data())
    await qa._advance_callback(callback, state, text, keyboard)
    await callback.answer()


def _confirm_caption(data: dict) -> str:
    seller = html.escape(data["seller_phone"])
    if data.get("seller_name"):
        seller += f" · {html.escape(data['seller_name'])}"
    lines = [
        "📋 Проверьте данные и подтвердите покупку.", "",
        f"<b>{html.escape(data['model'])}</b>",
        f"Фото: {data['photo_count']} · IMEI: {html.escape(data['imei'])}",
    ]
    if data.get("comment"):
        lines.append(f"Поломки: {html.escape(data['comment'])}")
    lines += [
        f"Цена: {_price_line(data)}",
        f"Склад: {html.escape(data.get('store_name') or '')}",
        f"Продавец: {seller}",
        "Оплата: " + "; ".join(_parts_lines(data)),
    ]
    text = "\n".join(lines)
    return text if len(text) <= 1024 else text[:1023] + "…"


@router.callback_query(F.data == "buy_pay_done", qa.PhoneBuy.pay_review)
async def buy_to_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if abs(_left(data)) > core_cash.PAYMENT_TOLERANCE_UAH:
        await callback.answer("Оплата ещё не распределена полностью.", show_alert=True)
        return
    await state.set_state(qa.PhoneBuy.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Купить", callback_data="buy_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="buy_cancel", style="danger"),
    ]])
    # The card shows the phone itself (first photo), so it is a photo
    # message — sent new, and the text screen it replaces is removed.
    caption = _confirm_caption(data)
    previous_id = data.get("prompt_message_id") or callback.message.message_id
    sent = await callback.message.answer_photo(
        BufferedInputFile(data["photos"][0], filename="phone.jpg"), caption=caption, reply_markup=keyboard,
    )
    await state.update_data(prompt_message_id=sent.message_id, prompt_text=caption, prompt_markup=keyboard)
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, [previous_id])
    await callback.answer()


@router.message(qa.PhoneBuy.confirm, ~F.text.in_(qa._ENTRY_BUTTONS))
async def buy_confirm_fallback(message: Message, state: FSMContext) -> None:
    """A stray message while the photo card is up — the generic fallback
    would re-post it as plain text and lose the photo."""
    await qa._consume(state, message)
    sent = await message.answer("Нажмите «✅ Купить» или «❌ Отмена» на карточке выше.")
    await qa._track(state, sent)


def _create(store, staff_id: int, data: dict, key: str) -> tuple[int, str, str, list[tuple[bytes, str]]]:
    """All the blocking work of the purchase (six photos through Pillow,
    the DB writes) in one call for asyncio.to_thread. Returns
    (order_id, document label, card text, photos for the album)."""
    with get_conn(store.db_path) as conn:
        order_id = core_buyback.create_purchase(
            conn,
            seller_phone=data["seller_phone"], seller_name=data.get("seller_name"),
            model=data["model"], imei=data["imei"], comment=data.get("comment"),
            price=data["price"], currency=data["currency"], rate=data["rate"],
            payments=[(p["account_id"], p["amount"], p["rate"]) for p in data["parts"]],
            photos=[(photo, ".jpg", _SLOTS[index]) for index, photo in enumerate(data["photos"])],
            staff_id=staff_id, location_id=store.location_id, key=key,
        )
    with get_conn(store.db_path) as conn:
        return (
            order_id, core_documents.label("buyback", order_id),
            core_buyback.card_text(conn, order_id), core_buyback.read_photos(conn, order_id),
        )


@router.callback_query(F.data == "buy_confirm", qa.PhoneBuy.confirm)
async def buy_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, staff, data = ctx
    if await qa._already_done(callback, store.db_path):
        return
    try:
        order_id, label, card, photos = await asyncio.to_thread(_create, store, staff["id"], data, qa._tap_key(callback))
    except (core_buyback.PurchaseError, core_cash.PaymentError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    ids = list(data.get("user_message_ids", [])) + [callback.message.message_id]
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, ids)
    # The card as it stays in the chat: the photos as one album with the
    # card text, then the actions (an album can't carry buttons).
    try:
        await callback.message.answer_media_group([
            InputMediaPhoto(
                media=BufferedInputFile(photo, filename=name),
                caption=card if index == 0 else None, parse_mode="HTML" if index == 0 else None,
            )
            for index, (photo, name) in enumerate(photos)
        ])
    except TelegramAPIError:
        await callback.message.answer(card)
    await callback.message.answer(
        f"✅ <b>{label} — куплен.</b>\nТелефон на складе точки, деньги списаны по источникам.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="🏷 Карточка и этикетка",
            web_app=WebAppInfo(url=crm_link(f"/buyback/{order_id}", staff["id"], store.id)),
        )]]),
    )
    await callback.answer("Куплено")
    # The точка's «Скупка» topic, if one is configured — off the loop.
    asyncio.create_task(asyncio.to_thread(core_buyback.post_card_to_group, store, order_id))
