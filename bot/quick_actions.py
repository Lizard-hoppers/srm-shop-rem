"""Быстрый приём — reply-клавиатура (Ремонт/Контакт/Скупка/Приход) для
DM с ботом, ведущая сотрудника по шагам вместо открытия Mini App. Тот же
набор обязательных полей, что и в веб-форме приёма (webapp/routers/
repairs.py) — оба места создают ремонт через одну общую функцию,
core.repairs.create_repair_intake, так что поведение не может разъехаться.

Диалог держится в aiogram FSM (MemoryStorage — состояние живёт в памяти
процесса бота; если сотрудник бросит диалог на середине, в БД ничего не
запишется, следующий /start или повторный тап на кнопку начнёт всё
заново без остатков).

Clean Chat (манифест §2): в чате в любой момент ровно ОДИН экран — одно
сообщение бота, всегда последнее, а ответы сотрудника (имя, телефон и
т.д.) удаляются сразу после того, как прочитаны. Введённое не теряется:
каждый экран показывает шапку с уже заполненными полями (_flow_screen
ниже). Подробнее про то, почему шаг переотправляется, а не
редактируется — в комментарии к блоку хелперов."""
from __future__ import annotations

import asyncio
import html

from aiogram import BaseMiddleware, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot.miniapp_links import crm_link
from core import accounts as core_accounts
from core import auth as core_auth
from core import cash as core_cash
from core import clients as core_clients
from core import documents as core_documents
from core import inventory as core_inventory
from core import money_ops as core_money_ops
from core import production as core_production
from core import repairs as core_repairs
from core import overview as core_overview
from core import sales as core_sales
from core import settlements as core_settlements
from core import shifts as core_shifts
from core import store_access
from core.accounts import money
from core.storage import get_conn
from core.stores import StoreConfig, get_store
from core.timefmt import kyiv_date_range_utc, kyiv_today

router = Router()

# Mirrors webapp.routers.repairs._REPAIR_WRITE_ROLES — kept as its own
# copy (same convention as bot/repair_attachments.py's _REPAIR_ROLES)
# rather than importing from webapp, which would pull FastAPI-only code
# into the bot process for no reason.
_REPAIR_ROLES = ("owner", "admin", "manager", "master")
# Mirrors webapp.routers.buyback._BUYBACK_ROLES.
_BUYBACK_ROLES = ("owner", "admin", "storekeeper", "manager")
# Mirrors bot.purchase_photo._DRAFT_ROLES.
_PURCHASE_ROLES = ("owner", "admin", "storekeeper", "manager")
# Mirrors webapp.routers.cash._CASH_ROLES.
_CASH_ROLES = ("owner", "admin", "storekeeper", "manager")
# «Видят все, корректируют только владельцы» — внести/снять is a correction
# of a balance (mirrors webapp.routers.cash._OWNER_ROLES).
_ADJUST_ROLES = ("owner", "admin")

# Real button colors: `style` isn't a typed aiogram field (not in
# KeyboardButton.model_fields), but the Pydantic model has extra="allow"
# and forwards whatever it's given straight into the Bot API JSON — the
# same undocumented-but-real technique already used throughout
# taki_vmeste/ui.py (style="primary"/blue, "success"/green,
# "danger"/red), confirmed live against that bot 24.08. Leave the kwarg
# off entirely for a plain/unstyled button — Приход stays plain, Павел's
# call.
BTN_REPAIR = "🔧 Принять ремонт"
# The label this button had before Заход 5. Telegram keeps showing a
# keyboard until the bot sends a new one, so people still tap it — it
# must keep starting the intake, not fall into silence (06.10).
BTN_REPAIR_LEGACY = "🔧 Ремонт"
BTN_OUR_REPAIR = "🛠 Наш ремонт"
BTN_SALE = "🛍 Продажа"
BTN_CLIENT = "👤 Клиент"
BTN_BUYBACK = "🛒 Покупка"
BTN_PURCHASE = "📦 Приход"
BTN_SUMMARY = "📊 Сводка"
BTN_CASH = "💵 Касса"
BTN_TRANSFER = "🔁 Перемещение"
BTN_CANCEL = "❌ Отмена"
# The one button of the group keyboard (bot/repairs_button.py) — listed
# here so its text is normalised like the rest.
BTN_REPAIRS_LIST = "🔧 Ремонты"
_ENTRY_BUTTONS = {
    BTN_SALE, BTN_REPAIR, BTN_REPAIR_LEGACY, BTN_OUR_REPAIR, BTN_CLIENT, BTN_BUYBACK, BTN_PURCHASE, BTN_SUMMARY, BTN_CASH, BTN_TRANSFER, BTN_CANCEL,
    BTN_REPAIRS_LIST,
}


# Telegram clients append the invisible variation selector U+FE0F to some
# emoji when a reply-keyboard button is tapped — «🛠 Наш ремонт» arrives as
# «🛠\ufe0f Наш ремонт» — and every handler here matches a button by its
# exact text. Two buttons added in Заходы 5–6 (🛠, 🛍) were dead in the real
# client because of it while every test passed (06.10). So: an incoming
# text that is one of our buttons give or take that selector is put back
# into its canonical form before any handler sees it.
_VARIATION_SELECTOR = "\ufe0f"


def canonical_button_text(text: str | None) -> str | None:
    """The button label this text is, if it differs from one only by
    variation selectors / outer spaces; otherwise the text unchanged."""
    if not text:
        return text
    stripped = text.replace(_VARIATION_SELECTOR, "").strip()
    for label in _ENTRY_BUTTONS:
        if stripped == label.replace(_VARIATION_SELECTOR, ""):
            return label
    return text


class ButtonTextMiddleware(BaseMiddleware):
    """Outer middleware for messages — see canonical_button_text."""

    async def __call__(self, handler, event, data):
        if isinstance(event, Message) and event.text:
            canonical = canonical_button_text(event.text)
            if canonical != event.text:
                event = event.model_copy(update={"text": canonical}).as_(data.get("bot"))
        return await handler(event, data)


# One single keyboard, always — Отмена lives on it permanently instead of
# swapping to a separate cancel-only keyboard mid-flow. Telegram was
# collapsing/hiding the custom keyboard between messages without
# is_persistent=True (Bot API 7.0+, "always show the keyboard when the
# regular keyboard is hidden") — Павел reported the buttons kept
# disappearing; swapping between two different keyboards made it worse
# (every swap is a fresh chance for a client to auto-collapse it). Set
# once at /start (bot/handlers.py) and never re-sent below — a custom
# reply keyboard stays docked at the bottom of the chat regardless of
# what other messages/edits happen, so there's no need to re-attach it.
QUICK_ACTIONS_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_SALE, style="success"), KeyboardButton(text=BTN_BUYBACK, style="success")],
        [KeyboardButton(text=BTN_REPAIR), KeyboardButton(text=BTN_OUR_REPAIR)],
        [KeyboardButton(text=BTN_PURCHASE), KeyboardButton(text=BTN_TRANSFER)],
        [KeyboardButton(text=BTN_CLIENT, style="primary"), KeyboardButton(text=BTN_CASH)],
        [KeyboardButton(text=BTN_SUMMARY), KeyboardButton(text=BTN_CANCEL, style="danger")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


class RepairIntake(StatesGroup):
    """Order (18.09, Павел): фото сразу, пока устройство ещё в руках у
    сотрудника — потом неисправность/модель/оценка, и только в конце
    контакты клиента, которые обычно уже написаны в талоне/на бумажке
    рядом. Тип устройства (device_type) убран совсем — "модель" одной
    строкой ("iPhone 12") несёт достаточно, а отдельный вопрос только
    замедлял приём."""
    # 11.10 (Павел): порядок шагов — как в боте «Гибрид сервис», к которому
    # привыкли приёмщики: фото → телефон → имя → модель → неисправность →
    # стоимость → пароль → комментарий → мастер, и только потом карточка.
    photo = State()
    phone = State()
    name = State()
    model = State()
    defect = State()
    price = State()
    password = State()
    comment = State()
    master = State()
    confirm = State()


class QuickContact(StatesGroup):
    name = State()
    phone = State()
    confirm = State()


class PhoneBuy(StatesGroup):
    """«🛒 Покупка» телефона (Заход 4) — handlers live in
    bot/buyback_flow.py; the states are declared here so the shared
    cancel/fallback handlers at the bottom of this module cover them like
    every other flow. The first six are the numbered steps of the header
    («шаг 3 из 6»); the rest are the price's rate, the payment split and
    the final card."""
    phone = State()
    photos = State()
    model = State()
    imei = State()
    comment = State()
    price = State()
    rate = State()
    pay_source = State()
    pay_amount = State()
    pay_rate = State()
    pay_review = State()
    confirm = State()


class CashExpense(StatesGroup):
    """`rate` is asked only when the expense is not in гривня — without it
    a 100 $ расход would count as 100 грн in прибыль."""
    currency = State()
    amount = State()
    rate = State()
    category = State()
    comment = State()
    confirm = State()


class CashAdjustment(StatesGroup):
    """Shared by both «➕ Внести» and «➖ Снять» — a `direction` field in
    the FSM data ("in"/"out", set at entry by cash_menu_in/cash_menu_out)
    tells the two apart — one flow with a branch rather than two
    separate StatesGroups."""
    currency = State()
    amount = State()
    comment = State()
    confirm = State()


class CashExchange(StatesGroup):
    """«💱 Обмен» гривна ↔ доллар — what was handed over and what was
    received; the rate is derived from the two (core.money_ops.exchange)."""
    direction = State()
    amount_from = State()
    amount_to = State()
    confirm = State()


class TransferFlow(StatesGroup):
    """«🔁 Перемещение» — handlers live in bot/transfer_flow.py; the
    states are declared here so the shared cancel/fallback handlers at the
    bottom of this module cover them like every other flow."""
    search = State()
    qty = State()
    review = State()


class SaleFlow(StatesGroup):
    """«🛍 Продажа» (Заход 6) — handlers live in bot/sale_flow.py; the
    states are declared here so the shared cancel/fallback handlers at the
    bottom of this module cover them like every other flow. `name` is
    asked only for a phone number the base doesn't know yet."""
    item = State()
    price = State()
    cart = State()
    phone = State()
    name = State()
    pay = State()
    confirm = State()


class PartAdd(StatesGroup):
    """«➕ Новая запчасть» from the part picker — handlers live in
    bot/part_add_flow.py; declared here so the shared cancel/fallback
    handlers at the bottom of this module cover it like every other flow."""
    photo = State()
    name = State()
    cost = State()


class ShiftOpen(StatesGroup):
    """«Есть расхождение» — one text step: what exactly doesn't match."""
    note = State()


class ClientSearch(StatesGroup):
    """One text step (phone or name), then either the match is shown
    directly (exactly one hit) or a picker of callback buttons (several
    hits) — no separate "pick" state needed, since a pick is a
    self-contained callback (client_pick:{id}), not more text to collect."""
    query = State()


def _tap_key(callback: CallbackQuery) -> str:
    """Idempotency key for a confirm button: the card it sits on. A second
    tap on the same card (a double tap, Telegram redelivering the update)
    finds the document the first one created — core.documents.find_by_key
    — instead of making another."""
    return f"tg:{callback.message.chat.id}:{callback.message.message_id}"


async def _already_done(callback: CallbackQuery, db_path: str) -> bool:
    with get_conn(db_path) as conn:
        done = core_documents.find_by_key(conn, _tap_key(callback))
    if done:
        await callback.answer(f"Уже проведено: {core_documents.doc_label(done)}", show_alert=True)
    return bool(done)


def _resolve_staff_for_dm(telegram_id: int) -> tuple[StoreConfig, object] | None:
    """(store, staff_row) for a DM sender, same "current store" resolution
    as bot/purchase_photo.py's _resolve_store_for_dm — None if telegram_id
    isn't CRM staff anywhere."""
    accessible = store_access.accessible_stores(telegram_id)
    if not accessible:
        return None
    store = store_access.pick_default_store(telegram_id, accessible)
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, telegram_id)
    if not staff:
        return None
    return store, staff


# --- Смена (Заход 2): before the first operation of the day the employee
# looks at the точка's balances and opens their shift — «всё совпадает» or
# «есть расхождение» (core.shifts). Every entry point that WRITES something
# (ремонт, скупка, приход, касса) goes through _shift_gate; looking things
# up (клиент, сводка) doesn't need a shift. ---

def _shift_is_open(store: StoreConfig, staff) -> bool:
    with get_conn(store.db_path) as conn:
        return core_shifts.current_shift(conn, staff["id"], store.location_id) is not None


def _shift_screen(store: StoreConfig, staff) -> tuple[str, InlineKeyboardMarkup]:
    with get_conn(store.db_path) as conn:
        snap = core_shifts.snapshot(conn, store.location_id)
    lines = [f"<b>{html.escape(staff['name'])} · {html.escape(store.name)}</b>", "Сверьте остатки:", ""]
    for account in snap["accounts"]:
        lines.append(f"{html.escape(account['name'])}: {account['balance']} {account['currency_label']}")
    lines.append(f"В товаре по всему бизнесу: {snap['stock_value']} грн")
    lines += ["", "<i>Видят все. Корректируют только владельцы.</i>"]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✓ Всё совпадает — открыть смену", callback_data="shift_open_ok", style="success")],
        [InlineKeyboardButton(text="⚠ Есть расхождение", callback_data="shift_open_mismatch")],
    ])
    return "\n".join(lines), keyboard


async def _shift_gate(message: Message, state: FSMContext, store: StoreConfig, staff) -> bool:
    """True if today's shift is open. Otherwise the tap is swallowed and
    the «сверить остатки» screen takes its place — after opening, the
    employee taps the action again."""
    if _shift_is_open(store, staff):
        return True
    await _reset(message, state)
    await state.update_data(store_id=store.id)
    await _consume(state, message)
    text, keyboard = _shift_screen(store, staff)
    await _repost(message, state, text, keyboard)
    return False


async def _resolve_shift_actor(callback: CallbackQuery, state: FSMContext):
    """(store, staff) for whoever tapped a shift button, or None (having
    answered the callback). The store comes from the FSM data when the
    screen was put up by _shift_gate, and is re-resolved from the tapper's
    identity when it was the standalone «Открыть смену» button from /start
    (no FSM data behind that one)."""
    data = await state.get_data()
    store = None
    if data.get("store_id"):
        try:
            store = get_store(data["store_id"])
        except KeyError:
            store = None
    if store is None:
        resolved = _resolve_staff_for_dm(callback.from_user.id)
        if not resolved:
            await callback.answer("Вы не подключены как сотрудник в CRM.", show_alert=True)
            return None
        store = resolved[0]
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff:
        await callback.answer("Вы не подключены как сотрудник в CRM.", show_alert=True)
        return None
    return store, staff


_SHIFT_OPENED_TEXT = "✅ <b>Смена открыта!</b>\nТеперь можно оформлять покупки и вести обычную работу."


@router.callback_query(F.data == "shift_begin")
async def shift_begin(callback: CallbackQuery, state: FSMContext) -> None:
    """«💼 Открыть смену» under the /start greeting."""
    actor = await _resolve_shift_actor(callback, state)
    if not actor:
        return
    store, staff = actor
    if _shift_is_open(store, staff):
        await _safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, _SHIFT_OPENED_TEXT)
        await callback.answer()
        return
    await state.update_data(store_id=store.id, prompt_message_id=callback.message.message_id)
    text, keyboard = _shift_screen(store, staff)
    await _safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, text, keyboard)
    await callback.answer()


@router.callback_query(F.data == "shift_open_ok")
async def shift_open_ok(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await _resolve_shift_actor(callback, state)
    if not actor:
        return
    store, staff = actor
    with get_conn(store.db_path) as conn:
        core_shifts.open_shift(conn, staff["id"], store.location_id)
    await state.clear()
    await _safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, _SHIFT_OPENED_TEXT)
    await callback.answer("Смена открыта")


@router.callback_query(F.data == "shift_open_mismatch")
async def shift_open_mismatch(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await _resolve_shift_actor(callback, state)
    if not actor:
        return
    store, _staff = actor
    await state.set_state(ShiftOpen.note)
    await state.update_data(store_id=store.id, prompt_message_id=callback.message.message_id)
    question = "⚠ Напишите, что именно не сходится (например: «наличных на 200 грн меньше»):"
    await _safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, question)
    await state.update_data(prompt_text=question, prompt_markup=None)
    await callback.answer()


# ~in_(_ENTRY_BUTTONS): this handler sits above the entry points and the
# ❌ Отмена handler in this router, so without the exclusion a tap on any
# keyboard button while the note is awaited would be read as the note's
# text instead of doing what the button says.
@router.message(ShiftOpen.note, F.text, ~F.text.in_(_ENTRY_BUTTONS))
async def shift_got_note(message: Message, state: FSMContext) -> None:
    note = message.text.strip()
    if not note:
        await _nudge(message, state, "Опишите расхождение текстом.")
        return
    data = await state.get_data()
    resolved = _resolve_staff_for_dm(message.from_user.id)
    try:
        store = get_store(data["store_id"])
    except KeyError:
        store = None
    if not resolved or store is None:
        await _consume(state, message)
        await state.clear()
        return
    staff = resolved[1]
    with get_conn(store.db_path) as conn:
        core_shifts.open_shift(conn, staff["id"], store.location_id, discrepancy_note=note)
        owners = [
            row["telegram_id"] for row in core_auth.list_staff(conn)
            if row["role"] in _ADJUST_ROLES and row["telegram_id"] and row["telegram_id"] != message.from_user.id
        ]
    await _consume(state, message)
    await _repost(
        message, state,
        f"{_SHIFT_OPENED_TEXT}\n\nРасхождение записано и передано владельцу: {html.escape(note)}",
    )
    await state.clear()
    # The discrepancy is a signal for whoever is allowed to correct a
    # balance — tell them now rather than waiting for them to open the
    # journal. Best effort: an owner who never started the bot in DM
    # simply doesn't get it (the shift document still carries the note).
    alert = (
        f"⚠ <b>Расхождение при открытии смены</b>\n"
        f"{html.escape(staff['name'])} · {html.escape(store.name)}\n{html.escape(note)}"
    )
    for telegram_id in owners:
        try:
            await message.bot.send_message(telegram_id, alert)
        except TelegramAPIError:
            pass


# --- Clean Chat helpers (манифест §2). Инвариант: у диалога ровно ОДНО
# сообщение бота, оно всегда ПОСЛЕДНЕЕ в чате, а ответы сотрудника
# удаляются сразу, как только прочитаны.
#
# Раньше это же сообщение редактировалось на месте — и это неверно для
# чата: правка оставляет сообщение там, где оно уже висит, поэтому сразу
# после ответа сотрудника следующий вопрос оказывается ВЫШЕ его ответа —
# за экраном, без уведомления и без бейджа непрочитанного. Павел
# сообщил об этом 03.09: «отвечаю, а бот ничего не присылает».
#
# Отсюда правило: шаг, вызванный СООБЩЕНИЕМ, переотправляется (послать
# новое → удалить старое); шаг, вызванный НАЖАТИЕМ КНОПКИ, по-прежнему
# редактируется на месте — нажатие ничего не добавляет в чат, значит
# сообщение бота и так последнее (переотправка дала бы только мигание).
#
# Удалять чужие сообщения бот вправе: Bot API прямо разрешает удаление
# ВХОДЯЩИХ сообщений в личном чате, не только собственных исходящих —
# на этом и держится «удалить ответ, как только он прочитан». ---

_SEPARATOR = "──────────────"


def _steps_repair(_data: dict) -> list[str]:
    return ["photo", "phone", "name", "model", "defect", "price", "password", "comment", "master"]


def _steps_contact(_data: dict) -> list[str]:
    return ["name", "phone"]


# HTML везде ниже: бот поднят с parse_mode=HTML (bot/bot.py), а имя клиента
# и описание неисправности сотрудник вводит руками. Без экранирования имя
# вида «Вася <дома>» валит editMessageText/sendMessage на разборе HTML —
# и раньше это тихо съедалось _safe_edit, то есть диалог просто замирал
# без единого следа в логах (манифест §7).

def _client_line(label: str, data: dict) -> str | None:
    # Neither field required alone: RepairIntake now asks for the phone
    # BEFORE the name (18.09), so a recap shown between those two steps
    # has a phone but no name yet — this used to require name and so
    # dropped an already-collected phone silently until the name step.
    name = data.get("client_name")
    phone = data.get("client_phone")
    if not name and not phone:
        return None
    parts = [html.escape(name)] if name else []
    if phone:
        parts.append(html.escape(phone))
    return f"{label}: " + " · ".join(parts)


def _device_line(data: dict) -> str | None:
    device = " ".join(x for x in (data.get("device_type"), data.get("model")) if x)
    return f"Устройство: {html.escape(device)}" if device else None


def _summary_repair(data: dict) -> list[str]:
    # Order mirrors collection order (photo first, client contacts last) —
    # a mid-flow recap should read like "what I've told the bot so far",
    # not a fixed card layout.
    lines = []
    if data.get("photo_bytes"):
        lines.append("Фото: приложено ✅")
    lines.append(_client_line("Клиент", data))
    lines.append(_device_line(data))
    if data.get("defect_description"):
        lines.append(f"Неисправность: {html.escape(data['defect_description'])}")
    if data.get("price_estimate"):
        lines.append(f"Оценка: {data['price_estimate']} грн")
    if data.get("device_password"):
        lines.append(f"Пароль: {html.escape(data['device_password'])}")
    if data.get("intake_comment"):
        lines.append(f"Комментарий: {html.escape(data['intake_comment'])}")
    return [line for line in lines if line]


def _steps_phone_buy(_data: dict) -> list[str]:
    return ["phone", "photos", "model", "imei", "comment", "price"]


def _summary_phone_buy(data: dict) -> list[str]:
    lines = []
    if data.get("seller_phone"):
        seller = html.escape(data["seller_phone"])
        if data.get("seller_name"):
            seller += f" · {html.escape(data['seller_name'])}"
        lines.append(f"Продавец: {seller}")
    if data.get("photo_count"):
        lines.append(f"Фото: {data['photo_count']}")
    if data.get("model"):
        lines.append(f"Модель: {html.escape(data['model'])}")
    if data.get("imei"):
        lines.append(f"IMEI: {html.escape(data['imei'])}")
    if data.get("comment"):
        lines.append(f"Поломки: {html.escape(data['comment'])}")
    return lines


def _summary_contact(data: dict) -> list[str]:
    return [f"Имя: {html.escape(data['name'])}"] if data.get("name") else []


def _steps_cash_expense(data: dict) -> list[str]:
    rate = ["rate"] if data.get("currency", core_accounts.BASE_CURRENCY) != core_accounts.BASE_CURRENCY else []
    return ["currency", "amount", *rate, "category", "comment"]


def _cur(data: dict) -> str:
    return core_accounts.currency_label(data.get("currency") or core_accounts.BASE_CURRENCY)


def _summary_cash_expense(data: dict) -> list[str]:
    lines = []
    if data.get("amount"):
        lines.append(f"Сумма: {data['amount']} {_cur(data)}")
    if data.get("rate"):
        lines.append(f"Курс: {data['rate']} грн")
    if data.get("category"):
        lines.append(f"Категория: {core_cash.EXPENSE_CATEGORIES[data['category']]}")
    return lines


def _steps_cash_adjustment(_data: dict) -> list[str]:
    return ["currency", "amount", "comment"]


def _summary_cash_adjustment(data: dict) -> list[str]:
    if not data.get("amount"):
        return []
    verb = "Внести" if data.get("direction") == "in" else "Снять"
    return [f"{verb}: {data['amount']} {_cur(data)}"]


def _steps_cash_exchange(_data: dict) -> list[str]:
    return ["direction", "amount_from", "amount_to"]


def _summary_cash_exchange(data: dict) -> list[str]:
    lines = []
    if data.get("from_currency"):
        lines.append(f"Обмен: {core_accounts.currency_label(data['from_currency'])} → "
                     f"{core_accounts.currency_label(data['to_currency'])}")
    if data.get("amount_from"):
        lines.append(f"Отдаём: {data['amount_from']} {core_accounts.currency_label(data['from_currency'])}")
    return lines


def _steps_sale(data: dict) -> list[str]:
    return ["item", "price", "phone"] + ([] if data.get("client_known") else ["name"]) + ["pay"]


def _summary_sale(data: dict) -> list[str]:
    lines = []
    items = data.get("items") or []
    for item in items:
        qty = f" × {item['qty']}" if item["qty"] > 1 else ""
        lines.append(f"В чеке: {html.escape(item['label'])}{qty} — {money(item['qty'] * item['price'])} грн")
    if len(items) > 1:
        lines.append(f"Итого: {money(sum(i['qty'] * i['price'] for i in items))} грн")
    if data.get("item_label"):
        lines.append(f"Товар: {html.escape(data['item_label'])}")
    lines.append(_client_line("Клиент", data))
    return [line for line in lines if line]


# Ключ — имя StatesGroup ровно так, как aiogram отдаёт его в
# state.get_state() ("RepairIntake:model").
_FLOWS = {
    "RepairIntake": ("🔧 Приём ремонта", _steps_repair, _summary_repair),
    "PhoneBuy": ("🛒 Покупка телефона", _steps_phone_buy, _summary_phone_buy),
    "SaleFlow": ("🛍 Продажа", _steps_sale, _summary_sale),
    "QuickContact": ("👤 Новый клиент", _steps_contact, _summary_contact),
    "CashExpense": ("💵 Расход", _steps_cash_expense, _summary_cash_expense),
    "CashAdjustment": ("💵 Касса", _steps_cash_adjustment, _summary_cash_adjustment),
    "CashExchange": ("💱 Обмен", _steps_cash_exchange, _summary_cash_exchange),
}


def _flow_screen(state_name: str | None, data: dict, question: str) -> str:
    """Экран шага: шапка («шаг 3 из 6»), всё уже введённое, и текущий
    вопрос. Блок «уже введённое» важнее, чем кажется: ответы сотрудника
    удаляются по мере прочтения, так что до карточки подтверждения это
    единственное место, где введённое вообще видно."""
    group, _, step_name = (state_name or "").partition(":")
    spec = _FLOWS.get(group)
    if not spec:
        return question
    title, steps_of, summary_of = spec
    steps = steps_of(data)
    if step_name not in steps:
        # Карточки подтверждения (RepairIntake.confirm и т.д.) приносят
        # свой полный текст со всей сводкой — шапку добавлять некуда.
        return question
    header = f"{title} · шаг {steps.index(step_name) + 1} из {len(steps)}"
    return "\n".join([header, *summary_of(data), _SEPARATOR, question])


async def _safe_edit(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass  # already edited/deleted (e.g. a duplicate/late update) — never worth crashing the handler over


async def _safe_delete_many(bot, chat_id: int, message_ids: list[int]) -> None:
    if not message_ids:
        return
    try:
        await bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
    except TelegramBadRequest:
        # One bad id (already gone, too old, ...) fails the WHOLE batch —
        # fall back to one-by-one so the other, perfectly deletable
        # messages don't get stranded over it.
        for mid in message_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=mid)
            except TelegramBadRequest:
                pass


async def _track(state: FSMContext, message: Message) -> None:
    """Запасной путь: id входящего сообщения, которое НЕ удалось удалить
    сразу (см. _consume), чтобы подмести его на выходе из диалога."""
    data = await state.get_data()
    ids = data.get("user_message_ids", [])
    ids.append(message.message_id)
    await state.update_data(user_message_ids=ids)


async def _consume(state: FSMContext, message: Message) -> None:
    """Удалить собственное сообщение сотрудника сразу, как только оно
    прочитано — тап по кнопке входа, ответ на вопрос, присланное фото."""
    try:
        await message.bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)
    except TelegramAPIError:
        # Намеренно шире, чем TelegramBadRequest у _safe_edit: неудачная
        # уборка не должна стоить сотруднику шага диалога. Что бы ни
        # случилось — запоминаем id и подметём в конце.
        await _track(state, message)


async def _repost(message: Message, state: FSMContext, text: str, reply_markup=None, remember: str | None = None) -> None:
    """Отправить экран диалога НОВЫМ сообщением вниз чата и удалить
    предыдущее. Именно в таком порядке: чат ни на мгновение не остаётся
    без экрана, а неудачная отправка не уносит с собой тот вопрос, на
    который сотрудник прямо сейчас смотрит."""
    data = await state.get_data()
    previous_id = data.get("prompt_message_id")
    sent = await message.answer(text, reply_markup=reply_markup)
    await state.update_data(
        prompt_message_id=sent.message_id,
        # Что потом переотрисует _nudge под своим предупреждением: сам
        # экран, а не экран, уже несущий предупреждение.
        prompt_text=text if remember is None else remember,
        # Класть сюда aiogram-объект безопасно: бот поднят на MemoryStorage
        # (обычный dict, без pickle), а InlineKeyboardMarkup не держит
        # ссылку на живой Bot — то есть это не та ловушка, что уронила
        # рассылку в taki_vmeste. Зато _nudge теперь может вернуть кнопки
        # карточки подтверждения, а не потерять их молча.
        prompt_markup=reply_markup,
    )
    if previous_id:
        await _safe_delete_many(message.bot, message.chat.id, [previous_id])


async def _reset(message: Message, state: FSMContext) -> None:
    """Бросить недоделанный диалог вместе с его следами перед началом
    нового. Голый state.clear() забывал prompt_message_id — экран
    брошенного диалога оставался висеть в чате навсегда."""
    data = await state.get_data()
    ids = list(data.get("user_message_ids", []))
    if data.get("prompt_message_id"):
        ids.append(data["prompt_message_id"])
    await state.clear()
    await _safe_delete_many(message.bot, message.chat.id, ids)


async def _send_prompt(message: Message, state: FSMContext, question: str) -> None:
    """Открыть диалог: съесть тап по кнопке входа (🔧 Ремонт и т.д.) и
    выложить первый экран."""
    await _consume(state, message)
    await _repost(message, state, _flow_screen(await state.get_state(), await state.get_data(), question))


async def _advance(message: Message, state: FSMContext, question: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Шаг вперёд после ответа сообщением: съесть ответ, выложить
    следующий экран вниз чата. Вызывать ПОСЛЕ set_state/update_data —
    шапка и сводка рисуются по тому, что в состоянии прямо сейчас."""
    await _consume(state, message)
    await _repost(message, state, _flow_screen(await state.get_state(), await state.get_data(), question), reply_markup)


async def _advance_callback(callback: CallbackQuery, state: FSMContext, question: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Шаг вперёд после нажатия инлайн-кнопки. Нажатие не добавляет в чат
    сообщения, значит экран диалога и так последний — правим на месте,
    без переотправки."""
    data = await state.get_data()
    text = _flow_screen(await state.get_state(), data, question)
    message_id = data.get("prompt_message_id") or callback.message.message_id
    await _safe_edit(callback.bot, callback.message.chat.id, message_id, text, reply_markup)
    await state.update_data(prompt_text=text, prompt_markup=reply_markup)


async def _nudge(message: Message, state: FSMContext, hint: str) -> None:
    """Некорректный ввод: переотправить ТОТ ЖЕ экран с предупреждением
    сверху. Вопрос обязан ехать вместе с ним, иначе у сотрудника остаётся
    претензия без единого намёка, о чём вообще спрашивали."""
    await _consume(state, message)
    data = await state.get_data()
    screen = data.get("prompt_text", "")
    await _repost(message, state, f"⚠️ {hint}\n\n{screen}", data.get("prompt_markup"), remember=screen)


# --- entry points — always available, even mid-flow (restarts fresh) ---

@router.message(F.text.in_({BTN_REPAIR, BTN_REPAIR_LEGACY}), F.chat.type == "private")
async def repair_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return  # not CRM staff — ignore silently, same as purchase_photo.py
    store, staff = resolved
    if staff["role"] not in _REPAIR_ROLES:
        await message.answer("Недостаточно прав для приёма ремонта.")
        return

    if message.text == BTN_REPAIR_LEGACY:
        # An old keyboard is still on this person's screen — replace it.
        await message.answer("Кнопки меню обновились — вот актуальные.", reply_markup=QUICK_ACTIONS_KEYBOARD)
    if not await _shift_gate(message, state, store, staff):
        return
    await _reset(message, state)
    await state.set_state(RepairIntake.photo)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "📷 Пришлите фото устройства:")


@router.message(F.text == BTN_CLIENT, F.chat.type == "private")
async def client_start(message: Message, state: FSMContext) -> None:
    """Not itself an FSM flow — a small menu (Найти / Добавить) whose
    buttons start ClientSearch/QuickContact. Same reason as cash_start:
    uses _repost (not a bare message.answer) so prompt_message_id is
    already tracked before either sub-flow's real first step (a tap on
    THIS menu, a callback — not a typed reply) needs it."""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved

    await _reset(message, state)
    await state.update_data(store_id=store.id)
    await _consume(state, message)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔍 Найти", callback_data="client_menu_search"),
        InlineKeyboardButton(text="➕ Добавить", callback_data="client_menu_add", style="success"),
    ]])
    await _repost(message, state, "👤 <b>Клиент</b>", keyboard)


async def _resolve_client_actor(callback: CallbackQuery, state: FSMContext):
    """Staff row for whoever tapped Найти/Добавить on the Клиент menu, or
    None (having already answered the callback) if the check fails. Any
    role passes — adding/searching clients isn't role-gated the way cash
    operations are (mirrors webapp.deps.require_staff, not
    _resolve_cash_actor's require_role-style check)."""
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return staff


@router.callback_query(F.data == "client_menu_add")
async def client_menu_add(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _resolve_client_actor(callback, state):
        return
    await state.set_state(QuickContact.name)
    await _advance_callback(callback, state, "Имя клиента:")
    await callback.answer()


@router.callback_query(F.data == "client_menu_search")
async def client_menu_search(callback: CallbackQuery, state: FSMContext) -> None:
    staff = await _resolve_client_actor(callback, state)
    if not staff:
        return
    await state.set_state(ClientSearch.query)
    await state.update_data(staff_id=staff["id"])
    await _advance_callback(callback, state, "Телефон или имя клиента:")
    await callback.answer()


@router.message(F.text == BTN_PURCHASE, F.chat.type == "private")
async def purchase_start(message: Message, state: FSMContext) -> None:
    """No FSM state of its own — just a discoverable prompt for an
    already-working, photo-triggered flow (bot/purchase_photo.py's
    photo_invoice handler already fires on any DM photo from receiving
    staff, OCRs it via OpenAI vision, and offers a draft to confirm — that
    handler already edits its own status message in place, Clean-Chat
    style, from "📷 Распознаю…" straight to the draft preview). state.clear()
    matters here: without it, a photo sent right after this while some
    OTHER flow's own .photo state was still active (forgot to cancel)
    would get grabbed by that flow's photo handler instead of ever
    reaching purchase_photo.py."""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _PURCHASE_ROLES:
        await message.answer("Недостаточно прав для приёма накладной.")
        return

    if not await _shift_gate(message, state, store, staff):
        return
    await _reset(message, state)
    await _consume(state, message)
    await message.answer("📦 Пришлите фото накладной — распознаю позиции и предложу оприходовать.")


@router.message(F.text == BTN_OUR_REPAIR, F.chat.type == "private")
async def our_repair_start(message: Message, state: FSMContext) -> None:
    """«Наш ремонт» / производство: a phone the business owns goes to a
    master with our parts and comes back worth more (core.production).
    The steps are checklists — parts across every склад, the master, the
    handover, his report — so they live on Mini App pages; this button is
    the way in, plus what is waiting on this person right now."""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    await _reset(message, state)
    await _consume(state, message)
    with get_conn(store.db_path) as conn:
        mine = core_production.list_orders(
            conn, master_id=staff["id"] if staff["role"] == "master" else None,
            statuses=("in_work",) if staff["role"] == "master" else ("draft", "in_work", "reported"),
        )
    rows = [[InlineKeyboardButton(
        text="🛠 Открыть наши ремонты",
        web_app=WebAppInfo(url=crm_link("/production", staff["id"], store.id)),
    )]]
    for order in mine[:6]:
        rows.append([InlineKeyboardButton(
            text=f"{core_documents.label('production', order['id'])} · {order['product_name']}"[:60],
            web_app=WebAppInfo(url=crm_link(f"/production/{order['id']}", staff["id"], store.id)),
        )])
    if staff["role"] == "master":
        text = f"🛠 <b>Наши ремонты</b>\nУ вас в работе: {len(mine)}." + (" Откройте заказ, чтобы сдать отчёт." if mine else "")
    else:
        text = f"🛠 <b>Наши ремонты и производство</b>\nВ работе и ждут решения: {len(mine)}."
    await _repost(message, state, text, InlineKeyboardMarkup(inline_keyboard=rows))


@router.message(F.text == BTN_SUMMARY, F.chat.type == "private")
async def summary_view(message: Message) -> None:
    """Read-only snapshot of the current store — the same numbers
    webapp/routers/dashboard.py and cash.py already compute for the Mini
    App, just reachable without opening it. Any staff role can see this
    (mirrors webapp.deps.require_staff, not require_role — it's totals
    only, no per-transaction detail, so it's not the privilege boundary
    the cash/reports pages themselves are)."""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved

    today = kyiv_today()
    utc_start, utc_end = kyiv_date_range_utc(today, today)
    with get_conn(store.db_path) as conn:
        location_id = store.location_id
        open_repairs = len([
            r for r in core_repairs.list_repairs(conn, location_id=location_id)
            if r["status"] not in ("issued", "cancelled")
        ])
        sales_today = [
            s for s in core_sales.list_sales(conn, limit=1000, location_id=location_id, include_cancelled=False)
            if utc_start <= s["created_at"] < utc_end
        ]
        sales_today_sum = sum(s["total"] for s in sales_today)
        cash_today = core_cash.period_summary(conn, utc_start, utc_end, location_id)
        cash_balance = core_cash.cash_balance(conn, location_id)
        low_stock = len(core_inventory.low_stock_report(conn, location_id))
        problem_lines = core_overview.problems_text(conn, location_id)
        # Прибыль и долги — the owner's/admin's numbers, as in the Mini App.
        money_lines = []
        if _staff["role"] in ("owner", "admin"):
            profit = core_overview.profit(conn, utc_start, utc_end, location_id)
            standing = core_overview.standing(conn, location_id)
            money_lines = [
                f"📈 Чистая прибыль сегодня: {profit['net']} грн",
                f"🤝 Нам должны: {standing['they_owe']} грн · мы должны: {money(standing['we_owe'] + standing['masters_owed'])} грн",
            ]

    lines = [
        f"📊 <b>Сводка — {html.escape(store.name)}</b>",
        "",
        f"🔧 Ремонтов в работе: {open_repairs}",
        f"🛒 Продаж сегодня: {len(sales_today)} шт · {sales_today_sum} грн",
        f"💵 Касса сегодня: +{cash_today['income_total']} / −{cash_today['expense_total']} грн",
        f"💰 Баланс в кассе: {cash_balance} грн",
        f"📉 Товаров с низким остатком: {low_stock}",
        *money_lines,
    ]
    if problem_lines:
        lines += ["", "⚠️ <b>Проблемы</b>", *problem_lines]
    await message.answer("\n".join(lines))


# --- Касса: расход / внести / снять (в грн, $ или USDT) и обмен, прямо из чата ---

# Валюты, которые предлагает Касса в боте. Деньги ложатся на счёт точки в
# этой валюте — наличные, если такой есть (у USDT это криптокошелёк).
_CASH_CURRENCIES = (("UAH", "🇺🇦 Гривна"), ("USD", "💵 Доллар"), ("USDT", "₮ USDT"))
# «💱 Обмен» в боте — гривна ↔ доллар; остальное — в приложении.
_EXCHANGE_DIRECTIONS = (("UAH", "USD"), ("USD", "UAH"))


def _currency_account(conn, location_id: int, currency: str):
    accounts = [a for a in core_accounts.list_accounts(conn, location_id) if a["currency"] == currency]
    cash = [a for a in accounts if a["kind"] == "cash"]
    return (cash or accounts or [None])[0]


def _last_rate(conn, currency: str):
    """Последний курс, по которому эта валюта ходила в кассе — подсказка
    кнопкой, чтобы не набирать один и тот же курс каждый раз."""
    row = conn.execute(
        """SELECT cash_transactions.rate FROM cash_transactions
           JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
           WHERE money_accounts.currency = ? AND cash_transactions.rate IS NOT NULL
             AND cash_transactions.cancelled_at IS NULL
           ORDER BY cash_transactions.id DESC LIMIT 1""",
        (currency,),
    ).fetchone()
    return money(row["rate"]) if row else None


def _currency_keyboard(store: StoreConfig) -> InlineKeyboardMarkup:
    with get_conn(store.db_path) as conn:
        offered = [(c, label) for c, label in _CASH_CURRENCIES if _currency_account(conn, store.location_id, c)]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"cash_cur_{c}") for c, label in offered
    ]])


@router.message(F.text == BTN_CASH, F.chat.type == "private")
async def cash_start(message: Message, state: FSMContext) -> None:
    """Not itself an FSM flow — a small menu (баланс + действия) whose
    buttons start CashExpense/CashAdjustment/CashExchange. Uses _repost
    (not a bare message.answer) so prompt_message_id is already tracked
    from this very first screen: each flow's real first step is a button
    tap on THIS menu (a callback), not a typed reply — unlike every other
    flow above, there's no earlier message-triggered _advance to have set
    prompt_message_id first."""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _CASH_ROLES:
        await message.answer("Недостаточно прав для операций с кассой.")
        return

    if not await _shift_gate(message, state, store, staff):
        return
    await _reset(message, state)
    await state.update_data(store_id=store.id)
    await _consume(state, message)
    with get_conn(store.db_path) as conn:
        balance = core_cash.cash_balance(conn, store.location_id)
        other_accounts = [
            a for a in core_accounts.balances(conn, store.location_id)
            if a["balance"] != 0 and not (a["kind"] == "cash" and a["currency"] == "UAH")
        ]
    rows = [[InlineKeyboardButton(text="➖ Расход", callback_data="cash_menu_expense")]]
    if staff["role"] in _ADJUST_ROLES:
        rows.append([
            InlineKeyboardButton(text="➕ Внести", callback_data="cash_menu_in", style="success"),
            InlineKeyboardButton(text="➖ Снять", callback_data="cash_menu_out"),
        ])
    rows.append([InlineKeyboardButton(text="💱 Обмен", callback_data="cash_menu_exchange")])
    rows.append([InlineKeyboardButton(text="🔒 Закрыть смену", callback_data="shift_close")])
    lines = ["💵 <b>Касса</b>", f"Баланс: {balance} грн"]
    lines += [f"{html.escape(a['name'])}: {a['balance']} {a['currency_label']}" for a in other_accounts]
    await _repost(message, state, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows))


async def _resolve_cash_actor(callback: CallbackQuery, state: FSMContext):
    """Staff row for whoever tapped a cash-menu button, re-checked here
    (not just once back at cash_start) — same reason repair/buyback/
    contact flows re-check role again at their own final confirm: a real,
    if rare, gap between "menu shown" and "button tapped" (role changed,
    store reconfigured). None (having already answered the callback) if
    the check fails."""
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff or staff["role"] not in _CASH_ROLES:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return staff


@router.callback_query(F.data == "cash_menu_expense")
async def cash_menu_expense(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _resolve_cash_actor(callback, state):
        return
    store = get_store((await state.get_data())["store_id"])
    await state.set_state(CashExpense.currency)
    await _advance_callback(callback, state, "В какой валюте расход?", _currency_keyboard(store))
    await callback.answer()


@router.callback_query(F.data == "shift_close")
async def shift_close(callback: CallbackQuery, state: FSMContext) -> None:
    actor = await _resolve_shift_actor(callback, state)
    if not actor:
        return
    store, staff = actor
    with get_conn(store.db_path) as conn:
        shift = core_shifts.current_shift(conn, staff["id"], store.location_id)
        if shift:
            core_shifts.close_shift(conn, shift["id"], staff["id"])
    await state.clear()
    await _safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        "🔒 Смена закрыта. Чтобы продолжить работу, откройте новую.",
    )
    await callback.answer("Смена закрыта")


def _may_adjust(staff) -> bool:
    return staff["role"] in _ADJUST_ROLES


async def _cash_menu_adjust(callback: CallbackQuery, state: FSMContext, direction: str) -> None:
    staff = await _resolve_cash_actor(callback, state)
    if not staff:
        return
    if not _may_adjust(staff):
        await callback.answer("Корректировать остаток может только владелец.", show_alert=True)
        return
    store = get_store((await state.get_data())["store_id"])
    await state.set_state(CashAdjustment.currency)
    await state.update_data(direction=direction)
    verb = "вносите" if direction == "in" else "снимаете"
    await _advance_callback(callback, state, f"В какой валюте {verb}?", _currency_keyboard(store))
    await callback.answer()


@router.callback_query(F.data == "cash_menu_in")
async def cash_menu_in(callback: CallbackQuery, state: FSMContext) -> None:
    await _cash_menu_adjust(callback, state, "in")


@router.callback_query(F.data == "cash_menu_out")
async def cash_menu_out(callback: CallbackQuery, state: FSMContext) -> None:
    await _cash_menu_adjust(callback, state, "out")


@router.callback_query(F.data.startswith("cash_cur_"), StateFilter(CashExpense.currency, CashAdjustment.currency))
async def cash_got_currency(callback: CallbackQuery, state: FSMContext) -> None:
    """Валюта → счёт этой валюты на точке; дальше сумма, в этой валюте."""
    currency = callback.data.removeprefix("cash_cur_")
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    with get_conn(store.db_path) as conn:
        account = _currency_account(conn, store.location_id, currency)
    if currency not in dict(_CASH_CURRENCIES) or not account:
        await callback.answer("Нет счёта в этой валюте.", show_alert=True)
        return
    data = await state.update_data(currency=currency, account_id=account["id"], account_name=account["name"])
    label = core_accounts.currency_label(currency)
    if await state.get_state() == CashExpense.currency.state:
        await state.set_state(CashExpense.amount)
        question = f"Сумма расхода ({label}):"
    else:
        await state.set_state(CashAdjustment.amount)
        question = f"Сумма для {'внесения' if data.get('direction') == 'in' else 'снятия'} ({label}):"
    await _advance_callback(callback, state, question)
    await callback.answer()


async def _ask_category(message_or_callback, state: FSMContext) -> None:
    await state.set_state(CashExpense.category)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"cash_cat_{key}")]
        for key, label in core_cash.EXPENSE_CATEGORIES.items()
    ])
    if isinstance(message_or_callback, CallbackQuery):
        await _advance_callback(message_or_callback, state, "Категория расхода:", keyboard)
    else:
        await _advance(message_or_callback, state, "Категория расхода:", reply_markup=keyboard)


@router.message(CashExpense.amount, F.text)
async def cash_expense_got_amount(message: Message, state: FSMContext) -> None:
    amount = core_accounts.parse_amount(message.text)
    if not amount:
        await _nudge(message, state, "Введите сумму числом, например: 250.")
        return
    data = await state.update_data(amount=amount)
    currency = data.get("currency", core_accounts.BASE_CURRENCY)
    if currency == core_accounts.BASE_CURRENCY:
        await _ask_category(message, state)
        return
    await state.set_state(CashExpense.rate)
    with get_conn(get_store(data["store_id"]).db_path) as conn:
        last = _last_rate(conn, currency)
    keyboard = None
    if last:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"По курсу {last}", callback_data=f"cash_rate_{last}"),
        ]])
    label = core_accounts.currency_label(currency)
    await _advance(message, state, f"Курс: сколько гривен за 1 {label}?", reply_markup=keyboard)


@router.message(CashExpense.rate, F.text)
async def cash_expense_got_rate(message: Message, state: FSMContext) -> None:
    rate = core_accounts.parse_amount(message.text)
    if not rate:
        await _nudge(message, state, "Введите курс числом, например: 41.5.")
        return
    await state.update_data(rate=rate)
    await _ask_category(message, state)


@router.callback_query(F.data.startswith("cash_rate_"), CashExpense.rate)
async def cash_expense_took_rate(callback: CallbackQuery, state: FSMContext) -> None:
    rate = core_accounts.parse_amount(callback.data.removeprefix("cash_rate_"))
    if not rate:
        await callback.answer()
        return
    await state.update_data(rate=rate)
    await _ask_category(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("cash_cat_"), CashExpense.category)
async def cash_expense_got_category(callback: CallbackQuery, state: FSMContext) -> None:
    category = callback.data.removeprefix("cash_cat_")
    if category not in core_cash.EXPENSE_CATEGORIES:
        await callback.answer()
        return
    await state.update_data(category=category)
    await state.set_state(CashExpense.comment)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без комментария ➡️", callback_data="cash_comment_skip"),
    ]])
    await _advance_callback(callback, state, "Комментарий (необязательно):", keyboard)
    await callback.answer()


def _in_uah(data: dict) -> str:
    """« (= 4 150 грн)» после суммы в валюте — сколько это в гривне по курсу."""
    if not data.get("rate"):
        return ""
    return f" (= {money(data['amount'] * data['rate'])} грн по курсу {data['rate']})"


def _cash_expense_confirm_text(data: dict) -> str:
    lines = [
        "📋 Расход:",
        f"Сумма: {data['amount']} {_cur(data)}{_in_uah(data)}",
        f"Категория: {core_cash.EXPENSE_CATEGORIES[data['category']]}",
    ]
    if data.get("account_name"):
        lines.append(f"Счёт: {html.escape(data['account_name'])}")
    if data.get("comment"):
        lines.append(f"Комментарий: {html.escape(data['comment'])}")
    return "\n".join(lines)


def _cash_expense_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="cash_expense_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cash_cancel", style="danger"),
    ]])


@router.message(CashExpense.comment, F.text)
async def cash_expense_got_comment(message: Message, state: FSMContext) -> None:
    data = await state.update_data(comment=message.text.strip() or None)
    await state.set_state(CashExpense.confirm)
    await _advance(message, state, _cash_expense_confirm_text(data), reply_markup=_cash_expense_confirm_keyboard())


@router.callback_query(F.data == "cash_comment_skip", CashExpense.comment)
async def cash_expense_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.update_data(comment=None)
    await state.set_state(CashExpense.confirm)
    await _advance_callback(callback, state, _cash_expense_confirm_text(data), _cash_expense_confirm_keyboard())
    await callback.answer()


@router.callback_query(F.data == "cash_expense_confirm", CashExpense.confirm)
async def cash_expense_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    if await _already_done(callback, store.db_path):
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _CASH_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        core_cash.record_expense(
            conn, "cash", data["amount"], data["category"], data.get("comment"), staff["id"],
            location_id=store.location_id, key=_tap_key(callback),
            account_id=data.get("account_id"), rate=data.get("rate"),
        )

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть в CRM", web_app=WebAppInfo(url=crm_link("/cash", staff["id"], store.id))),
    ]])
    await callback.message.edit_text(
        f"✅ Расход записан: {data['amount']} {_cur(data)} ({core_cash.EXPENSE_CATEGORIES[data['category']]}).",
        reply_markup=open_keyboard,
    )
    await callback.answer("Готово")


@router.message(CashAdjustment.amount, F.text)
async def cash_adjustment_got_amount(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    amount = core_accounts.parse_amount(message.text)
    if not amount:
        hint = "Введите сумму числом, например: 500." if data.get("direction") == "in" else "Введите сумму числом, например: 300."
        await _nudge(message, state, hint)
        return
    await state.update_data(amount=amount)
    await state.set_state(CashAdjustment.comment)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без комментария ➡️", callback_data="cash_comment_skip"),
    ]])
    await _advance(message, state, "Комментарий (необязательно):", reply_markup=keyboard)


def _cash_adjustment_confirm_text(data: dict) -> str:
    verb = "Внести" if data.get("direction") == "in" else "Снять"
    lines = [f"📋 {verb}:", f"Сумма: {data['amount']} {_cur(data)}"]
    if data.get("account_name"):
        lines.append(f"Счёт: {html.escape(data['account_name'])}")
    if data.get("comment"):
        lines.append(f"Комментарий: {html.escape(data['comment'])}")
    return "\n".join(lines)


def _cash_adjustment_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="cash_adjustment_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cash_cancel", style="danger"),
    ]])


@router.message(CashAdjustment.comment, F.text)
async def cash_adjustment_got_comment(message: Message, state: FSMContext) -> None:
    data = await state.update_data(comment=message.text.strip() or None)
    await state.set_state(CashAdjustment.confirm)
    await _advance(
        message, state, _cash_adjustment_confirm_text(data), reply_markup=_cash_adjustment_confirm_keyboard()
    )


@router.callback_query(F.data == "cash_comment_skip", CashAdjustment.comment)
async def cash_adjustment_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.update_data(comment=None)
    await state.set_state(CashAdjustment.confirm)
    await _advance_callback(
        callback, state, _cash_adjustment_confirm_text(data), _cash_adjustment_confirm_keyboard()
    )
    await callback.answer()


@router.callback_query(F.data == "cash_adjustment_confirm", CashAdjustment.confirm)
async def cash_adjustment_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return

    if await _already_done(callback, store.db_path):
        return
    signed_amount = data["amount"] if data.get("direction") == "in" else -data["amount"]
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _ADJUST_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        core_cash.record_adjustment(
            conn, signed_amount, data.get("comment"), staff["id"],
            location_id=store.location_id, key=_tap_key(callback), account_id=data.get("account_id"),
        )

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    verb = "Внесено" if signed_amount > 0 else "Снято"
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть в CRM", web_app=WebAppInfo(url=crm_link("/cash", staff["id"], store.id))),
    ]])
    await callback.message.edit_text(f"✅ {verb}: {data['amount']} {_cur(data)}.", reply_markup=open_keyboard)
    await callback.answer("Готово")


# --- 💱 Обмен: гривна ↔ доллар между счетами точки ---

@router.callback_query(F.data == "cash_menu_exchange")
async def cash_menu_exchange(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _resolve_cash_actor(callback, state):
        return
    await state.set_state(CashExchange.direction)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text=f"{core_accounts.currency_label(src)} → {core_accounts.currency_label(dst)}",
            callback_data=f"cash_ex_{src}_{dst}",
        )
        for src, dst in _EXCHANGE_DIRECTIONS
    ]])
    await _advance_callback(callback, state, "Что меняем?", keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("cash_ex_"), CashExchange.direction)
async def cash_exchange_got_direction(callback: CallbackQuery, state: FSMContext) -> None:
    src, _, dst = callback.data.removeprefix("cash_ex_").partition("_")
    if (src, dst) not in _EXCHANGE_DIRECTIONS:
        await callback.answer()
        return
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    with get_conn(store.db_path) as conn:
        source = _currency_account(conn, store.location_id, src)
        target = _currency_account(conn, store.location_id, dst)
        available = core_accounts.balance(conn, source["id"]) if source else 0
    if not source or not target:
        await callback.answer("На точке нет счёта в одной из валют — заведите его в CRM.", show_alert=True)
        return
    await state.update_data(
        from_currency=src, to_currency=dst, from_account_id=source["id"], to_account_id=target["id"],
        from_account_name=source["name"], to_account_name=target["name"],
    )
    await state.set_state(CashExchange.amount_from)
    label = core_accounts.currency_label(src)
    await _advance_callback(
        callback, state, f"Сколько {label} отдаёте? (на счёте «{html.escape(source['name'])}»: {available} {label})",
    )
    await callback.answer()


@router.message(CashExchange.amount_from, F.text)
async def cash_exchange_got_from(message: Message, state: FSMContext) -> None:
    amount = core_accounts.parse_amount(message.text)
    if not amount:
        await _nudge(message, state, "Введите сумму числом, например: 41500.")
        return
    data = await state.update_data(amount_from=amount)
    await state.set_state(CashExchange.amount_to)
    label = core_accounts.currency_label(data["to_currency"])
    await _advance(message, state, f"Сколько {label} получаете?")


def _exchange_rate(data: dict):
    """Гривен за доллар — с какой стороны ни стояла бы гривна."""
    if data["from_currency"] == core_accounts.BASE_CURRENCY:
        return money(data["amount_from"] / data["amount_to"])
    return money(data["amount_to"] / data["amount_from"])


def _cash_exchange_confirm_text(data: dict) -> str:
    src = core_accounts.currency_label(data["from_currency"])
    dst = core_accounts.currency_label(data["to_currency"])
    return "\n".join([
        "📋 Обмен:",
        f"Отдаём: −{data['amount_from']} {src} ({html.escape(data['from_account_name'])})",
        f"Получаем: +{data['amount_to']} {dst} ({html.escape(data['to_account_name'])})",
        f"Курс: {_exchange_rate(data)}",
    ])


@router.message(CashExchange.amount_to, F.text)
async def cash_exchange_got_to(message: Message, state: FSMContext) -> None:
    amount = core_accounts.parse_amount(message.text)
    if not amount:
        await _nudge(message, state, "Введите сумму числом, например: 1000.")
        return
    data = await state.update_data(amount_to=amount)
    await state.set_state(CashExchange.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Подтвердить", callback_data="cash_exchange_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="cash_cancel", style="danger"),
    ]])
    await _advance(message, state, _cash_exchange_confirm_text(data), reply_markup=keyboard)


@router.callback_query(F.data == "cash_exchange_confirm", CashExchange.confirm)
async def cash_exchange_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    if await _already_done(callback, store.db_path):
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _CASH_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        try:
            core_money_ops.exchange(
                conn, data["from_account_id"], data["to_account_id"], data["amount_from"], data["amount_to"],
                staff["id"], key=_tap_key(callback),
            )
        except core_accounts.AccountError as exc:
            # Карточка остаётся — можно отменить и начать с другой суммой.
            await callback.answer(str(exc), show_alert=True)
            return

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Открыть в CRM", web_app=WebAppInfo(url=crm_link("/cash", staff["id"], store.id))),
    ]])
    src = core_accounts.currency_label(data["from_currency"])
    dst = core_accounts.currency_label(data["to_currency"])
    await callback.message.edit_text(
        f"✅ Обмен проведён: −{data['amount_from']} {src} → +{data['amount_to']} {dst}, курс {_exchange_rate(data)}.",
        reply_markup=open_keyboard,
    )
    await callback.answer("Готово")


# --- Клиент: поиск по телефону/имени прямо из чата ---

_CLIENT_SEARCH_LIMIT = 8


async def _client_card(store: StoreConfig, client_id: int, staff_id: int) -> tuple[str, InlineKeyboardMarkup]:
    """Same shape as webapp/routers/clients.py's detail_view, condensed to
    what fits a chat message — counts instead of the full history list."""
    with get_conn(store.db_path) as conn:
        client = core_clients.get_client(conn, client_id)
        repairs_count = len(core_repairs.list_repairs_by_client(conn, client_id))
        sales_count = len(core_sales.list_sales_by_client(conn, client_id))
        position = core_settlements.client_position(conn, client_id)

    lines = [f"👤 <b>{html.escape(client['name'])}</b>"]
    if client["phone"]:
        lines.append(f"📞 {html.escape(client['phone'])}")
    lines.append(f"🔧 Ремонтов: {repairs_count}")
    lines.append(f"🛒 Продаж: {sales_count}")
    # Взаиморасчёты — the first thing to know before selling or paying out.
    if position["they_owe"]:
        lines.append(f"💰 <b>Нам должны: {position['they_owe']} грн</b>")
    elif position["we_owe"]:
        lines.append(f"💰 <b>Мы должны: {position['we_owe']} грн</b>")
    else:
        lines.append("💰 Взаиморасчёты: 0")
    if client["notes"]:
        lines.append(f"📝 {html.escape(client['notes'])}")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="📋 Карточка: история, деньги, сверка",
            web_app=WebAppInfo(url=crm_link(f"/clients/{client_id}", staff_id, store.id)),
        )],
        [InlineKeyboardButton(
            text="🧾 Сверка за период",
            web_app=WebAppInfo(url=crm_link(f"/clients/{client_id}/statement", staff_id, store.id)),
        )],
    ])
    return "\n".join(lines), keyboard


@router.message(ClientSearch.query, F.text)
async def client_search_got_query(message: Message, state: FSMContext) -> None:
    query = message.text.strip()
    if not query:
        await _nudge(message, state, "Введите телефон или имя.")
        return

    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await _consume(state, message)
        await state.clear()
        await message.answer("Магазин больше не настроен.")
        return

    # Ищем и по номеру, и по имени одним запросом (core.clients.list_clients
    # LIKE'ит оба поля) — но телефон, введённый в местном/без-плюса формате
    # ("0501234567"), не совпал бы по LIKE с сохранённым каноничным
    # "+380501234567" без предварительной нормализации; чистый текст (или
    # обрывок цифр короче полного номера) normalize_phone честно вернёт ""
    # для, и тогда ищем как есть — тем же путём находятся и куски номера.
    term = core_clients.normalize_phone(query) or query
    with get_conn(store.db_path) as conn:
        rows = core_clients.list_clients(conn, search=term)

    if not rows:
        await _nudge(message, state, f"Ничего не найдено по «{html.escape(query)}». Попробуйте ещё раз.")
        return

    if len(rows) == 1:
        # _repost BEFORE state.clear(): it reads prompt_message_id out of
        # the FSM data to delete the previous screen — clearing first
        # would leave that screen (the question, or a "не найдено" nudge)
        # orphaned in the chat instead of replaced.
        text, keyboard = await _client_card(store, rows[0]["id"], data["staff_id"])
        await _consume(state, message)
        await _repost(message, state, text, keyboard)
        await state.clear()
        return

    shown = rows[:_CLIENT_SEARCH_LIMIT]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=c["name"] + (f" · {c['phone']}" if c["phone"] else ""),
            callback_data=f"client_pick:{c['id']}",
        )]
        for c in shown
    ])
    suffix = (
        f"\n\nПоказаны первые {len(shown)} из {len(rows)} — уточните запрос для точного результата."
        if len(rows) > len(shown) else ""
    )
    await _advance(message, state, f"Нашлось {len(rows)} — выберите:{suffix}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("client_pick:"), ClientSearch.query)
async def client_search_pick(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return

    client_id = int(callback.data.split(":", 1)[1])
    text, keyboard = await _client_card(store, client_id, data["staff_id"])
    await state.clear()
    await _safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, text, keyboard)
    await callback.answer()


@router.message(F.text == BTN_CANCEL, StateFilter(RepairIntake, QuickContact, PhoneBuy, CashExpense, CashAdjustment, CashExchange, ClientSearch, ShiftOpen, TransferFlow, SaleFlow, PartAdd))
async def cancel_flow(message: Message, state: FSMContext) -> None:
    """Deletes EVERYTHING from this flow attempt — the bot's own tracked
    message, every reply the staff member typed along the way (name,
    phone, ...), the tap on the entry button (🔧 Ремонт etc.), and this
    very "❌ Отмена" tap itself. Telegram's Bot API explicitly allows a
    bot to delete incoming messages in a private chat, not just its own,
    so nothing has to survive a cancel."""
    data = await state.get_data()
    ids = list(data.get("user_message_ids", []))
    ids.append(message.message_id)
    prompt_message_id = data.get("prompt_message_id")
    if prompt_message_id:
        ids.append(prompt_message_id)
    await state.clear()
    await _safe_delete_many(message.bot, message.chat.id, ids)


@router.message(F.text == BTN_CANCEL, F.chat.type == "private")
async def cancel_noop(message: Message) -> None:
    """❌ Отмена tapped with nothing active — cancel_flow above (state-
    gated) doesn't match, so this would otherwise sit in the chat
    un-acted-on forever. Nothing to clean up but the tap itself."""
    await _safe_delete_many(message.bot, message.chat.id, [message.message_id])


# --- Ремонт: step by step ---

@router.message(RepairIntake.photo, F.photo)
async def repair_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    await state.update_data(photo_bytes=buf.read())
    await state.set_state(RepairIntake.phone)
    await _advance(message, state, "Телефон клиента:")


@router.message(RepairIntake.photo)
async def repair_photo_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Пришлите фото устройства (как фото, не файлом).")


_MODEL_QUESTION = "Модель устройства (например: iPhone 12):"


@router.message(RepairIntake.phone, F.text)
async def repair_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await _nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    data = await state.update_data(client_phone=phone)
    # A returning client is already on record under this number — no
    # point asking their name again; straight on to the device.
    known_name = None
    try:
        store = get_store(data["store_id"])
        with get_conn(store.db_path) as conn:
            known = core_clients.get_by_phone(conn, phone)
        known_name = known["name"] if known and known["name"] != phone else None
    except KeyError:
        pass
    if known_name:
        await state.update_data(client_name=known_name)
        await state.set_state(RepairIntake.model)
        await _advance(message, state, _MODEL_QUESTION)
        return
    await state.set_state(RepairIntake.name)
    await _advance(message, state, "Имя клиента:")


@router.message(RepairIntake.name, F.text)
async def repair_got_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await _nudge(message, state, "Имя не может быть пустым.")
        return
    await state.update_data(client_name=name)
    await state.set_state(RepairIntake.model)
    await _advance(message, state, _MODEL_QUESTION)


@router.message(RepairIntake.model, F.text)
async def repair_got_model(message: Message, state: FSMContext) -> None:
    model = message.text.strip()
    if not model:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(model=model)
    await state.set_state(RepairIntake.defect)
    await _advance(message, state, "Неисправность (что случилось с устройством):")


@router.message(RepairIntake.defect, F.text)
async def repair_got_defect(message: Message, state: FSMContext) -> None:
    defect = message.text.strip()
    if not defect:
        await _nudge(message, state, "Опишите неисправность.")
        return
    await state.update_data(defect_description=defect)
    await state.set_state(RepairIntake.price)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Пока не знаю ➡️", callback_data="repair_price_skip"),
    ]])
    await _advance(message, state, "Оценочная стоимость ремонта (грн):", reply_markup=keyboard)


_PASSWORD_QUESTION = "Пароль от устройства:"


def _password_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без пароля ➡️", callback_data="repair_password_skip"),
    ]])


@router.message(RepairIntake.price, F.text)
async def repair_got_price(message: Message, state: FSMContext) -> None:
    price = _parse_positive_int(message.text)
    if not price:
        await _nudge(message, state, "Введите сумму числом, например: 500 — либо «Пока не знаю».")
        return
    await state.update_data(price_estimate=price)
    await state.set_state(RepairIntake.password)
    await _advance(message, state, _PASSWORD_QUESTION, reply_markup=_password_keyboard())


@router.callback_query(F.data == "repair_price_skip", RepairIntake.price)
async def repair_skip_price(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(price_estimate=None)
    await state.set_state(RepairIntake.password)
    await _advance_callback(callback, state, _PASSWORD_QUESTION, reply_markup=_password_keyboard())
    await callback.answer()


_COMMENT_QUESTION = "Комментарий к заказу:"


def _comment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Без комментария ➡️", callback_data="repair_comment_skip"),
    ]])


@router.message(RepairIntake.password, F.text)
async def repair_got_password(message: Message, state: FSMContext) -> None:
    await state.update_data(device_password=message.text.strip() or None)
    await state.set_state(RepairIntake.comment)
    await _advance(message, state, _COMMENT_QUESTION, reply_markup=_comment_keyboard())


@router.callback_query(F.data == "repair_password_skip", RepairIntake.password)
async def repair_skip_password(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(device_password=None)
    await state.set_state(RepairIntake.comment)
    await _advance_callback(callback, state, _COMMENT_QUESTION, reply_markup=_comment_keyboard())
    await callback.answer()


def _store_masters(store) -> list:
    """Masters a repair of this точка may go to: its own and the ones not
    tied to any точка — another shop's masters are not offered."""
    with get_conn(store.db_path) as conn:
        masters = core_auth.list_masters(conn)
    return [m for m in masters if m["location_id"] in (None, store.location_id)]


def _master_step_keyboard(data: dict) -> InlineKeyboardMarkup | None:
    try:
        store = get_store(data["store_id"])
    except KeyError:
        return None
    rows = [[InlineKeyboardButton(text=m["name"][:40], callback_data=f"rm_pick:{m['id']}")] for m in _store_masters(store)]
    rows.append([InlineKeyboardButton(text="Не назначать ➡️", callback_data="rm_pick:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(RepairIntake.comment, F.text)
async def repair_got_comment(message: Message, state: FSMContext) -> None:
    data = await state.update_data(intake_comment=message.text.strip() or None)
    await state.set_state(RepairIntake.master)
    await _advance(message, state, "Выберите мастера:", reply_markup=_master_step_keyboard(data))


@router.callback_query(F.data == "repair_comment_skip", RepairIntake.comment)
async def repair_skip_comment(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.update_data(intake_comment=None)
    await state.set_state(RepairIntake.master)
    await _advance_callback(callback, state, "Выберите мастера:", reply_markup=_master_step_keyboard(data))
    await callback.answer()


@router.callback_query(F.data.startswith("rm_pick:"), RepairIntake.master)
async def repair_master_pick(callback: CallbackQuery, state: FSMContext) -> None:
    """The last question: the master. After it — the «Проверьте данные»
    card, where the choice can still be changed («👤 Мастер»)."""
    data = await state.get_data()
    master_id = int(callback.data.split(":", 1)[1])
    master_name = None
    if master_id:
        try:
            store = get_store(data["store_id"])
        except KeyError:
            await callback.answer("Магазин больше не настроен.", show_alert=True)
            return
        with get_conn(store.db_path) as conn:
            master = core_auth.get_master(conn, master_id)
        if not master or not master["active"]:
            await callback.answer("Этого мастера выбрать нельзя.", show_alert=True)
            return
        master_name = master["name"]
    data = await state.update_data(master_id=master_id or None, master_name=master_name)
    await _send_repair_confirm(callback.message, state, data, consume=False)
    await callback.answer()


def _repair_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать ремонт", callback_data="quick_repair_confirm", style="success")],
        [
            InlineKeyboardButton(text="👤 Мастер", callback_data="rm_menu"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="quick_repair_cancel", style="danger"),
        ],
    ])


async def _send_repair_confirm(message: Message, state: FSMContext, data: dict, consume: bool = True) -> None:
    """The «Проверьте данные» card. Not _advance: it is a PHOTO message
    (Павел wants the actual device photo on the card, not a text line
    saying it exists), and _repost/_advance only ever send plain text.
    Same Clean-Chat shape by hand instead — consume the reply, send the
    new screen, delete the previous one. `consume=False` when it is
    reached by a button (the master step) — `message` is then the bot's
    own screen, not a reply of the employee's to clear away."""
    await state.set_state(RepairIntake.confirm)
    keyboard = _repair_confirm_keyboard()
    caption = _repair_confirm_caption(data)
    if consume:
        await _consume(state, message)
    previous_id = data.get("prompt_message_id")
    photo = BufferedInputFile(data["photo_bytes"], filename="device.jpg")
    sent = await message.answer_photo(photo, caption=caption, reply_markup=keyboard)
    await state.update_data(prompt_message_id=sent.message_id, prompt_text=caption, prompt_markup=keyboard)
    if previous_id:
        await _safe_delete_many(message.bot, message.chat.id, [previous_id])


def _repair_confirm_caption(data: dict) -> str:
    """Full recap as a PHOTO caption (Павел, 18.09): the confirm screen
    must actually show the device photo, not just say "фото приложено" —
    photo is collected first now, so it's already in hand by this point.
    Telegram caps a photo caption at 1024 chars (core.notify._as_caption
    hits the same limit on the group card) — a free-typed defect
    description has no length limit on its own end, so this stays a
    safety net, not the expected case."""
    price_line = f"Оценка: {data['price_estimate']} грн" if data.get("price_estimate") else "Оценка: пока не известна"
    lines = [
        "📋 Проверьте данные:",
        "",
        f"Телефон: {html.escape(data['client_phone'])}",
        f"Имя: {html.escape(data['client_name'])}",
        f"Модель: {html.escape(data['model'])}",
        f"Неисправность: {html.escape(data['defect_description'])}",
        price_line,
        f"Пароль: {html.escape(data['device_password']) if data.get('device_password') else 'нет'}",
    ]
    if data.get("intake_comment"):
        lines.append(f"Комментарий: {html.escape(data['intake_comment'])}")
    lines.append(f"Мастер: {html.escape(data['master_name']) if data.get('master_name') else 'не назначен'}")
    text = "\n".join(lines)
    return text if len(text) <= 1024 else text[:1023] + "…"


# «👤 Мастер» on the card: the buttons under the photo turn into the list
# of masters, a pick turns them back and rewrites the «Мастер:» line. Done
# on the card itself rather than as one more question in the dialog —
# most repairs are taken in with no master chosen yet (whoever is free
# presses «Взять в работу» in the group).

@router.callback_query(F.data == "rm_menu", RepairIntake.confirm)
async def repair_master_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    rows = [[InlineKeyboardButton(text=m["name"][:40], callback_data=f"rm_set:{m['id']}")] for m in _store_masters(store)]
    rows.append([InlineKeyboardButton(text="Не назначать", callback_data="rm_set:0")])
    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await callback.answer()


@router.callback_query(F.data.startswith("rm_set:"), RepairIntake.confirm)
async def repair_master_set(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    master_id = int(callback.data.split(":", 1)[1])
    master_name = None
    if master_id:
        try:
            store = get_store(data["store_id"])
        except KeyError:
            await callback.answer("Магазин больше не настроен.", show_alert=True)
            return
        with get_conn(store.db_path) as conn:
            master = core_auth.get_master(conn, master_id)
        if not master or not master["active"]:
            await callback.answer("Этого мастера выбрать нельзя.", show_alert=True)
            return
        master_name = master["name"]
    data = await state.update_data(master_id=master_id or None, master_name=master_name)
    caption, keyboard = _repair_confirm_caption(data), _repair_confirm_keyboard()
    await callback.message.edit_caption(caption=caption, reply_markup=keyboard)
    await state.update_data(prompt_text=caption, prompt_markup=keyboard)
    await callback.answer()


@router.message(RepairIntake.confirm)
async def repair_confirm_fallback(message: Message, state: FSMContext) -> None:
    """Stray message while the photo confirm card is showing. Unlike
    _nudge (which _reposts as plain text — quick_flow_fallback's normal
    path for every other flow's confirm step), this card carries the
    device photo, so a text-only repost would silently lose it. Just
    delete the stray reply and point back at the card instead — tracked
    in user_message_ids (not prompt_message_id: the card itself stays
    "current") so this reminder gets swept on confirm/cancel/abandon same
    as any other clutter, instead of lingering as a second permanent
    message once the flow actually finishes."""
    await _consume(state, message)
    sent = await message.answer("Нажмите «✅ Принять» или «❌ Отмена» на карточке выше.")
    await _track(state, sent)


@router.callback_query(F.data == "quick_repair_confirm", RepairIntake.confirm)
async def repair_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return
    if await _already_done(callback, store.db_path):
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _REPAIR_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        order_id, card_text, keyboard, photo_for_notify = core_repairs.create_repair_intake(
            conn,
            client_name=data["client_name"], client_phone=data["client_phone"],
            device_type="", brand=None, model=data["model"],
            serial_number=None, defect_description=data["defect_description"],
            channel="offline", master_id=data.get("master_id"), price_estimate=data.get("price_estimate"),
            staff_id=staff["id"], photo=(data["photo_bytes"], ".jpg"),
            location_id=store.location_id, key=_tap_key(callback),
            device_password=data.get("device_password"), intake_comment=data.get("intake_comment"),
        )

    # asyncio.to_thread: core.notify does blocking httpx calls to Telegram
    # (up to a few seconds with a photo attached) — awaited directly this
    # would freeze the whole bot (every user's messages) for that long.
    await asyncio.to_thread(core_repairs.notify_and_save, store, order_id, card_text, keyboard, photo_for_notify)

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    # Clean Chat everywhere else means "delete the trail, keep only the
    # final screen" — but this card (photo + full details) stays exactly
    # as approved (Павел, 18.09: "карточка должна остаться, а не
    # удалиться"), not collapse into a short "принято" line. Trail still
    # goes; just strip the buttons (edit_reply_markup — works on a photo
    # message, unlike edit_text/edit_caption's own separate methods) so a
    # second tap can't double-submit, and post the CRM link separately.
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    await callback.message.edit_reply_markup(reply_markup=None)

    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Открыть в CRM",
            web_app=WebAppInfo(url=crm_link(f"/repairs/{order_id}", staff["id"], store.id)),
        ),
    ]])
    await callback.message.answer(
        f"✅ {core_documents.label('repair', order_id)} создан.\n\nЕсли нужно уточнить цену или мастера — карточка ремонта:",
        reply_markup=open_keyboard,
    )
    await callback.answer("Готово")


def _parse_positive_int(text: str) -> int | None:
    text = text.strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


# --- Контакт: step by step ---

@router.message(QuickContact.name, F.text)
async def contact_got_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await _nudge(message, state, "Имя не может быть пустым.")
        return
    await state.update_data(name=name)
    await state.set_state(QuickContact.phone)
    await _advance(message, state, "Номер телефона клиента:")


@router.message(QuickContact.phone, F.text)
async def contact_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await _nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    data = await state.update_data(phone=phone)
    await state.set_state(QuickContact.confirm)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Добавить", callback_data="quick_contact_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_contact_cancel", style="danger"),
    ]])
    await _advance(message, state, f"📋 Проверьте:\nИмя: {html.escape(data['name'])}\nТелефон: {html.escape(phone)}", reply_markup=keyboard)


@router.callback_query(F.data == "quick_contact_confirm", QuickContact.confirm)
async def contact_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        client_id = core_clients.get_or_create_by_phone(conn, data["name"], data["phone"], source="offline")

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Открыть в CRM",
            web_app=WebAppInfo(url=crm_link(f"/clients/{client_id}", staff["id"], store.id)),
        ),
    ]])
    await callback.message.edit_text(f"✅ Клиент добавлен (№{client_id}).", reply_markup=open_keyboard)
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"quick_repair_cancel", "quick_contact_cancel", "buy_cancel", "cash_cancel", "tr_cancel", "sl_cancel"}))
async def quick_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    """Inline ❌ Отмена on the confirm card — same full cleanup as
    cancel_flow, minus the entry-tap-of-cancel (a button tap doesn't
    create a chat message the way a reply-keyboard tap does, so there's
    nothing extra to add here beyond what was already tracked)."""
    data = await state.get_data()
    ids = list(data.get("user_message_ids", []))
    ids.append(callback.message.message_id)
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, ids)
    await callback.answer("Отменено")


# --- catch-all: anything unexpected mid-flow (wrong content type, a stray
# message while waiting on inline-button confirm) gets a nudge instead of
# silence ---

@router.message(StateFilter(RepairIntake, QuickContact, PhoneBuy, CashExpense, CashAdjustment, CashExchange, ClientSearch, ShiftOpen, TransferFlow, SaleFlow, PartAdd))
async def quick_flow_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Не понял ответ. Следуйте подсказке выше, либо ❌ Отмена.")
