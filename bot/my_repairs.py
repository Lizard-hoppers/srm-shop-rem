"""«📋 Мои ремонты» (26.10, Павел) — each master's own repairs not yet
handed out, by status, the names linking to the cards.

Three places, because most masters of Гибрид are not linked to the CRM by
their Telegram (the bot doesn't know them in a DM):
  * the master's DM with the bot — for a master who is linked: the button
    is the first row of his keyboard (bot.quick_actions.keyboard_for);
  * his own group (staff.personal_chat_id) — whoever presses it there,
    it is that group's master's list;
  * the точка's work group, next to «🔧 Ремонты» — in a master's topic
    (staff.topic_id) his list, elsewhere the list of whoever pressed, if
    he is a linked master."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup

from bot import quick_actions as qa
from core import auth as core_auth
from core import repair_digest
from core.storage import get_conn
from core.stores import load_stores, store_for_chat_id

router = Router()

PERSONAL_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=qa.BTN_MY_REPAIRS)]], resize_keyboard=True, is_persistent=True,
)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _db() -> str:
    return load_stores()[0].db_path


def master_for(chat_id: int, chat_type: str, thread_id: int | None, user_id: int | None):
    """Whose list a press is asking for — or None."""
    with get_conn(_db()) as conn:
        if chat_type != "private":
            own = conn.execute(
                "SELECT * FROM staff WHERE personal_chat_id = ? AND role = 'master' AND active = 1", (chat_id,),
            ).fetchone()
            if own:
                return own
            store = store_for_chat_id(chat_id)
            if store and thread_id:
                topic = conn.execute(
                    """SELECT * FROM staff WHERE topic_id = ? AND role = 'master' AND active = 1
                       AND (location_id IS NULL OR location_id = ?)""",
                    (thread_id, store.location_id),
                ).fetchone()
                if topic:
                    return topic
        staff = core_auth.get_staff_by_telegram_id(conn, user_id) if user_id else None
        return staff if staff and staff["active"] else None


def chunks_for(master) -> list[str]:
    """His open repairs at every точка, the links into the work group of
    his точка when there is one."""
    stores = load_stores()
    home = next((s for s in stores if master["location_id"] and s.location_id == master["location_id"]), None)
    group = home.staff_group_chat_id if home and home.staff_group_chat_id else None
    topics = {str(s.staff_group_chat_id): s.repair_topic_id for s in stores if s.staff_group_chat_id}
    with get_conn(_db()) as conn:
        return repair_digest.build(conn, None, master_id=master["id"], prefer_chat_id=group, topics=topics,
                                   heading=f"Ремонты — {master['name']}")


async def _send(message: Message, chunks: list[str], keyboard) -> None:
    for index, chunk in enumerate(chunks):
        await message.answer(chunk, link_preview_options=_NO_PREVIEW,
                             reply_markup=keyboard if index == len(chunks) - 1 else None)


@router.message(F.text == qa.BTN_MY_REPAIRS, F.chat.type == "private")
async def my_repairs_dm(message: Message) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    _store, staff = resolved
    await qa._safe_delete_many(message.bot, message.chat.id, [message.message_id])
    await _send(message, chunks_for(staff), qa.keyboard_for(staff))


@router.message(F.text == qa.BTN_MY_REPAIRS, F.chat.type.in_({"group", "supergroup"}))
async def my_repairs_group(message: Message) -> None:
    from bot import repairs_button

    thread = message.message_thread_id if message.is_topic_message else None
    master = master_for(message.chat.id, message.chat.type, thread, message.from_user.id if message.from_user else None)
    try:
        await message.delete()
    except TelegramAPIError:
        pass  # not an admin here — the line just stays
    store = store_for_chat_id(message.chat.id)
    keyboard = repairs_button.group_keyboard(store) if store else PERSONAL_KEYBOARD
    if not master:
        await message.answer("Чьи ремонты? Нажмите «📋 Мои ремонты» в своей теме или своей группе.", reply_markup=keyboard)
        return
    await _send(message, chunks_for(master), keyboard)
