from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot.config import MINIAPP_URL

router = Router()

WELCOME = (
    "Здравствуйте! Нажмите кнопку ниже, чтобы открыть CRM сервис-центра.\n\n"
    "Ваш Telegram ID: <code>{user_id}</code>\n"
    "Если доступ ещё не открыт — пришлите этот ID владельцу."
)


@router.message(CommandStart())
async def start(message: Message) -> None:
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Открыть CRM", web_app=WebAppInfo(url=MINIAPP_URL))]]
    )
    await message.answer(WELCOME.format(user_id=message.from_user.id), reply_markup=keyboard)
