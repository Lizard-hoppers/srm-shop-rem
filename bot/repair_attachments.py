"""Staff replying with a photo directly on a repair's card (in "Ремонт
техники" or "Мастера 007") attaches it to that repair's history —
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

import os
import uuid

from aiogram import F, Router
from aiogram.types import Message

from core import auth as core_auth
from core import photos as core_photos
from core import repairs as core_repairs
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()

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
    compressed = core_photos.compress_photo(data)
    if compressed is not None:
        data = compressed

    os.makedirs(_ATTACH_DIR, exist_ok=True)
    filename = f"{order_id}_{uuid.uuid4().hex}.jpg"
    with open(os.path.join(_ATTACH_DIR, filename), "wb") as f:
        f.write(data)

    with get_conn(store.db_path) as conn:
        core_repairs.add_attachment(conn, order_id, filename, message.caption, staff["id"])

    await message.reply(f"📎 Добавлено в историю ремонта №{order_id}")
