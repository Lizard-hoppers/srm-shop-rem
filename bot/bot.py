import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import MenuButtonWebApp, WebAppInfo

from bot.config import BOT_TOKEN, MINIAPP_URL
from bot.handlers import router
from bot.purchase_photo import router as purchase_photo_router
from bot.repair_actions import router as repair_actions_router
from bot.repair_attachments import router as repair_attachments_router
from core.storage import init_db

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(repair_actions_router)
    dp.include_router(repair_attachments_router)
    dp.include_router(purchase_photo_router)
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="CRM", web_app=WebAppInfo(url=MINIAPP_URL))
    )
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
