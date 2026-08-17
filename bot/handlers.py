from __future__ import annotations

from aiogram import Router
from aiogram.filters import CommandStart
from aiogram.types import Message

from core import clients as core_clients
from core.storage import get_conn

router = Router()

WELCOME = (
    "Здравствуйте! Это бот сервис-центра.\n\n"
    "Сейчас через бота можно только оставить свои контакты — проверка статуса "
    "ремонта и каталог товаров подключатся в одной из следующих фаз проекта.\n\n"
    "Если у вас уже есть ремонт в работе, статус уточните у мастера напрямую."
)


@router.message(CommandStart())
async def start(message: Message) -> None:
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM clients WHERE telegram_id = ?", (message.from_user.id,)
        ).fetchone()
        if not existing:
            core_clients.create_client(
                conn,
                name=message.from_user.full_name,
                telegram_id=message.from_user.id,
                source="online",
            )
    await message.answer(WELCOME)
