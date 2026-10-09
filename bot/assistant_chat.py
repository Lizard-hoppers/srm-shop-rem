"""Talking to the bot by name (09.10): a message that starts with «бот» —
«бот, найди заказ по 13 айфону и пришли в чат», «бот, сколько у нас
ремонтов», «бот, что с Poco» — in a work group or in the DM is a request
to it, and it answers in the same chat. In the DM a staff member can also
just say it: a voice message there is a request too.

The request goes to the AI assistant (core.ai_agent), which looks things
up in the base through its tools and may ask for a repair's card or the
ready-made list of repairs to be posted — that part is done here. The
assistant changes nothing in the books.

One thing is handled before the assistant is asked: a status told to the
bot — «бот, 13 про мах выдан, наличными», «бот, поко взял». It goes the
same way a reply to the repair's card does (_status_said below).

Who may ask: in a точка's work groups — anyone in the group; in the DM —
CRM staff. Money (касса, прибыль, долги) is answered only to an owner or
admin, wherever they ask — mind that an answer in a group is read by the
whole group.

If the model is unavailable the bot falls back to what it can do without
it: the list of repairs for a question about repairs, a line of help
otherwise — never silence.

Registered ahead of the note handler (bot/repair_attachments.py) — «бот,
…» is a request even when it is sent as a reply to a card.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import StateFilter
from aiogram.types import (
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
    WebAppInfo,
)

from bot import reminder_flow
from bot.miniapp_links import crm_link
from bot.quick_actions import _resolve_staff_for_dm
from bot.repair_actions import _sync_after_change
from bot.repair_attachments import apply_status
from core import agent_tools
from core import ai_agent
from core import ai_notes
from core import auth as core_auth
from core import documents as core_documents
from core import notify as core_notify
from core import repair_digest
from core import repairs as core_repairs
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()
logger = logging.getLogger(__name__)

# «бот», «Бот,», «бот:» … as a word of its own at the very start — not
# «ботинок», not a sentence that merely mentions a bot.
_CALL = re.compile(r"^\s*бот(?![\w])[\s,.:;!?—-]*", re.IGNORECASE)

_MONEY_ROLES = ("owner", "admin")
_REPAIR_WORDS = ("ремонт", "трубк", "телефон", "заказ", "аппарат")
_STATUS_WORDS = (
    (("готов",), "ready"),
    (("в работе", "делает", "делаем", "чин"), "in_progress"),
    (("нов", "не взят", "очеред"), "new"),
)
_HELP = (
    "Спросите меня, например:\n"
    "• «бот, сколько у нас ремонтов» / «какие готовы»\n"
    "• «бот, найди ремонт по 13 айфону и пришли в чат»\n"
    "• «бот, что с ремонтом Poco» · «бот, найди клиента 0671234567»\n"
    "• «бот, есть ли в наличии дисплей на iPhone 13»\n"
    "• «бот, напомни завтра в 10:20 позвонить клиенту» · «бот, какие напоминания»\n"
    "• «бот, 13 про мах выдан, наличными» · «бот, РК-45 взял»"
)
_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


_MD_LINK = re.compile(r"\[([^\]\n]{1,80})\]\((https://t\.me/c/[0-9/]+)\)")


def to_html(answer: str) -> str:
    """The assistant's plain text as safe Telegram HTML. Everything is
    escaped; the one thing let through is a link to a repair's card it
    was asked to write as [название](https://t.me/c/…) — that becomes a
    clickable name. Stray Markdown emphasis is dropped."""
    text = html.escape(answer.replace("**", "").replace("__", ""), quote=False)
    text = _MD_LINK.sub(lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', text)
    return "\n".join(line.rstrip() for line in text.split("\n"))


def question_of(text: str | None) -> str | None:
    """What was asked, if the message is addressed to the bot; else None."""
    match = _CALL.match(text or "")
    return (text or "")[match.end():].strip() if match else None


def statuses_asked(question: str) -> tuple[str, ...]:
    """Which statuses the question narrows the list to — all open ones
    when it names none."""
    lowered = question.lower()
    picked = tuple(status for words, status in _STATUS_WORDS if any(word in lowered for word in words))
    return picked or repair_digest.OPEN_STATUSES


def _topics(store) -> dict:
    return {str(store.staff_group_chat_id): store.repair_topic_id} if store.staff_group_chat_id else {}


def _ask_blocking(store, question: str, can_money: bool, chat_id: int) -> tuple[str, list[tuple]]:
    with get_conn(store.db_path) as conn:
        return ai_agent.ask(
            conn, question, location_id=store.location_id, point_name=store.name, can_money=can_money,
            chat_id=str(chat_id), topics=_topics(store),
        )


async def _send_list(message: Message, store, statuses: tuple[str, ...]) -> None:
    with get_conn(store.db_path) as conn:
        chunks = repair_digest.build(
            conn, store.location_id, statuses=statuses, prefer_chat_id=message.chat.id, topics=_topics(store),
        )
    for chunk in chunks:
        await message.answer(chunk, link_preview_options=_NO_PREVIEW)


async def _send_card(message: Message, store, staff, order_id: int) -> None:
    """The repair's card into this chat — whole, as the original went
    out: the device's photo with the card as its caption, when the repair
    has a photo. In a work group it is a live card: its buttons work, it
    is kept up to date with the repair, and a reply to it becomes a note.
    In the DM the group buttons would do nothing — there it comes with
    «Открыть в CRM»."""
    with get_conn(store.db_path) as conn:
        repair = core_repairs.get_repair(conn, order_id)
        if not repair:
            return
        text, keyboard = core_repairs.card(conn, order_id)
    if message.chat.type == "private":
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Открыть в CRM", web_app=WebAppInfo(url=crm_link(f"/repairs/{order_id}", staff["id"], store.id)),
        )]]) if staff else None
    else:
        markup = InlineKeyboardMarkup.model_validate(keyboard)

    photo_path = os.path.join(core_repairs.PHOTO_DIR, repair["device_photo_path"]) if repair["device_photo_path"] else None
    has_photo = bool(photo_path) and os.path.exists(photo_path)
    sent = None
    if has_photo:
        try:
            sent = await message.answer_photo(FSInputFile(photo_path), caption=core_notify._as_caption(text), reply_markup=markup)
        except TelegramAPIError as exc:
            # A photo Telegram won't take must not cost the card itself.
            logger.warning("card of repair %s sent without its photo: %s", order_id, exc)
            has_photo = False
    if sent is None:
        sent = await message.answer(text, reply_markup=markup)
    if message.chat.type != "private":
        with get_conn(store.db_path) as conn:
            core_repairs.save_order_messages(conn, order_id, [(str(message.chat.id), sent.message_id, "copy", has_photo)])


# A request that asks something is never a status report: «какие готовы?»
# must list, not mark something ready.
_ASKING = ("?", "какие", "какой", "какая", "сколько", "что ", "где", "когда", "кто", "найди", "найти", "покажи",
           "пришли", "выстав", "скинь", "список", "есть ли", "почему", "чей", "чья")


def looks_like_question(text: str) -> bool:
    lowered = f"{text.casefold()} "
    return any(marker in lowered for marker in _ASKING)


async def _status_said(message: Message, store, staff, question: str) -> bool:
    """«Бот, 13 про мах выдан, наличными» — a status told to the bot. The
    status and the way of paying are read by the same reading a reply to
    a card gets (core.ai_notes.analyze — tried on live wording); WHICH
    repair is settled without the model (agent_tools.repairs_for_status).
    Exactly one repair fits every word — it is moved, with «Вернуть»
    under the answer; none, several or only a near miss — the bot says
    so and changes nothing. Returns True if the
    message was a status report (handled here), False to let the
    assistant answer it."""
    if looks_like_question(question):
        return False
    try:
        read = await asyncio.to_thread(ai_notes.analyze, question, "")
    except ai_notes.NoteAiError:
        return False
    status = read["status"]
    if not status:
        return False
    target = core_repairs.STATUS_LABELS[status]
    with get_conn(store.db_path) as conn:
        found, close = agent_tools.repairs_for_status(conn, store.location_id, question, status)
        lines = [
            f"• {html.escape(' '.join(filter(None, [r['brand'], r['model']])) or r['device_type'])} — "
            f"{core_documents.label('repair', r['id'])}, {core_repairs.STATUS_LABELS[r['status']]}"
            for r in (found or close)[:8]
        ]
        already = [] if (found or close) else agent_tools.already_there(conn, store.location_id, question, status)
    how = "Назовите номер (например «бот, РК-48 выдан») или ответьте этим словом на карточку нужного."
    if already:
        numbers = ", ".join(core_documents.label("repair", r["id"]) for r in already)
        await message.reply(f"{numbers} — уже «{target}», менять нечего.")
        return True
    if not found and not close:
        await message.reply(f"Не нашёл ремонт, который можно перевести в «{target}». " + how)
        return True
    if not found:
        await message.reply(
            f"Точно такого ремонта нет — ближайшие, статус не трогаю:\n" + "\n".join(lines) + "\n" + how
        )
        return True
    if len(found) > 1:
        await message.reply(
            f"Под это подходят несколько ремонтов — не угадываю, какой из них «{target}»:\n" + "\n".join(lines) + "\n" + how
        )
        return True
    order_id = found[0]["id"]
    result = apply_status(store, order_id, status, staff, payment=read.get("payment"))
    if result:
        await message.reply(result[0], reply_markup=result[1])
        _sync_after_change(order_id, store.db_path)
    return True


async def _handle(message: Message, store, staff, question: str) -> None:
    if not question:
        await message.reply(_HELP)
        return
    # «напомни …» — before anything else looks at the words: «напомни
    # отдать 13 про мах» must set a reminder, not hand the phone over.
    if reminder_flow.is_list_request(question):
        await reminder_flow.send_list(message, store)
        return
    if reminder_flow.is_reminder_request(question):
        await reminder_flow.set_from_request(message, store, staff, question)
        return
    if await _status_said(message, store, staff, question):
        return
    can_money = bool(staff) and staff["role"] in _MONEY_ROLES
    try:
        await message.bot.send_chat_action(message.chat.id, "typing", message_thread_id=message.message_thread_id)
    except TelegramAPIError:
        pass
    try:
        text, actions = await asyncio.to_thread(_ask_blocking, store, question, can_money, message.chat.id)
    except ai_agent.AgentError as exc:
        logger.warning("assistant unavailable (%s) — answering without it: %r", exc, question[:80])
        # Without the model: the one thing that needs none of it.
        about_repairs = any(word in question.lower() for word in _REPAIR_WORDS)
        if about_repairs or statuses_asked(question) != repair_digest.OPEN_STATUSES:
            await _send_list(message, store, statuses_asked(question))
        else:
            await message.reply("ИИ сейчас недоступен — попробуйте через минуту.\n\n" + _HELP)
        return
    logger.info("assistant: %r -> %d chars, actions %s", question[:80], len(text), actions)
    if text:
        await message.reply(to_html(text), link_preview_options=_NO_PREVIEW)
    for kind, value in actions:
        if kind == "open_repairs":
            await _send_list(message, store, value)
        elif kind == "repair_card":
            await _send_card(message, store, staff, value)



@router.message(F.text.func(lambda text: question_of(text) is not None), StateFilter(None))
async def asked(message: Message) -> None:
    if message.chat.type == "private":
        resolved = _resolve_staff_for_dm(message.from_user.id)
        if not resolved:
            return
        store, staff = resolved
    else:
        store = store_for_chat_id(message.chat.id)
        if not store:
            return
        with get_conn(store.db_path) as conn:
            staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
    await _handle(message, store, staff, question_of(message.text) or "")


@router.message(F.voice, F.chat.type == "private", StateFilter(None))
async def asked_by_voice(message: Message) -> None:
    """A voice message to the bot in the DM, outside any dialog: a
    request said aloud. (In a group a voice message is a note when it
    replies to a card and nobody's business otherwise.)"""
    resolved = _resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    try:
        file = await message.bot.get_file(message.voice.file_id)
        buf = await message.bot.download_file(file.file_path)
        heard = await asyncio.to_thread(ai_notes.transcribe, buf.read())
    except (ai_notes.NoteAiError, TelegramAPIError) as exc:
        logger.warning("voice request not transcribed: %s", exc)
        await message.reply("Не смог расшифровать голосовое — напишите текстом, начав со слова «бот».")
        return
    await message.reply(f"🎤 {html.escape(heard)}")
    await _handle(message, store, staff, question_of(heard) if question_of(heard) is not None else heard)
