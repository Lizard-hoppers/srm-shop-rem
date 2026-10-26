"""«🔧 Ремонты» — a button that is always under the input field in the
masters' group and the work group (11.10).

One tap shows every repair not yet handed out, the same list «бот,
сколько у нас ремонтов» gives (core.repair_digest): name, price, status,
the name a link to the repair's card.

It is a reply keyboard, not an inline one, on purpose: an inline button
lives under one message and scrolls away with it; this one stays docked
(is_persistent) whatever is written after. Telegram keeps it per person
from the last message that carried it, so every list sent here carries
it again — someone who joined later or cleared the chat gets it back
with the next list anyone asks for.

The chat stays clean: the «🔧 Ремонты» line the tap sends is removed, and
so is the previous list — the fresh one is always the last message.
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup

from bot.quick_actions import BTN_MY_REPAIRS, BTN_REPAIRS_LIST
from core import repair_digest
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()

GROUP_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_REPAIRS_LIST), KeyboardButton(text=BTN_MY_REPAIRS)]], resize_keyboard=True, is_persistent=True,
)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
# The list last sent to each chat/topic, to take it away when the next one
# comes. In memory: after a restart one old list simply stays in the chat.
_last_list: dict[tuple[int, int | None], list[int]] = {}


async def send_list(message: Message, store, statuses: tuple[str, ...] = repair_digest.OPEN_STATUSES) -> None:
    """The list into the chat (and topic) the message came from, with the
    button attached. Outside a group the keyboard is left alone — a DM has
    the quick-action keyboard of its own."""
    in_group = message.chat.type != "private"
    topics = {str(store.staff_group_chat_id): store.repair_topic_id} if store.staff_group_chat_id else {}
    with get_conn(store.db_path) as conn:
        chunks = repair_digest.build(conn, store.location_id, statuses=statuses, prefer_chat_id=message.chat.id, topics=topics)
    place = (message.chat.id, message.message_thread_id if message.is_topic_message else None)
    for old in _last_list.pop(place, []) if in_group else []:
        try:
            await message.bot.delete_message(message.chat.id, old)
        except TelegramAPIError:
            pass
    sent = []
    for index, chunk in enumerate(chunks):
        last = index == len(chunks) - 1
        reply = await message.answer(chunk, link_preview_options=_NO_PREVIEW, reply_markup=GROUP_KEYBOARD if in_group and last else None)
        sent.append(reply.message_id)
    if in_group:
        _last_list[place] = sent


@router.message(F.text == BTN_REPAIRS_LIST, F.chat.type.in_({"group", "supergroup"}))
async def pressed(message: Message) -> None:
    store = store_for_chat_id(message.chat.id)
    if not store:
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass   # not an admin here — the line just stays
    await send_list(message, store)
