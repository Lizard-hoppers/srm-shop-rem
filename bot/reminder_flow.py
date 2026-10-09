"""Напоминания in the bot (core.reminders): setting one from a request —
«бот, напомни завтра в 10:20 спросить Андрея про готовность», «бот,
напомни в пятницу в 15:00 отдать этот заказ» said in reply to a repair's
card — and posting it into the chat when its time comes.

Where it goes: asked in a work group — that same chat (and topic); asked
in the DM — the точка's staff group, because the point of a reminder is
that the people at the counter see it (the DM itself if the точка has no
group). A reminder about a repair goes out as a reply to that repair's
card, so the card is one tap away.

The bot sets the reminder at once and says exactly what it understood —
when and about what — with «Отменить» under it: a misheard time is
caught by reading that line, not by a question before.
"""
from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from core import agent_tools
from core import ai_notes
from core import documents as core_documents
from core import repair_digest
from core import repair_notes as core_notes
from core import reminders as core_reminders
from core import repairs as core_repairs
from core.storage import get_conn
from core.stores import load_stores, store_for_chat_id
from core.timefmt import HUMAN_FORMAT, KYIV

router = Router()
logger = logging.getLogger(__name__)

_SET_WORDS = ("напомни", "напомин", "напоминан", "нагадай", "нагадув")
_LIST_WORDS = ("какие", "список", "покажи", "все ", "что стоит", "активн")


def is_reminder_request(question: str) -> bool:
    lowered = question.casefold()
    return any(word in lowered for word in _SET_WORDS)


def is_list_request(question: str) -> bool:
    lowered = f"{question.casefold()} "
    return is_reminder_request(question) and any(word in lowered for word in _LIST_WORDS) and "напомни " not in lowered


def _device(repair) -> str:
    return " ".join(filter(None, [repair["brand"], repair["model"]])) or repair["device_type"] or "устройство"


def _target(message: Message, store) -> tuple[str, int | None]:
    """(chat, topic) the reminder will be posted into — see the module docstring."""
    if message.chat.type != "private":
        return str(message.chat.id), message.message_thread_id if message.is_topic_message else None
    if store.staff_group_chat_id:
        return str(store.staff_group_chat_id), None
    return str(message.chat.id), None


def _repair_for(conn, store, message: Message, named: str | None) -> int | None:
    """The repair the reminder is about: the card (or a note on it) the
    request replies to — «вот этот заказ» — else the one the words name,
    if exactly one open repair carries all of them. No guessing."""
    if message.reply_to_message and message.chat.type != "private":
        chat_id, replied = str(message.chat.id), message.reply_to_message.message_id
        order_id = core_repairs.find_order_by_message(conn, chat_id, replied) or core_notes.find_order_by_note_message(conn, chat_id, replied)
        if order_id:
            return order_id
    if not named:
        return None
    numbered = agent_tools._REPAIR_NUMBER.search(named)
    rows = [r for r in core_repairs.list_repairs(conn, location_id=store.location_id) if r["status"] not in ("issued", "cancelled")]
    if numbered:
        found = [r for r in rows if r["id"] == int(numbered.group(1))]
    else:
        found = [r for r in rows if agent_tools.matches(named, r["device_type"], r["brand"], r["model"], r["client_name"])]
    return found[0]["id"] if len(found) == 1 else None


def _cancel_markup(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отменить напоминание", callback_data=f"rem_cancel:{reminder_id}")]])


async def set_from_request(message: Message, store, staff, question: str) -> None:
    """«бот, напомни …» — set it, or say plainly why not."""
    now = datetime.now(KYIV)
    try:
        read = await asyncio.to_thread(ai_notes.parse_reminder, question, now)
    except ai_notes.NoteAiError as exc:
        logger.warning("reminder request not read: %s", exc)
        await message.reply("Не смог разобрать напоминание — ИИ сейчас недоступен. Попробуйте через минуту.")
        return
    chat_id, thread_id = _target(message, store)
    try:
        due = core_reminders.resolve_due(read["date"], read["time"], now)
        with get_conn(store.db_path) as conn:
            order_id = _repair_for(conn, store, message, read["repair"])
            repair = core_repairs.get_repair(conn, order_id) if order_id else None
            reminder_id = core_reminders.create(
                conn, text=read["what"] or "", due=due, chat_id=chat_id, thread_id=thread_id,
                location_id=store.location_id, order_id=order_id, staff_id=staff["id"] if staff else None,
                author_name=message.from_user.full_name if message.from_user else None,
            )
    except core_reminders.ReminderError as exc:
        await message.reply(str(exc))
        return
    about = f"\n🔧 {core_documents.label('repair', order_id)} · {html.escape(_device(repair))}" if repair else ""
    where = "" if chat_id == str(message.chat.id) else "\nПришлю в рабочую группу."
    await message.reply(
        f"⏰ Напомню <b>{due.strftime(HUMAN_FORMAT)}</b> (по Киеву): {html.escape(read['what'])}{about}{where}",
        reply_markup=_cancel_markup(reminder_id),
    )


async def send_list(message: Message, store) -> None:
    with get_conn(store.db_path) as conn:
        rows = core_reminders.pending(conn, store.location_id)
    if not rows:
        await message.reply("Активных напоминаний нет.")
        return
    lines = ["⏰ <b>Напоминания</b>"]
    for row in rows:
        about = f" · {core_documents.label('repair', row['order_id'])}" if row["order_id"] else ""
        lines.append(f"• {core_reminders.kyiv(row['due_at'])} — {html.escape(row['text'])}{about} <i>({html.escape(row['author'])})</i>")
    await message.reply("\n".join(lines))


@router.callback_query(F.data.startswith("rem_cancel:"))
async def cancel_reminder(callback: CallbackQuery) -> None:
    store = store_for_chat_id(callback.message.chat.id) or (load_stores() or [None])[0]
    reminder_id = int(callback.data.split(":")[1])
    with get_conn(store.db_path) as conn:
        cancelled = core_reminders.cancel(conn, reminder_id)
    try:
        if cancelled:
            await callback.message.edit_text("✖️ Напоминание отменено.")
        else:
            await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await callback.answer("Отменено" if cancelled else "Оно уже отправлено или отменено")


@router.callback_query(F.data.startswith("rem_snooze:"))
async def snooze_reminder(callback: CallbackQuery) -> None:
    store = store_for_chat_id(callback.message.chat.id) or (load_stores() or [None])[0]
    reminder_id = int(callback.data.split(":")[1])
    with get_conn(store.db_path) as conn:
        again_at = core_reminders.snooze(conn, reminder_id, 60)
    if not again_at:
        await callback.answer("Уже отложено")
        return
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await callback.answer(f"Напомню ещё раз {again_at}", show_alert=True)


# ---- доставка ----

def _due_blocking(db_path: str) -> list[dict]:
    """Everything due, with what is needed to post it — read in one go,
    off the event loop."""
    with get_conn(db_path) as conn:
        result = []
        for row in core_reminders.due_now(conn):
            item = {"row": dict(row), "reply_to": None, "thread_id": row["thread_id"], "about": ""}
            if row["order_id"]:
                repair = core_repairs.get_repair(conn, row["order_id"])
                if repair:
                    item["about"] = f"\n🔧 {core_documents.label('repair', repair['id'])} · {html.escape(_device(repair))}"
                    card = conn.execute(
                        """SELECT message_id, kind FROM repair_order_messages WHERE order_id = ? AND chat_id = ?
                           ORDER BY (kind = 'copy'), id LIMIT 1""",
                        (row["order_id"], row["chat_id"]),
                    ).fetchone()
                    if card:
                        item["reply_to"] = card["message_id"]
                    else:
                        links = repair_digest._links(conn, [repair["id"]], row["chat_id"], {})
                        if repair["id"] in links:
                            item["about"] += f' · <a href="{links[repair["id"]]}">карточка</a>'
            result.append(item)
        return result


def _mark_blocking(db_path: str, reminder_id: int, sent: bool) -> None:
    with get_conn(db_path) as conn:
        (core_reminders.mark_sent if sent else core_reminders.mark_failed)(conn, reminder_id)


async def deliver_due(bot) -> int:
    """Post every reminder whose time has come. Called on a short timer
    (bot/bot.py). A reminder Telegram won't take stays pending for the
    next tick. Returns how many went out."""
    stores = load_stores()
    if not stores:
        return 0
    db_path, sent = stores[0].db_path, 0
    for item in await asyncio.to_thread(_due_blocking, db_path):
        row = item["row"]
        text = f"⏰ <b>Напоминание</b>\n{html.escape(row['text'])}{item['about']}\n<i>поставил(а) {html.escape(row['author'])}</i>"
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔁 Напомнить через час", callback_data=f"rem_snooze:{row['id']}")]])
        try:
            await bot.send_message(
                int(row["chat_id"]), text, reply_markup=markup,
                # A reply lands in its card's topic by itself; otherwise the topic the request came from.
                message_thread_id=None if item["reply_to"] else item["thread_id"],
                reply_parameters=ReplyParameters(message_id=item["reply_to"], allow_sending_without_reply=True) if item["reply_to"] else None,
            )
            ok = True
        except TelegramAPIError as exc:
            logger.warning("reminder %s not delivered: %s", row["id"], exc)
            ok = False
        await asyncio.to_thread(_mark_blocking, db_path, row["id"], ok)
        sent += ok
    return sent
