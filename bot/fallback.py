"""The last router: a text in a private chat that nothing else handled.

In practice that is a tap on a button of an OLD reply keyboard — Telegram
keeps showing a keyboard until the bot sends a new one, and a button
whose label has since changed («🔧 Ремонт» → «🔧 Принять ремонт») matches
no handler any more. Silence there reads as «бот не работает» (06.10), so
a staff member gets the current keyboard and a line saying why.

Must be included after every other router (bot/bot.py) — it takes
anything that reaches it.
"""
from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.types import Message

from bot.quick_actions import _resolve_staff_for_dm, keyboard_for
from core import store_access

router = Router()
logger = logging.getLogger(__name__)


@router.message(F.chat.type == "private", F.text, StateFilter(None))
async def unknown_text(message: Message) -> None:
    if not store_access.accessible_stores(message.from_user.id):
        return
    # What exactly nobody handled — the only way to tell a stale button
    # from a real gap when someone reports «кнопка не работает».
    logger.info("unhandled DM text from staff %s: %r", message.from_user.id, message.text[:60])
    await message.answer(
        "Кнопки меню обновились — вот актуальные. Нажмите нужную ещё раз.",
        reply_markup=keyboard_for((_resolve_staff_for_dm(message.from_user.id) or (None, None))[1]),
    )
