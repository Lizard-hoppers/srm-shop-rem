"""Talking to the bot by name (09.10): a message that starts with «бот» —
«бот, сколько у нас ремонтов?» — in a work group or in the DM is a
question to it, and it answers in the same chat.

What it can answer now: the list of repairs not yet выданные
(core.repair_digest) — all of them, or only «готовые» / «в работе» /
«новые» if the question says so. Anything else addressed to it gets a
short «вот что я умею», never silence.

Matching is by plain words on purpose: the question is one of a handful,
and a list of repairs must be exactly what the base holds. Registered
ahead of the note handler (bot/repair_attachments.py) — «бот, …» is a
question even when it is sent as a reply to a card.
"""
from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import LinkPreviewOptions, Message

from bot.quick_actions import _resolve_staff_for_dm
from core import repair_digest
from core.storage import get_conn
from core.stores import store_for_chat_id

router = Router()

# «бот», «Бот,», «бот:» … as a word of its own at the very start — not
# «ботинок», not a sentence that merely mentions a bot.
_CALL = re.compile(r"^\s*бот(?![\w])[\s,.:;!?—-]*", re.IGNORECASE)

_REPAIR_WORDS = ("ремонт", "трубк", "телефон", "заказ", "аппарат")
_STATUS_WORDS = (
    (("готов",), "ready"),
    (("в работе", "делает", "делаем", "чин"), "in_progress"),
    (("нов", "не взят", "очеред"), "new"),
)
_HELP = (
    "Я на связи. Пока умею вот что:\n"
    "• «бот, сколько у нас ремонтов» — все невыданные ремонты: устройство, цена, статус, со ссылкой на карточку;\n"
    "• «бот, какие ремонты готовы» / «в работе» / «новые» — только с этим статусом."
)


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


@router.message(F.text.func(lambda text: question_of(text) is not None), StateFilter(None))
async def asked(message: Message) -> None:
    if message.chat.type == "private":
        resolved = _resolve_staff_for_dm(message.from_user.id)
        if not resolved:
            return
        store = resolved[0]
    else:
        store = store_for_chat_id(message.chat.id)
        if not store:
            return
    question = question_of(message.text) or ""
    # «какие готовы?» is about repairs too, without the word itself.
    about_repairs = any(word in question.lower() for word in _REPAIR_WORDS)
    if not about_repairs and statuses_asked(question) == repair_digest.OPEN_STATUSES:
        await message.reply(_HELP)
        return
    with get_conn(store.db_path) as conn:
        chunks = repair_digest.build(
            conn, store.location_id, statuses=statuses_asked(question), prefer_chat_id=message.chat.id,
            topics={str(store.staff_group_chat_id): store.repair_topic_id} if store.staff_group_chat_id else {},
        )
    for index, chunk in enumerate(chunks):
        send = message.reply if index == 0 else message.answer
        await send(chunk, link_preview_options=LinkPreviewOptions(is_disabled=True))
