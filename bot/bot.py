import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    MenuButtonWebApp,
    WebAppInfo,
)

from bot.config import BOT_TOKEN, MINIAPP_URL
from bot.channel_orders import router as channel_orders_router
from bot.handlers import router
from bot.purchase_photo import router as purchase_photo_router
from bot.quick_actions import router as quick_actions_router
from bot.repair_actions import router as repair_actions_router
from bot.repair_attachments import router as repair_attachments_router
from core.storage import init_db
from core.stores import load_stores

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    # Schema ready before the first update arrives — matters if this process
    # starts before the web one ever has. One base for every точка (06.10).
    init_db(load_stores()[0].db_path)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # MemoryStorage: the quick-intake FSM (bot/quick_actions.py) only needs
    # its state to survive between a staff member's own messages within one
    # sitting — in-process is enough, and a restart just means an
    # abandoned draft (nothing's written to the DB until the final
    # confirm), no persistence needed across process restarts.
    dp = Dispatcher(storage=MemoryStorage())
    # Before `router`: its bare CommandStart() matches every /start, deep
    # link or not — see bot/channel_orders.py.
    dp.include_router(channel_orders_router)
    dp.include_router(router)
    dp.include_router(quick_actions_router)
    dp.include_router(repair_actions_router)
    dp.include_router(repair_attachments_router)
    dp.include_router(purchase_photo_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="CRM", web_app=WebAppInfo(url=MINIAPP_URL))
    )
    # The chat menu button above (WebApp) is a separate thing from the "/"
    # suggestion popup — that one is driven purely by set_my_commands and
    # was never set here, so typing "/" showed nothing at all. /start goes
    # to private chats (that's the only place it does anything — see
    # bot/handlers.py's start()); /chatid to groups (its actual use case:
    # onboarding a new store's staff/masters group, see its own docstring).
    await bot.set_my_commands(
        [BotCommand(command="start", description="Открыть меню CRM")],
        scope=BotCommandScopeAllPrivateChats(),
    )
    await bot.set_my_commands(
        [BotCommand(command="chatid", description="Узнать chat_id этой группы")],
        scope=BotCommandScopeAllGroupChats(),
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
