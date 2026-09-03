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

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
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

# Real button colors: `style` isn't a typed aiogram field (not in
# KeyboardButton.model_fields), but the Pydantic model has extra="allow"
# and forwards whatever it's given straight into the Bot API JSON — the
# same undocumented-but-real technique already used throughout
# taki_vmeste/ui.py (style="primary"/blue, "success"/green,
# "danger"/red), confirmed live against that bot 24.08. Leave the kwarg
# off entirely for a plain/unstyled button — Приход stays plain, Павел's
# call.
BTN_REPAIR = "🔧 Ремонт"
BTN_CONTACT = "👤 Контакт"
BTN_BUYBACK = "💰 Скупка"
BTN_PURCHASE = "📦 Приход"
BTN_CANCEL = "❌ Отмена"

# Один словарь на оба места, где способ оплаты показывается сотруднику
# (шапка экрана и карточка подтверждения) — раньше метки были продублированы
# инлайновым тернарником в карточке.
_PAYMENT_LABELS = {"cash": "Наличные", "card": "Карта/перевод"}

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
        [KeyboardButton(text=BTN_REPAIR), KeyboardButton(text=BTN_CONTACT, style="primary")],
        [KeyboardButton(text=BTN_BUYBACK, style="success"), KeyboardButton(text=BTN_PURCHASE)],
        [KeyboardButton(text=BTN_CANCEL, style="danger")],
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
    return ["name", "phone", "device_type", "model", "defect", "photo"]


def _steps_contact(_data: dict) -> list[str]:
    return ["name", "phone"]


def _steps_buyback(data: dict) -> list[str]:
    steps = ["name", "phone", "device_type", "model", "price",
             "payment_method", "purpose", "resale_price", "photo"]
    if data.get("purpose") == "parts":
        # «На запчасти» пропускает вопрос о цене продажи — не считаем шаг,
        # которого уже точно не будет.
        steps.remove("resale_price")
    return steps


# HTML везде ниже: бот поднят с parse_mode=HTML (bot/bot.py), а имя клиента
# и описание неисправности сотрудник вводит руками. Без экранирования имя
# вида «Вася <дома>» валит editMessageText/sendMessage на разборе HTML —
# и раньше это тихо съедалось _safe_edit, то есть диалог просто замирал
# без единого следа в логах (манифест §7).

def _client_line(label: str, data: dict) -> str | None:
    name = data.get("client_name")
    if not name:
        return None
    phone = data.get("client_phone")
    return f"{label}: {html.escape(name)}" + (f" · {html.escape(phone)}" if phone else "")


def _device_line(data: dict) -> str | None:
    device = " ".join(x for x in (data.get("device_type"), data.get("model")) if x)
    return f"Устройство: {html.escape(device)}" if device else None


def _summary_repair(data: dict) -> list[str]:
    lines = [_client_line("Клиент", data), _device_line(data)]
    if data.get("defect_description"):
        lines.append(f"Неисправность: {html.escape(data['defect_description'])}")
    return [line for line in lines if line]


def _summary_buyback(data: dict) -> list[str]:
    lines = [_client_line("Продавец", data), _device_line(data)]
    if data.get("purchase_price"):
        method = _PAYMENT_LABELS.get(data.get("payment_method"))
        lines.append(f"Платим клиенту: {data['purchase_price']} грн" + (f" ({method})" if method else ""))
    if data.get("purpose"):
        lines.append(f"Назначение: {core_buyback.PURPOSES[data['purpose']]}")
    if data.get("resale_price"):
        lines.append(f"Цена продажи: {data['resale_price']} грн")
    return [line for line in lines if line]


def _summary_contact(data: dict) -> list[str]:
    return [f"Имя: {html.escape(data['name'])}"] if data.get("name") else []


# Ключ — имя StatesGroup ровно так, как aiogram отдаёт его в
# state.get_state() ("RepairIntake:model").
_FLOWS = {
    "RepairIntake": ("🔧 Приём ремонта", _steps_repair, _summary_repair),
    "BuybackIntake": ("💰 Скупка техники", _steps_buyback, _summary_buyback),
    "QuickContact": ("👤 Новый клиент", _steps_contact, _summary_contact),
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

@router.message(F.text == BTN_REPAIR, F.chat.type == "private")
async def repair_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return  # not CRM staff — ignore silently, same as purchase_photo.py
    store, staff = resolved
    if staff["role"] not in _REPAIR_ROLES:
        await message.answer("Недостаточно прав для приёма ремонта.")
        return

    await _reset(message, state)
    await state.set_state(RepairIntake.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "Имя клиента:")


@router.message(F.text == BTN_CONTACT, F.chat.type == "private")
async def contact_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved

    await _reset(message, state)
    await state.set_state(QuickContact.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "Имя клиента:")


@router.message(F.text == BTN_BUYBACK, F.chat.type == "private")
async def buyback_start(message: Message, state: FSMContext) -> None:
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _BUYBACK_ROLES:
        await message.answer("Недостаточно прав для скупки.")
        return

    await _reset(message, state)
    await state.set_state(BuybackIntake.name)
    await state.update_data(store_id=store.id)
    await _send_prompt(message, state, "Имя клиента (продавца):")


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

    await _reset(message, state)
    await _consume(state, message)
    await message.answer("📦 Пришлите фото накладной — распознаю позиции и предложу оприходовать.")


@router.message(F.text == BTN_CANCEL, StateFilter(RepairIntake, QuickContact, BuybackIntake))
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
        f"Клиент: {html.escape(data['client_name'])}\n"
        f"Телефон: {html.escape(data['client_phone'])}\n"
        f"Устройство: {html.escape(data['device_type'])} {html.escape(data['model'])}\n"
        f"Неисправность: {html.escape(data['defect_description'])}\n"
        "Фото: приложено ✅"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data="quick_repair_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_repair_cancel", style="danger"),
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

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    # Clean Chat: wipe the whole Q&A trail (name/phone/... replies), leave
    # only the final confirmation card as this flow's one lasting trace.
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
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
    await _advance_callback(callback, state, "Назначение:", keyboard)
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
    await _advance_callback(callback, state, text)
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
        f"Продавец: {html.escape(data['client_name'])}",
        f"Телефон: {html.escape(data['client_phone'])}",
        f"Устройство: {html.escape(data['device_type'])} {html.escape(data['model'])}",
        f"Платим клиенту: {data['purchase_price']} грн ({_PAYMENT_LABELS[data['payment_method']]})",
        f"Назначение: {core_buyback.PURPOSES[data['purpose']]}",
    ]
    if data["purpose"] == "resale":
        lines.append(f"Цена продажи: {data['resale_price']} грн")
    lines.append("Фото: приложено ✅")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data="quick_buyback_confirm", style="success"),
        InlineKeyboardButton(text="❌ Отмена", callback_data="quick_buyback_cancel", style="danger"),
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

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await _safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
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
    await callback.message.edit_text(f"✅ Клиент добавлен (№{client_id}).")
    await callback.answer("Готово")


@router.callback_query(F.data.in_({"quick_repair_cancel", "quick_contact_cancel", "quick_buyback_cancel"}))
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

@router.message(StateFilter(RepairIntake, QuickContact, BuybackIntake))
async def quick_flow_fallback(message: Message, state: FSMContext) -> None:
    await _nudge(message, state, "Не понял ответ. Следуйте подсказке выше, либо ❌ Отмена.")
