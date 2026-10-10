"""What staff send in reply to a repair's card in a work group stays with
the repair.

A TEXT or VOICE reply becomes a note (core.repair_notes, 09.10): a voice
message is transcribed, a long message is cut down to one short line by a
language model (core.ai_notes), the original is kept next to it, and the
card — in the group and in the Mini App — shows it. The same reading
tells where the repair stands: that becomes the card's «Стадия», and when
the message says the status itself has moved («взял», «готово», «отдал
клиенту, оплатил картой») the bot moves it right there, money included —
nobody goes to the CRM for it — and leaves «Вернуть» under its answer in
case it read the message wrong. Likewise a message that names a new price for the
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
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji

from bot.repair_actions import _sync_after_change
from core import accounts as core_accounts
from core import ai_notes
from core import auth as core_auth
from core import cash as core_cash
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

STATUS_ONLY_LEN = 40
_MAX_VOICE_BYTES = 20 * 1024 * 1024  # what a bot may download from Telegram at all


def _author_name(message: Message) -> str:
    user = message.from_user
    return (user.full_name or user.username or str(user.id)) if user else "—"


def apply_status(
    store, order_id: int, new_status: str | None, staff, *, payment: str | None = None, account_id: int | None = None,
) -> tuple[str, InlineKeyboardMarkup | None] | None:
    """Act on «the repair has moved on» — from a note, from a request to
    the assistant, or from the «как оплатили?» buttons below. Returns
    what to say in the chat (text, keyboard), or None when there is
    nothing to do (not a forward move from where the repair is).

    The status changes right here — nobody is sent to the CRM. «Выдан»
    takes the price into the касса: onto the account named, or by the way
    of paying the message named; when neither is known and there is a
    price, the bot asks ONE thing — куда оплата — with a button per
    гривневый счёт of the точка. Under every change there is «Вернуть»:
    a sentence can be misread, and one tap puts it back."""
    label = core_documents.label("repair", order_id)
    staff_id = staff["id"] if staff else None
    with get_conn(store.db_path) as conn:
        repair = core_repairs.get_repair(conn, order_id)
        if not repair or not core_repairs.chat_move_allowed(repair["status"], new_status):
            return None
        price = core_repairs.current_price(repair)
        if new_status == "issued" and price and not payment and not account_id:
            accounts = [a for a in core_accounts.list_accounts(conn, repair["location_id"])
                        if a["currency"] == core_accounts.BASE_CURRENCY]
            return (
                f"🤖 {label}: выдаю клиенту. Как оплатили <b>{price} грн</b>?",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=a["name"], callback_data=f"rissue:{order_id}:{a['id']}")] for a in accounts
                ]),
            )
        try:
            done = core_repairs.move_from_chat(conn, order_id, new_status, staff_id, account_id=account_id, method=payment)
        except (core_repairs.RepairPartError, core_cash.PaymentError) as exc:
            return f"🤖 {label}: {html.escape(str(exc))}", None
        warn = ""
        if new_status == "ready" and core_repairs.needs_part(conn, core_repairs.get_repair(conn, order_id)):
            warn = "\n⚠️ Запчасть не указана — отметьте её кнопкой «⚙️ Указать запчасть» на карточке."
    labels = core_repairs.STATUS_LABELS
    if new_status == "issued":
        money_line = f" · {done['paid']} грн → {html.escape(done['account'])}" if done["paid"] else " · без оплаты (цена не указана)"
        text = f"✅ {label} выдан{money_line}"
    else:
        text = f"✅ {label}: {labels[done['previous']]} → <b>{labels[new_status]}</b>{warn}"
    undo = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"↩️ Вернуть «{labels[done['previous']]}»", callback_data=f"rundo:{order_id}:{done['previous']}",
    )]])
    return text, undo


async def _callback_context(callback: CallbackQuery):
    """(store, staff-or-None) for a button under one of the bot's own
    messages in a work group. Anyone in the group may press these — the
    same people whose messages move a status in the first place."""
    store = store_for_chat_id(callback.message.chat.id)
    if not store:
        await callback.answer("Эта группа не привязана ни к одному магазину.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        return store, core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)


@router.callback_query(F.data.startswith("rissue:"))
async def issue_paid_into(callback: CallbackQuery) -> None:
    """An answer to «как оплатили?»: hand the repair over, the price onto this account."""
    ctx = await _callback_context(callback)
    if not ctx:
        return
    store, staff = ctx
    _prefix, raw_order, raw_account = callback.data.split(":")
    order_id = int(raw_order)
    result = apply_status(store, order_id, "issued", staff, account_id=int(raw_account))
    if not result:
        await callback.answer("Этот ремонт уже не ждёт выдачи.", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        return
    await callback.message.edit_text(result[0], reply_markup=result[1])
    await callback.answer("Выдан")
    _sync_after_change(order_id, store.db_path)


@router.callback_query(F.data.startswith("rundo:"))
async def status_undo(callback: CallbackQuery) -> None:
    """«Вернуть» under a status the bot changed."""
    ctx = await _callback_context(callback)
    if not ctx:
        return
    store, staff = ctx
    _prefix, raw_order, back_to = callback.data.split(":")
    order_id = int(raw_order)
    try:
        with get_conn(store.db_path) as conn:
            was = core_repairs.undo_chat_status(conn, order_id, back_to, staff["id"] if staff else None)
    except core_repairs.RepairPartError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    labels = core_repairs.STATUS_LABELS
    note = " Оплата снята с кассы." if was == "issued" else ""
    await callback.message.edit_text(
        f"↩️ {core_documents.label('repair', order_id)}: возвращён из «{labels[was]}» в <b>{labels[back_to]}</b>.{note}"
    )
    await callback.answer("Возвращено")
    _sync_after_change(order_id, store.db_path)


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
    """Read (short version, stage, status, price, payment), store, act,
    bring the cards up to date. Returns the text the card will show and
    what the bot has to say about it: «статус изменён» with its «Вернуть»
    (apply_status — the status moves on the message alone), «сменить
    цену?» (_price_hint — the price moves only on «да»). A model failure
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
                "stage": None, "status": None, "price": None, "payment": None}
    # A few words that only report a status («выдан», «готово», «взял в
    # работу») are an instruction, not something to remember: the status
    # moves and the repair's history records it — they don't pile up in
    # the notes. Anything longer may carry more than the status and is kept.
    only_status = (core_repairs.chat_move_allowed(current, read["status"]) and len(original) <= STATUS_ONLY_LEN
                   and not read["stage"] and read["price"] is None)
    if not only_status:
        with get_conn(store.db_path) as conn:
            core_notes.add_note(
                conn, order_id, kind, original, read["summary"], staff_id=staff["id"] if staff else None,
                author_name=_author_name(message), chat_id=str(message.chat.id), message_id=message.message_id,
                stage=read["stage"], suggested_status=read["status"], suggested_price=read["price"],
            )
    moved = apply_status(store, order_id, read["status"], staff, payment=read.get("payment"))
    _sync_after_change(order_id, store.db_path)
    # A price named in the same breath as «выдан» is what was paid, not a
    # change to argue about — only an open repair is asked about its price.
    hints = [moved, None if read["status"] in ("issued", "cancelled") else _price_hint(order_id, current, price, read["price"])]
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


# ---- команды карточке: то, что меняет её саму, а не добавляет к ней слова ----

# «сумма 3000», «цена 3000 грн», «стоимость 0», «3000 к оплате», «итого 4500» — a price said
# to the card, nothing else in the message. Longer talk that mentions a
# price («нашли ещё поломку, выйдет 4000») is not this — that one is read
# by the model and asked about (_price_hint).
_PRICE_WORDS = r"(?:сумма|сума|цена|ціна|стоимость|вартість|итого|разом|к\s+оплате|до\s+сплати|ремонт)"
_PRICE_SAID = re.compile(
    rf"^\s*(?:{_PRICE_WORDS}\s*(?:ремонта|ремонту)?\s*[:=—-]?\s*(\d[\d\s]{{0,8}})(?:[.,]\d+)?\s*(?:грн|гривен|гривень|₴)?"
    rf"|(\d[\d\s]{{0,8}})\s*(?:грн|гривен|гривень|₴)?\s*{_PRICE_WORDS})\s*[.!]?\s*$",
    re.IGNORECASE,
)
_HIDE_NOTES = re.compile(r"(?:убери|убрать|уберите|удали|удалить|очисти|очистить|скрой|скрыть|прибери|видали)\w*\s+(?:все\s+|всі\s+)?(?:заметк|нотатк|комментар|коментар)", re.IGNORECASE)


_SHOW_NOTES = re.compile(r"(?:верни|вернуть|верните|покажи|показать|восстанови|восстановить|поверни|покажи)\w*\s+(?:назад\s+|обратно\s+)?(?:все\s+|всі\s+)?(?:заметк|нотатк|комментар|коментар)", re.IGNORECASE)
# «удали», «убери это», «сотри эту заметку» — said in reply to the message a note was made from.
_REMOVE_THIS = re.compile(r"^\s*(?:удали|удалить|убери|убрать|сотри|стереть|видали|прибери)\w*(?:\s+(?:это|эту|этот|цю|це))?(?:\s+(?:заметку|запись|сообщение|нотатку|запис))?\s*[.!]?\s*$", re.IGNORECASE)


def show_notes_said(text: str | None) -> bool:
    return bool(_SHOW_NOTES.search(text or ""))


def price_said(text: str | None):
    """The price a message states to a card, if that is ALL it says — else None. Zero counts."""
    match = _PRICE_SAID.match(text or "")
    if not match:
        return None
    digits = re.sub(r"\s", "", match.group(1) or match.group(2))
    return int(digits) if digits and int(digits) < ai_notes.MAX_PRICE else None


def hide_notes_said(text: str | None) -> bool:
    return bool(_HIDE_NOTES.search(text or ""))


async def card_command(message: Message, store, order_id: int, staff, text: str) -> bool:
    """A reply to a repair's card that is an instruction ABOUT the card:
    «сумма 3000» sets the repair's price right there (it is not filed as a
    note — the price on the card is the record), «убери заметки» takes
    the notes block off the card. Returns True if the message was one of
    these and has been dealt with. «Верни заметки» puts the block back;
    «удали» in reply to one note's own message takes just that note off.
    Also reached from «бот, …» sent as a reply to a card
    (bot/assistant_chat.py)."""
    label = core_documents.label("repair", order_id)
    replied = message.reply_to_message
    if replied and _REMOVE_THIS.match(text or ""):
        # Only when the reply is to a message that made a note — «убери» said to the card itself is not this.
        with get_conn(store.db_path) as conn:
            removed = core_notes.remove_by_message(conn, str(message.chat.id), replied.message_id)
        if removed:
            _sync_after_change(order_id, store.db_path)
            shown = removed["summary"] or removed["original_text"]
            await message.reply(f"🧹 {label}: заметка убрана с карточки — «{html.escape(shown[:80])}».")
            return True
        # A bare «удали» said to the card itself: not a note worth keeping, and not clear enough to act on.
        await message.reply("Что убрать? Одну заметку — ответьте «удали» на то сообщение, из которого она сделана; все сразу — «убери заметки».")
        return True
    if show_notes_said(text):
        with get_conn(store.db_path) as conn:
            back = core_notes.show_on_card(conn, order_id)
        _sync_after_change(order_id, store.db_path)
        await message.reply(f"📝 {label}: заметки возвращены на карточку ({back})." if back else f"{label}: скрытых заметок нет — возвращать нечего.")
        return True
    if hide_notes_said(text):
        with get_conn(store.db_path) as conn:
            hidden = core_notes.hide_from_card(conn, order_id)
        _sync_after_change(order_id, store.db_path)
        await message.reply(f"🧹 {label}: заметки убраны с карточки ({hidden})." if hidden else f"{label}: заметок на карточке и так нет.")
        return True
    price = price_said(text)
    if price is None:
        return False
    if not staff:
        # Not linked to the CRM: his word doesn't move a price by itself —
        # the question goes to the chat for a staff member to answer.
        with get_conn(store.db_path) as conn:
            repair = core_repairs.get_repair(conn, order_id)
        hint = _price_hint(order_id, repair["status"], core_repairs.current_price(repair), price)
        if hint:
            await message.reply(hint[0], reply_markup=hint[1])
        return True
    try:
        with get_conn(store.db_path) as conn:
            old = core_repairs.change_price(conn, order_id, price, staff["id"])
    except core_repairs.RepairPartError as exc:
        await message.reply(f"{label}: {html.escape(str(exc))}")
        return True
    _sync_after_change(order_id, store.db_path)
    undo = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="↩️ Вернуть", callback_data=f"aundo:price:{order_id}:{0 if old is None else old}",
    )]])
    await message.reply(f"✅ {label}: цена {'—' if old is None else old} → <b>{price} грн</b>", reply_markup=undo)
    return True


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
        if await card_command(message, store, order_id, staff, original):
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

    if kind == "voice" and (price_said(original) is not None or hide_notes_said(original) or show_notes_said(original)):
        await message.reply(f"🎤 {html.escape(original)}")
        if await card_command(message, store, order_id, staff, original):
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
