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
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, LinkPreviewOptions, Message, ReplyKeyboardMarkup,
)

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
# The list last sent into each topic by its pinned button — taken away when
# the next one comes. In memory: after a restart one old list stays.
_last_in_topic: dict[tuple[int, int | None], list[int]] = {}


def topic_button(master_id: int) -> InlineKeyboardMarkup:
    """27.10 (Павел): a group has one keyboard for all its topics, so a
    master's topic gets its own «📋 Мои ремонты» as a pinned message with
    this button under it."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=qa.BTN_MY_REPAIRS, callback_data=f"my_repairs:{master_id}"),
    ]])


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


_ONLY_HERE = ("В этой группе бот знает ваши ремонты:\n"
              "• «бот, мои ремонты» — все незакрытые\n"
              "• «бот, РК-1189» или «бот, что с 15 про макс» — карточка ремонта\n"
              "Остальное — в группе «РМТ».")


def _group_master(message: Message):
    """The master whose own group this is — or None (not a master's group)."""
    if message.chat.type not in ("group", "supergroup") or store_for_chat_id(message.chat.id):
        return None
    with get_conn(_db()) as conn:
        return conn.execute(
            "SELECT * FROM staff WHERE personal_chat_id = ? AND role = 'master' AND active = 1", (message.chat.id,),
        ).fetchone()


def _asked_here(message: Message) -> bool:
    from bot.assistant_chat import question_of

    return question_of(message.text) is not None and _group_master(message) is not None


def answer_in_group(master, question: str) -> list[str]:
    """«бот, …» in a master's own group (27.10): only about HIS repairs — the
    точка's are not his business, so no assistant here, just a lookup."""
    import html
    import re

    from core import repairs as core_repairs
    from core import simple_flow
    from core.agent_tools import matches

    text = question.strip()
    if not text or re.search(r"мои|мой|у меня|ремонт|список|что есть|все", text, re.IGNORECASE) and not re.search(r"\d{3,}|рк", text, re.IGNORECASE):
        return chunks_for(master)
    with get_conn(_db()) as conn:
        mine = [r for r in core_repairs.list_repairs(conn, master_id=master["id"]) if r["status"] in repair_digest.OPEN_STATUSES]
        number = re.search(r"(?:рк|№|#)?\s*-?\s*(\d{2,6})", text, re.IGNORECASE)
        found = [r for r in mine if number and r["id"] == int(number.group(1))]
        if not found:
            words = re.sub(r"^(?:что\s+с|найди|покажи|пришли|карточк\w*)\s+", "", text, flags=re.IGNORECASE)
            found = [r for r in mine if matches(words, r["brand"], r["model"], r["device_type"])]
        if len(found) == 1:
            repair = core_repairs.get_repair(conn, found[0]["id"])
            card = simple_flow.master_text(repair, core_repairs.roadmap(conn, repair))
            link = simple_flow._main_link(conn, repair["id"])
            return [card + (f'\n\n<a href="{link}">🔗 Карточка в группе</a>' if link else "")]
    if len(found) > 1:
        lines = [f"• {html.escape(' '.join(filter(None, [r['brand'], r['model']])) or r['device_type'] or 'устройство')} — "
                 f"{core_repairs.STATUS_LABELS[r['status']]} · РК-{r['id']:03d}" for r in found[:10]]
        return ["Подходят несколько ваших ремонтов — назовите номер РК:\n" + "\n".join(lines)]
    return ["Среди ваших незакрытых ремонтов такого нет.\n\n" + _ONLY_HERE]


@router.message(F.text, F.func(_asked_here))
async def asked_in_master_group(message: Message) -> None:
    from bot.assistant_chat import question_of

    master = _group_master(message)
    for chunk in answer_in_group(master, question_of(message.text) or ""):
        await message.reply(chunk, link_preview_options=_NO_PREVIEW)


@router.message(F.migrate_to_chat_id)
async def group_upgraded(message: Message) -> None:
    """«Группа стала супергруппой» — the service message in the old group:
    the new number goes everywhere the old one was (core.chat_migration)."""
    from core import chat_migration

    chat_migration.remap(message.chat.id, message.migrate_to_chat_id, _db())


@router.callback_query(F.data.startswith("my_repairs:"))
async def my_repairs_pinned(callback: CallbackQuery) -> None:
    """The pinned button in a master's topic: that master's list, into the topic."""
    with get_conn(_db()) as conn:
        master = conn.execute(
            "SELECT * FROM staff WHERE id = ? AND role = 'master' AND active = 1", (int(callback.data.split(":", 1)[1]),),
        ).fetchone()
    if not master or not callback.message:
        await callback.answer("Этого мастера больше нет.", show_alert=True)
        return
    message = callback.message
    place = (message.chat.id, message.message_thread_id if message.is_topic_message else None)
    for old in _last_in_topic.pop(place, []):
        try:
            await message.bot.delete_message(message.chat.id, old)
        except TelegramAPIError:
            pass
    sent = []
    for chunk in chunks_for(master):
        reply = await message.answer(chunk, link_preview_options=_NO_PREVIEW)
        sent.append(reply.message_id)
    _last_in_topic[place] = sent
    await callback.answer()


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
