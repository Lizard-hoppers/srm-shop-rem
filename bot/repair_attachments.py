"""What staff send in reply to a repair's card in a work group stays with
the repair.

A TEXT or VOICE reply becomes a note (core.repair_notes, 09.10): a voice
message is transcribed, a long message is cut down to one short line by a
language model (core.ai_notes), the original is kept next to it, and the
card — in the group and in the Mini App — shows it. The same reading
tells where the repair stands: that becomes the card's «Стадия», and when
the message says the status itself has moved («готово», «отдал клиенту»)
the bot offers the one button that confirms it — it never changes a
status on its own. Likewise a message that names a new price for the
repair («нашли ещё поломку, выйдет 3000») gets the question «сменить цену
на 3000 грн?» with да / нет, and the price changes only on «да». A reply to such a
reply lands in the same repair. Nobody has to retype anything into the
CRM for the conversation not to be lost.

A PHOTO reply (in "Ремонт техники" or "Мастера 007") attaches it to that
repair's history —
core.repairs.repair_attachments. Trigger is the reply itself, resolved via
the same repair_order_messages table core.notify.sync_repair_cards() uses
— no picker UI, no extra typing, just Telegram's native reply gesture.

A bare photo with no reply is silently ignored (not every photo posted in
a staff group is meant for a specific repair — no case to guess which
one). Separate from bot/purchase_photo.py's F.photo handler, which is
private-chat-only and does something entirely different (OCR a supplier
invoice) — the two must never both fire on the same photo.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji

from bot.repair_actions import _sync_after_change
from core import ai_notes
from core import auth as core_auth
from core import documents as core_documents
from core import photos as core_photos
from core import repair_notes as core_notes
from core import repairs as core_repairs
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()
logger = logging.getLogger(__name__)

_REPAIR_ROLES = ("owner", "admin", "master")

_ATTACH_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "repair_photos")


@router.message(F.photo, F.reply_to_message, F.chat.type != "private")
async def photo_reply_to_repair(message: Message) -> None:
    """Group-chat only, deliberately — repair cards only ever get posted
    in "Ремонт техники"/"Мастера 007" (both groups), never DM, so a reply
    to one can only happen there. Scoping this out of private chats keeps
    it from ever matching before bot/purchase_photo.py's DM-only invoice-
    OCR handler gets a look at a reply-photo sent there (aiogram stops at
    the first matching handler, and this filter would otherwise match
    first purely by luck of router registration order)."""
    # Фаза C (23.08): resolve the store by the group this reply landed in
    # (same reasoning as bot/repair_actions.py) before touching any DB —
    # an unrecognized group (store config changed since a card went out,
    # or the bot's in some other group entirely) is silently ignored, same
    # spirit as "a bare photo with no reply is silently ignored" above.
    store = store_for_chat_id(message.chat.id)
    if not store:
        return

    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
        if not staff or staff["role"] not in _REPAIR_ROLES:
            return
        order_id = core_repairs.find_order_by_message(
            conn, str(message.chat.id), message.reply_to_message.message_id
        )
        if not order_id:
            return  # replied to something else — not our case

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)

    data = buf.read()
    compressed = await asyncio.to_thread(core_photos.compress_photo, data)
    if compressed is not None:
        data = compressed

    os.makedirs(_ATTACH_DIR, exist_ok=True)
    filename = f"{order_id}_{uuid.uuid4().hex}.jpg"
    with open(os.path.join(_ATTACH_DIR, filename), "wb") as f:
        f.write(data)

    with get_conn(store.db_path) as conn:
        core_repairs.add_attachment(conn, order_id, filename, message.caption, staff["id"])
    # A caption is something said about the repair too — it goes into
    # the notes like any other reply.
    if (message.caption or "").strip():
        _shown, hints = await _save_note(message, store, order_id, "photo", message.caption.strip(), staff)
        for text, keyboard in hints:
            await message.reply(text, reply_markup=keyboard)

    await message.reply(f"📎 Добавлено в историю ремонта №{order_id}")


# ---- заметки: текст и голос в ответ на карточку ----

_MAX_VOICE_BYTES = 20 * 1024 * 1024  # what a bot may download from Telegram at all


def _author_name(message: Message) -> str:
    user = message.from_user
    return (user.full_name or user.username or str(user.id)) if user else "—"


def _status_hint(order_id: int, current: str, suggested: str | None) -> tuple[str, InlineKeyboardMarkup] | None:
    """The message says the repair has moved on, and the CRM doesn't know
    yet: a line saying so and the ONE button that makes it true — the
    same buttons the card itself carries, so every check behind them
    applies («Готово» still asks about the part, «Выдан» still takes the
    money in the app). The bot never changes a status off a sentence."""
    label = core_documents.label("repair", order_id)
    step = {
        ("new", "in_progress"): ("в работе", "🔧 Взять в работу", f"repair_take:{order_id}"),
        ("in_progress", "ready"): ("готов", "✅ Готово", f"repair_done:{order_id}"),
        ("in_progress", "cancelled"): ("починить не удалось", "❌ Не удалось починить", f"repair_release:{order_id}"),
        ("in_progress", "issued"): ("уже выдан", "📲 Оформить выдачу в CRM", f"open_crm:repair:{order_id}"),
        ("ready", "issued"): ("выдан клиенту", "📲 Оформить выдачу в CRM", f"open_crm:repair:{order_id}"),
    }.get((current, suggested))
    if not step:
        return None
    said, button, data = step
    text = f"🤖 {label}: похоже, {said}, а в CRM статус «{core_repairs.STATUS_LABELS[current]}». Подтвердите:"
    return text, InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=button, callback_data=data)]])


def _price_hint(order_id: int, status: str, old, new) -> tuple[str, InlineKeyboardMarkup] | None:
    """The message names a new price for the repair: ask, with «да» and
    «нет» — the price changes only on «да» (price_confirm below)."""
    if new is None or status in ("issued", "cancelled"):
        return None
    label = core_documents.label("repair", order_id)
    was = f"сейчас {old} грн" if old else "сейчас цена не указана"
    text = f"🤖 {label}: похоже, цена ремонта изменилась — {was}. Сменить на <b>{new} грн</b>?"
    return text, InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"✅ Да, {new} грн", callback_data=f"rprice:{order_id}:{new}"),
        InlineKeyboardButton(text="Нет", callback_data=f"rprice_no:{order_id}"),
    ]])


async def _save_note(message: Message, store, order_id: int, kind: str, original: str, staff) -> tuple[str, list[tuple]]:
    """Read (short version, stage, status, price), store, bring the cards
    up to date. Returns the text the card will show and the questions the
    message raises — «the status has moved on?» (_status_hint), «the price
    has changed?» (_price_hint) — for a person to confirm. A model failure
    costs only the short version and the reading — the note itself is
    always saved."""
    with get_conn(store.db_path) as conn:
        repair = core_repairs.get_repair(conn, order_id)
    current, price = repair["status"], core_repairs.current_price(repair)
    try:
        read = await asyncio.to_thread(ai_notes.analyze, original, core_repairs.STATUS_LABELS[current], price)
    except ai_notes.NoteAiError as exc:
        logger.warning("note for repair %s saved as it is, unread: %s", order_id, exc)
        # Nothing to cut in a short message — it is its own short version.
        read = {"summary": original if len(original) <= ai_notes.SHORT_ENOUGH else None,
                "stage": None, "status": None, "price": None}
    with get_conn(store.db_path) as conn:
        core_notes.add_note(
            conn, order_id, kind, original, read["summary"], staff_id=staff["id"] if staff else None,
            author_name=_author_name(message), chat_id=str(message.chat.id), message_id=message.message_id,
            stage=read["stage"], suggested_status=read["status"], suggested_price=read["price"],
        )
    _sync_after_change(order_id, store.db_path)
    hints = [_status_hint(order_id, current, read["status"]), _price_hint(order_id, current, price, read["price"])]
    return read["summary"] or original, [hint for hint in hints if hint]


@router.callback_query(F.data.startswith("rprice:"))
async def price_confirm(callback: CallbackQuery) -> None:
    """«Да» under «сменить цену?». Money, so only someone who is staff in
    the CRM may press it — the question stays for them if anyone else
    taps."""
    store = store_for_chat_id(callback.message.chat.id)
    if not store:
        await callback.answer("Эта группа не привязана ни к одному магазину.", show_alert=True)
        return
    _prefix, raw_order, raw_price = callback.data.split(":")
    order_id, new_price = int(raw_order), float(raw_price)
    new_price = int(new_price) if new_price == int(new_price) else new_price
    try:
        with get_conn(store.db_path) as conn:
            staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
            if not staff:
                await callback.answer("Цену меняет сотрудник, подключённый к CRM.", show_alert=True)
                return
            old = core_repairs.change_price(conn, order_id, new_price, staff["id"])
    except core_repairs.RepairPartError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    label = core_documents.label("repair", order_id)
    await callback.message.edit_text(
        f"✅ {label}: цена изменена — {old or '—'} → <b>{new_price} грн</b> ({html.escape(staff['name'])})"
    )
    await callback.answer("Цена изменена")
    _sync_after_change(order_id, store.db_path)


@router.callback_query(F.data.startswith("rprice_no:"))
async def price_decline(callback: CallbackQuery) -> None:
    """«Нет»: the question goes away, the price stays."""
    try:
        await callback.message.delete()
    except TelegramAPIError:
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.answer("Цена не изменена")


@router.message(F.reply_to_message, F.chat.type != "private", F.text | F.voice | F.audio)
async def note_reply_to_repair(message: Message) -> None:
    """A text or voice message sent in reply to a repair's card (or to an
    earlier note on it). Anyone in the work group may leave one — a master
    who was never linked to a staff card is recorded under his Telegram
    name. Replies to anything else in the group are none of our business."""
    store = store_for_chat_id(message.chat.id)
    if not store:
        return
    if message.text and message.text.startswith("/"):
        return
    chat_id, replied_id = str(message.chat.id), message.reply_to_message.message_id
    with get_conn(store.db_path) as conn:
        order_id = (
            core_repairs.find_order_by_message(conn, chat_id, replied_id)
            or core_notes.find_order_by_note_message(conn, chat_id, replied_id)
        )
        if not order_id or core_notes.exists(conn, chat_id, message.message_id):
            return
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
    label = core_documents.label("repair", order_id)

    if message.text:
        kind, original = "text", message.text.strip()
        if not original:
            return
    else:
        kind, media = "voice", message.voice or message.audio
        if media.file_size and media.file_size > _MAX_VOICE_BYTES:
            await message.reply("Слишком длинное голосовое — запишите короче или напишите текстом.")
            return
        try:
            file = await message.bot.get_file(media.file_id)
            buf = await message.bot.download_file(file.file_path)
            original = await asyncio.to_thread(ai_notes.transcribe, buf.read())
        except (ai_notes.NoteAiError, TelegramAPIError) as exc:
            logger.warning("voice note for repair %s not transcribed: %s", order_id, exc)
            await message.reply(f"Не смог расшифровать голосовое — в {label} ничего не записано. Напишите текстом.")
            return

    shown, hints = await _save_note(message, store, order_id, kind, original, staff)
    if kind == "voice":
        # What was heard is said back: the sender can see at once whether
        # the note says what he meant.
        await message.reply(f"🎤 {label}: {html.escape(shown)}")
    else:
        try:
            await message.react([ReactionTypeEmoji(emoji="✍")])
        except TelegramAPIError:
            pass  # reactions switched off in this group — the note is saved either way
    for text, keyboard in hints:
        await message.reply(text, reply_markup=keyboard)
