from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from bot.quick_actions import QUICK_ACTIONS_KEYBOARD
from core import auth as core_auth
from core import clients as core_clients
from core import qr as core_qr
from core import store_access
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()

# Shown only to someone not yet in any store's staff list AND not already a
# registered client — the ID line is for a future staff member who needs to
# send it to the owner for core.link_telegram. Once linked, every future
# /start matches is_staff=True (straight to QUICK_ACTIONS_KEYBOARD below,
# no ID line) — this message never repeats for them again. Was on the old
# staff-welcome message itself before (backwards — already-approved staff
# saw "если доступ ещё не открыт" on every single /start; Павел flagged it
# 24.08).
CLIENT_ASK_PHONE = (
    "Здравствуйте! Это бот сервис-центра.\n\n"
    "Поделитесь номером телефона, чтобы получить карту скидок — при следующем визите "
    "мы найдём вас по QR-коду за секунду.\n\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n"
    "Если вы сотрудник и доступ к CRM ещё не открыт — пришлите этот ID владельцу."
)

CLIENT_WELCOME_BACK = "С возвращением! Вот ваша карта скидок — покажите её в сервисе."
CLIENT_REGISTERED = "Готово! Вот ваша карта скидок — покажите её в сервисе."


@router.message(CommandStart(), F.chat.type == "private")
async def start(message: Message) -> None:
    # Фаза C (23.08): "is this telegram_id CRM staff at all" has to check
    # every store, not just the default one — a master added only to
    # Магазин 2/3 used to be wrongly treated as a walk-in client here
    # (get_conn() with no path only ever saw the default store's DB) and
    # asked to share their phone number. Self-registration itself (the
    # client branch below) stays on the default store, unchanged —
    # согласовано с Павлом 23.08, see OPERATIONS.md "Мультимагазинность".
    is_staff = bool(store_access.accessible_stores(message.from_user.id))
    client = None
    if not is_staff:
        with get_conn() as conn:
            client = core_clients.get_by_telegram_id(conn, message.from_user.id)

    if is_staff:
        # No separate "Открыть CRM" message/button — the chat's menu
        # button (bottom-left, set in bot/bot.py) already opens the Mini
        # App directly, so a staff member always has that regardless of
        # /start. Straight to the quick-action buttons.
        await message.answer("Быстрые действия:", reply_markup=QUICK_ACTIONS_KEYBOARD)
        return

    if client:
        await _send_card(message, client["id"], CLIENT_WELCOME_BACK)
        return

    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )
    await message.answer(CLIENT_ASK_PHONE.format(user_id=message.from_user.id), reply_markup=keyboard)


@router.message(Command("chatid"))
async def chat_id_cmd(message: Message) -> None:
    """Onboarding helper: add the bot to a store's group as admin, send this
    there, and put the returned id into that store's staff_group_chat_id/
    masters_group_chat_id in stores.json (Фаза C, 23.08 — was CRM_STAFF_GROUP_CHAT_ID
    in .env before per-store config existed) — that's the chat new-repair
    cards get posted to. Commands reach the bot in groups regardless of
    privacy mode, so no extra bot setup is needed. Also reports which
    store (if any) this chat is already wired to — handy for checking a
    new store's group is configured correctly."""
    store = store_for_chat_id(message.chat.id)
    suffix = f"\nМагазин: {store.name}" if store else "\n⚠️ Не привязан ни к одному магазину."
    await message.answer(f"Chat ID: <code>{message.chat.id}</code>{suffix}")


@router.message(F.contact)
async def got_contact(message: Message) -> None:
    """Two distinct flows share this one trigger:
    - a client, in DM, sharing their OWN contact -> self-registration
      (unchanged).
    - a STAFF member, anywhere (DM or a staff group — "Работа",
      "Мастера 007"), sharing SOMEONE ELSE's contact -> offer to add that
      person as a client (see offer_add_client/confirm_add_client below).
      This is exactly the case the 67dea42 private-only fix accidentally
      silenced: staff sharing a *client's* contact in a group topic to
      note it now gets routed to the right flow instead of nothing at all.
    Anything else (a non-staff person sharing someone else's contact
    outside DM) is ignored — no case for the bot to react there."""
    if not message.contact:
        return
    is_own_contact = message.contact.user_id == message.from_user.id

    # Same Фаза C fix as start(): "is this telegram_id staff at all" has
    # to check every store, not just the default one.
    is_staff = bool(store_access.accessible_stores(message.from_user.id))

    if is_staff and not is_own_contact:
        await offer_add_client(message)
        return

    if message.chat.type != "private":
        return

    if not is_own_contact:
        await message.answer("Пожалуйста, поделитесь своим собственным номером — кнопкой ниже.")
        return

    name = message.from_user.full_name or "Клиент"
    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, name, message.contact.phone_number, source="online")
        core_clients.link_telegram(conn, client_id, message.from_user.id)

    await _send_card(message, client_id, CLIENT_REGISTERED, remove_keyboard=True)


async def offer_add_client(message: Message) -> None:
    contact = message.contact
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "Клиент"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➕ Добавить как клиента", callback_data="contact_add_client"),
    ]])
    # html.escape: бот поднят с parse_mode=HTML (bot/bot.py), а имя приходит
    # из чужой карточки контакта — «Вася <дома>» уронил бы отправку на
    # разборе HTML (манифест §7).
    await message.reply(
        f"Добавить в CRM как клиента?\n{html.escape(name)} — {html.escape(contact.phone_number)}",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "contact_add_client")
async def confirm_add_client(callback: CallbackQuery) -> None:
    # Фаза C (23.08): staff adding SOMEONE ELSE's contact as a client is a
    # staff action, so it resolves the same way as any other — a group
    # (the confirmation reply lands in whichever chat the contact was
    # shared in) resolves unambiguously by chat_id; a DM has no such
    # signal and falls back to the sender's current store (same mechanism
    # as bot/purchase_photo.py).
    chat = callback.message.chat if callback.message else None
    if chat and chat.type != "private":
        store = store_for_chat_id(chat.id)
    else:
        accessible = store_access.accessible_stores(callback.from_user.id)
        store = store_access.pick_default_store(callback.from_user.id, accessible) if accessible else None
    if not store:
        await callback.answer("Эта группа не привязана ни к одному магазину.", show_alert=True)
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff:
        await callback.answer("Недостаточно прав.", show_alert=True)
        return

    contact_message = callback.message.reply_to_message if callback.message else None
    if not contact_message or not contact_message.contact:
        await callback.answer("Не нашёл контакт — попробуйте ещё раз.", show_alert=True)
        return

    contact = contact_message.contact
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() or "Клиент"
    with get_conn(store.db_path) as conn:
        client_id = core_clients.get_or_create_by_phone(conn, name, contact.phone_number, source="offline")

    await callback.message.edit_text(f"✅ Добавлен клиент: {html.escape(name)} (№{client_id})")
    await callback.answer("Готово")


async def _send_card(message: Message, client_id: int, caption: str, remove_keyboard: bool = False) -> None:
    png = core_qr.generate_png(core_qr.client_code(client_id))
    photo = BufferedInputFile(png, filename="card.png")
    kwargs = {"reply_markup": ReplyKeyboardRemove()} if remove_keyboard else {}
    await message.answer_photo(photo, caption=f"{caption}\n\nНомер карты: №{client_id}", **kwargs)
