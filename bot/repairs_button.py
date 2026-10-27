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
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup,
)

from bot.quick_actions import BTN_MY_REPAIRS, BTN_REPAIRS_LIST
from core import auth as core_auth
from core import repair_digest
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()

GROUP_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_REPAIRS_LIST), KeyboardButton(text=BTN_MY_REPAIRS)]], resize_keyboard=True, is_persistent=True,
)
# 26.10 (Павел): a master sees only his own repairs, never the точка's. A group
# has one keyboard for all its topics, so the group of a simple-mode точка
# (Гибрид — its masters sit there) keeps «🔧 Ремонты» alone, and the button
# itself tells people apart (sees_all): the точка's list for those below, a
# master his own (also in his topic). «📋 Мои ремонты» — his group and DM.
REPAIRS_ONLY_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_REPAIRS_LIST)]], resize_keyboard=True, is_persistent=True,
)
ALL_REPAIRS_ROLES = ("owner", "admin", "manager", "storekeeper")


def group_keyboard(store) -> ReplyKeyboardMarkup:
    return REPAIRS_ONLY_KEYBOARD if getattr(store, "simple_flow", False) else GROUP_KEYBOARD


def sees_all(staff, store=None) -> bool:
    """Whether this person may see the точка's whole list of repairs: never a
    master; someone not in the CRM only where no masters sit (a точка that
    isn't in the simple mode — 007's masters' group is the owners')."""
    if staff:
        return staff["role"] in ALL_REPAIRS_ROLES
    return store is not None and not getattr(store, "simple_flow", False)
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
        reply = await message.answer(chunk, link_preview_options=_NO_PREVIEW, reply_markup=group_keyboard(store) if in_group and last else None)
        sent.append(reply.message_id)
    if in_group:
        _last_list[place] = sent


# 27.10 (Павел): the same «🔧 Ремонты» pinned in the work group's feed, under
# a message — it stays at the top whatever the keyboard. Who sees what is as
# with the keyboard button (sees_all).
PINNED_BUTTON = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BTN_REPAIRS_LIST, callback_data="all_repairs")]])


@router.callback_query(F.data == "all_repairs")
async def pinned_pressed(callback: CallbackQuery) -> None:
    message = callback.message
    store = store_for_chat_id(message.chat.id) if message else None
    if not store:
        await callback.answer()
        return
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not sees_all(staff, store):
        from bot import my_repairs

        if staff and staff["role"] == "master":
            for chunk in my_repairs.chunks_for(staff):
                await message.answer(chunk, link_preview_options=_NO_PREVIEW)
            await callback.answer()
        else:
            await callback.answer("Список ремонтов точки — для администраторов и менеджеров. Свои ремонты — кнопка в вашей теме.",
                                  show_alert=True)
        return
    topics = {str(store.staff_group_chat_id): store.repair_topic_id} if store.staff_group_chat_id else {}
    with get_conn(store.db_path) as conn:
        chunks = repair_digest.build(conn, store.location_id, prefer_chat_id=message.chat.id, topics=topics)
    place = (message.chat.id, message.message_thread_id if message.is_topic_message else None)
    for old in _last_list.pop(place, []):
        try:
            await message.bot.delete_message(message.chat.id, old)
        except TelegramAPIError:
            pass
    _last_list[place] = [(await message.answer(chunk, link_preview_options=_NO_PREVIEW)).message_id for chunk in chunks]
    await callback.answer()


@router.message(F.text == BTN_REPAIRS_LIST, F.chat.type.in_({"group", "supergroup"}))
async def pressed(message: Message) -> None:
    store = store_for_chat_id(message.chat.id)
    if not store:
        return
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id) if message.from_user else None
    if not sees_all(staff, store):
        # A master (or someone not in the CRM) pressing it on an old keyboard
        # gets his own list — the точка's isn't his business.
        from bot import my_repairs

        await my_repairs.my_repairs_group(message)
        return
    try:
        await message.delete()
    except TelegramAPIError:
        pass   # not an admin here — the line just stays
    await send_list(message, store)
