"""Быстрый приём — reply-клавиатура (Ремонт/Контакт/Скупка/Приход) для
DM с ботом, ведущая сотрудника по шагам вместо открытия Mini App. Тот же
набор обязательных полей, что и в веб-форме приёма (webapp/routers/
repairs.py) — оба места создают ремонт через одну общую функцию,
core.repairs.create_repair_intake, так что поведение не может разъехаться.

Диалог держится в aiogram FSM (MemoryStorage — состояние живёт в памяти
процесса бота; если сотрудник бросит диалог на середине, в БД ничего не
запишется, следующий /start или повторный тап на кнопку начнёт всё
заново без остатков).

Clean Chat (манифест §2): каждый диалог — ОДНО сообщение бота, которое
редактируется на каждом шаге (_send_prompt/_advance/_nudge ниже), а не
плодит новое сообщение на каждый вопрос. Telegram не даёт боту удалять
чужие (пользовательские) сообщения в личке — это ограничение самого Bot
API, не наше решение, так что ответы сотрудника (имя, телефон и т.д.)
по-прежнему остаются в чате; редактируется только собственная сторона
бота."""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from bot.config import MINIAPP_URL
from core import auth as core_auth
from core import buyback as core_buyback
from core import clients as core_clients
from core import repairs as core_repairs
from core import store_access
from core.storage import get_conn
from core.stores import StoreConfig, get_store

router = Router()

# Mirrors webapp.routers.repairs._REPAIR_WRITE_ROLES — kept as its own
# copy (same convention as bot/repair_attachments.py's _REPAIR_ROLES)
# rather than importing from webapp, which would pull FastAPI-only code
# into the bot process for no reason.
_REPAIR_ROLES = ("owner", "admin", "master")
# Mirrors webapp.routers.buyback._BUYBACK_ROLES.
_BUYBACK_ROLES = ("owner", "admin", "storekeeper")
# Mirrors bot.purchase_photo._DRAFT_ROLES.
_PURCHASE_ROLES = ("owner", "admin", "storekeeper")

BTN_REPAIR = "🔧 Ремонт"
BTN_CONTACT = "👤 Контакт"
BTN_BUYBACK = "💰 Скупка"
BTN_PURCHASE = "📦 Приход"
BTN_CANCEL = "❌ Отмена"

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
        [KeyboardButton(text=BTN_REPAIR), KeyboardButton(text=BTN_CONTACT)],
        [KeyboardButton(text=BTN_BUYBACK), KeyboardButton(text=BTN_PURCHASE)],
        [KeyboardButton(text=BTN_CANCEL)],
    ],
    resize_keyboard=True,
    is_persistent=True,
)


class RepairIntake(StatesGroup):
    name = State()
    phone = State()
    device_type = State()
    model = State()
    defect = State()
    photo = State()
    confirm = State()


class QuickContact(StatesGroup):
    name = State()
    phone = State()
    confirm = State()


class BuybackIntake(StatesGroup):
    name = State()
    phone = State()
    device_type = State()
    model = State()
    price = State()
    payment_method = State()
    purpose = State()
    resale_price = State()
    photo = State()
    confirm = State()


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


# --- Clean Chat helpers: one tracked message per flow, edited in place ---

async def _safe_edit(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except TelegramBadRequest:
        pass  # already edited/deleted (e.g. a duplicate/late update) — never worth crashing the handler over


async def _safe_delete(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        pass  # already gone, or too old — bots can only delete their OWN
              # messages in a DM anyway (Bot API limit, not our choice),
              # so this only ever touches the tracked flow message itself


async def _send_prompt(message: Message, state: FSMContext, text: str) -> None:
    """Start (or restart) a flow's one tracked message — every later step
    edits THIS message (see _advance/_nudge) instead of sending a new
    one."""
    sent = await message.answer(text)
    await state.update_data(prompt_message_id=sent.message_id, prompt_text=text)


async def _advance(message: Message, state: FSMContext, text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    """Move the flow forward: edit the tracked message into the next step."""
    data = await state.get_data()
    await _safe_edit(message.bot, message.chat.id, data["prompt_message_id"], text, reply_markup)
    await state.update_data(prompt_text=text)


async def _nudge(message: Message, state: FSMContext, hint: str) -> None:
    """Invalid input mid-flow: show the hint above the still-current
    question (not instead of it — a fresh message.answer would both lose
    the question and pile up, exactly what this whole pattern avoids)."""
    data = await state.get_data()
    await _safe_edit(message.bot, message.chat.id, data["prompt_message_id"], f"⚠️ {hint}\n\n{data.get('prompt_text', '')}")


# --- entry points — always available, even mid-flow (restarts fresh) ---

@router.message(F.text == BTN_REPAIR, F.chat.type == "private")
async def repair_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return  # not CRM staff — ignore silently, same as purchase_photo.py
    store, staff = resolved
    if staff["role"] not in _REPAIR_ROLES:
        await message.answer("Недостаточно прав для приёма ремонта.")
        return

    await state.clear()
    await state.set_state(RepairIntake.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "🔧 Быстрый приём ремонта.\n\nИмя клиента:")


@router.message(F.text == BTN_CONTACT, F.chat.type == "private")
async def contact_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved

    await state.clear()
    await state.set_state(QuickContact.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "👤 Быстрое добавление клиента.\n\nИмя клиента:")


@router.message(F.text == BTN_BUYBACK, F.chat.type == "private")
async def buyback_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _BUYBACK_ROLES:
        await message.answer("Недостаточно прав для скупки.")
        return

    await state.clear()
    await state.set_state(BuybackIntake.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "💰 Быстрая скупка техники.\n\nИмя клиента (продавца):")


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
    _store, staff = resolved
    if staff["role"] not in _PURCHASE_ROLES:
        await message.answer("Недостаточно прав для приёма накладной.")
        return

    await state.clear()
    await message.answer("📦 Пришлите фото накладной — распознаю позиции и предложу оприходовать.")


@router.message(F.text == BTN_CANCEL, StateFilter(RepairIntake, QuickContact, BuybackIntake))
async def cancel_flow(message: Message, state: FSMContext) -> None:
    """Deletes the flow's own tracked message entirely rather than
    editing it to "Отменено." — Павел asked for nothing left behind from
    the moment a flow started. The one thing this can't reach is the
    staff member's OWN typed replies (name, phone, ...) still sitting in
    the chat — Bot API only lets a bot delete messages it sent itself in
    a DM, not the other side's; that's a Telegram-wide limit, not a
    choice made here."""
    data = await state.get_data()
    prompt_message_id = data.get("prompt_message_id")
    await state.clear()
    if prompt_message_id:
        await _safe_delete(message.bot, message.chat.id, prompt_message_id)


# --- Ремонт: step by step ---

@router.message(RepairIntake.name, F.text)
async def repair_got_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await _nudge(message, state, "Имя не может быть пустым.")
        return
    await state.update_data(client_name=name)
    await state.set_state(RepairIntake.phone)
    await _advance(message, state, "Телефон клиента:")


@router.message(RepairIntake.phone, F.text)
async def repair_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await _nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    await state.update_data(client_phone=phone)
    await state.set_state(RepairIntake.device_type)
    await _advance(message, state, "Тип устройства (например: Смартфон, Ноутбук, Планшет):")


@router.message(RepairIntake.device_type, F.text)
async def repair_got_device_type(message: Message, state: FSMContext) -> None:
    device_type = message.text.strip()
    if not device_type:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(device_type=device_type)
    await state.set_state(RepairIntake.model)
    await _advance(message, state, "Модель устройства:")


@router.message(RepairIntake.model, F.text)
async def repair_got_model(message: Message, state: FSMContext) -> None:
    model = message.text.strip()
    if not model:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(model=model)
    await state.set_state(RepairIntake.defect)
    await _advance(message, state, "Опишите неисправность:")


@router.message(RepairIntake.defect, F.text)
async def repair_got_defect(message: Message, state: FSMContext) -> None:
    defect = message.text.strip()
    if not defect:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(defect_description=defect)
    await state.set_state(RepairIntake.photo)
    await _advance(message, state, "📷 Пришлите фото устройства:")


@router.message(RepairIntake.photo, F.photo)
async def repair_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    await state.update_data(photo_bytes=buf.read())
    await state.set_state(RepairIntake.confirm)

    data = await state.get_data()
    text = (
        "📋 Проверьте данные:\n\n"
        f"Клиент: {data['client_name']}\n"
        f"Телефон: {data['client_phone']}\n"
        f"Устройство: {data['device_type']} {data['model']}\n"
        f"Неисправность: {data['defect_description']}\n"
        "Фото: приложено ✅"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data="quick_repair_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_repair_cancel"),
    ]])
    await _advance(message, state, text, reply_markup=keyboard)


@router.message(RepairIntake.photo)
async def repair_photo_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Пришлите фото устройства (как фото, не файлом).")


@router.callback_query(F.data == "quick_repair_confirm", RepairIntake.confirm)
async def repair_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
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
            device_type=data["device_type"], brand=None, model=data["model"],
            serial_number=None, defect_description=data["defect_description"],
            channel="offline", master_id=None, price_estimate=None,
            staff_id=staff["id"], photo=(data["photo_bytes"], ".jpg"),
        )

    # asyncio.to_thread: core.notify does blocking httpx calls to Telegram
    # (up to a few seconds with a photo attached) — awaited directly this
    # would freeze the whole bot (every user's messages) for that long.
    await asyncio.to_thread(core_repairs.notify_and_save, store, order_id, card_text, keyboard, photo_for_notify)

    await state.clear()
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Открыть в CRM", web_app=WebAppInfo(url=f"{MINIAPP_URL.rstrip('/')}/repairs/{order_id}"),
        ),
    ]])
    await callback.message.edit_text(
        f"✅ Ремонт №{order_id} принят.\n\nЕсли нужно уточнить цену или назначить мастера — карточка ремонта:",
        reply_markup=open_keyboard,
    )
    await callback.answer("Готово")


# --- Скупка: step by step ---

@router.message(BuybackIntake.name, F.text)
async def buyback_got_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name:
        await _nudge(message, state, "Имя не может быть пустым.")
        return
    await state.update_data(client_name=name)
    await state.set_state(BuybackIntake.phone)
    await _advance(message, state, "Телефон клиента:")


@router.message(BuybackIntake.phone, F.text)
async def buyback_got_phone(message: Message, state: FSMContext) -> None:
    phone = core_clients.normalize_phone(message.text.strip())
    if not phone:
        await _nudge(message, state, "Не похоже на номер телефона. Например: 0501234567.")
        return
    await state.update_data(client_phone=phone)
    await state.set_state(BuybackIntake.device_type)
    await _advance(message, state, "Тип устройства (например: Смартфон, Ноутбук, Планшет):")


@router.message(BuybackIntake.device_type, F.text)
async def buyback_got_device_type(message: Message, state: FSMContext) -> None:
    device_type = message.text.strip()
    if not device_type:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(device_type=device_type)
    await state.set_state(BuybackIntake.model)
    await _advance(message, state, "Модель устройства:")


@router.message(BuybackIntake.model, F.text)
async def buyback_got_model(message: Message, state: FSMContext) -> None:
    model = message.text.strip()
    if not model:
        await _nudge(message, state, "Не может быть пустым.")
        return
    await state.update_data(model=model)
    await state.set_state(BuybackIntake.price)
    await _advance(message, state, "Сумма, которую платим клиенту (грн):")


def _parse_positive_int(text: str) -> int | None:
    text = text.strip()
    if not text.isdigit():
        return None
    value = int(text)
    return value if value > 0 else None


@router.message(BuybackIntake.price, F.text)
async def buyback_got_price(message: Message, state: FSMContext) -> None:
    price = _parse_positive_int(message.text)
    if not price:
        await _nudge(message, state, "Введите сумму числом, например 1500.")
        return
    await state.update_data(purchase_price=price)
    await state.set_state(BuybackIntake.payment_method)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="💵 Наличные", callback_data="buyback_pm_cash"),
        InlineKeyboardButton(text="💳 Карта/перевод", callback_data="buyback_pm_card"),
    ]])
    await _advance(message, state, "Чем платим клиенту?", reply_markup=keyboard)


@router.callback_query(F.data.startswith("buyback_pm_"), BuybackIntake.payment_method)
async def buyback_got_payment_method(callback: CallbackQuery, state: FSMContext) -> None:
    method = callback.data.removeprefix("buyback_pm_")
    await state.update_data(payment_method=method)
    await state.set_state(BuybackIntake.purpose)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔧 На запчасти", callback_data="buyback_purpose_parts"),
        InlineKeyboardButton(text="💵 На продажу", callback_data="buyback_purpose_resale"),
    ]])
    text = "Назначение:"
    await callback.message.edit_text(text, reply_markup=keyboard)
    await state.update_data(prompt_text=text)
    await callback.answer()


@router.callback_query(F.data.startswith("buyback_purpose_"), BuybackIntake.purpose)
async def buyback_got_purpose(callback: CallbackQuery, state: FSMContext) -> None:
    purpose = callback.data.removeprefix("buyback_purpose_")
    await state.update_data(purpose=purpose)

    if purpose == "resale":
        await state.set_state(BuybackIntake.resale_price)
        text = "Цена продажи (грн) — товар сразу появится в Продажах:"
    else:
        await state.set_state(BuybackIntake.photo)
        text = "📷 Пришлите фото устройства:"
    await callback.message.edit_text(text)
    await state.update_data(prompt_text=text)
    await callback.answer()


@router.message(BuybackIntake.resale_price, F.text)
async def buyback_got_resale_price(message: Message, state: FSMContext) -> None:
    price = _parse_positive_int(message.text)
    if not price:
        await _nudge(message, state, "Введите цену продажи числом, например 3000.")
        return
    await state.update_data(resale_price=price)
    await state.set_state(BuybackIntake.photo)
    await _advance(message, state, "📷 Пришлите фото устройства:")


@router.message(BuybackIntake.photo, F.photo)
async def buyback_got_photo(message: Message, state: FSMContext) -> None:
    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)
    await state.update_data(photo_bytes=buf.read())
    await state.set_state(BuybackIntake.confirm)

    data = await state.get_data()
    lines = [
        "📋 Проверьте данные:", "",
        f"Продавец: {data['client_name']}",
        f"Телефон: {data['client_phone']}",
        f"Устройство: {data['device_type']} {data['model']}",
        f"Платим клиенту: {data['purchase_price']} грн ({'Наличные' if data['payment_method'] == 'cash' else 'Карта/перевод'})",
        f"Назначение: {core_buyback.PURPOSES[data['purpose']]}",
    ]
    if data["purpose"] == "resale":
        lines.append(f"Цена продажи: {data['resale_price']} грн")
    lines.append("Фото: приложено ✅")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data="quick_buyback_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_buyback_cancel"),
    ]])
    await _advance(message, state, "\n".join(lines), reply_markup=keyboard)


@router.message(BuybackIntake.photo)
async def buyback_photo_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Пришлите фото устройства (как фото, не файлом).")


@router.callback_query(F.data == "quick_buyback_confirm", BuybackIntake.confirm)
async def buyback_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Магазин больше не настроен.", show_alert=True)
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _BUYBACK_ROLES:
            await state.clear()
            await callback.answer("Недостаточно прав.", show_alert=True)
            return

        order_id = core_buyback.create_buyback_intake(
            conn,
            client_name=data["client_name"], client_phone=data["client_phone"],
            device_type=data["device_type"], brand=None, model=data["model"],
            serial_number=None, condition_note=None,
            purchase_price=data["purchase_price"], payment_method=data["payment_method"],
            purpose=data["purpose"], resale_price=data.get("resale_price"),
            staff_id=staff["id"], photo=(data["photo_bytes"], ".jpg"),
        )

    await state.clear()
    open_keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(
            text="Открыть в CRM", web_app=WebAppInfo(url=f"{MINIAPP_URL.rstrip('/')}/buyback/{order_id}"),
        ),
    ]])
    await callback.message.edit_text(
        f"✅ Скупка №{order_id} принята.\n\nЕсли нужно уточнить состояние или способ оплаты — карточка скупки:",
        reply_markup=open_keyboard,
    )
    await callback.answer("Готово")


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
        InlineKeyboardButton(text="✅ Добавить", callback_data="quick_contact_confirm"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_contact_cancel"),
    ]])
    await _advance(message, state, f"📋 Проверьте:\nИмя: {data['name']}\nТелефон: {phone}", reply_markup=keyboard)


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

    await state.clear()
    await callback.message.edit_text(f"✅ Клиент добавлен (№{client_id}).")
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"quick_repair_cancel", "quick_contact_cancel", "quick_buyback_cancel"}))
async def quick_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _safe_delete(callback.bot, callback.message.chat.id, callback.message.message_id)
    await callback.answer("Отменено")


# --- catch-all: anything unexpected mid-flow (wrong content type, a stray
# message while waiting on inline-button confirm) gets a nudge instead of
# silence ---

@router.message(StateFilter(RepairIntake, QuickContact, BuybackIntake))
async def quick_flow_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Не понял ответ. Следуйте подсказке выше, либо ❌ Отмена.")
