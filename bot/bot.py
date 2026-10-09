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
from bot.assistant_chat import router as assistant_chat_router
from bot.buyback_flow import router as buyback_flow_router
from bot.fallback import router as fallback_router
from bot.channel_orders import router as channel_orders_router
from bot.handlers import router
from bot.purchase_photo import router as purchase_photo_router
from bot.quick_actions import ButtonTextMiddleware
from bot.quick_actions import router as quick_actions_router
from bot.repair_actions import router as repair_actions_router
from bot.repair_attachments import router as repair_attachments_router
from bot.sale_flow import router as sale_flow_router
from bot.transfer_flow import router as transfer_flow_router
from core import assistant as core_assistant
from core.storage import init_db
from core.stores import load_stores

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO — and a Bot API URL contains the
# bot token (core.notify posts through httpx). Keep it out of journald.
logging.getLogger("httpx").setLevel(logging.WARNING)


def build_dispatcher() -> Dispatcher:
    """Every router in its order, and the middleware — one place, so the
    whole thing can be exercised as it runs (simulate_tests feeds it real
    updates; calling handler functions directly skips the filters, which
    is exactly where a dead button hides)."""
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
    # «Бот, …» — a question to the bot itself (bot/assistant_chat.py): ahead
    # of the note handler, so it is answered even when sent as a reply.
    dp.include_router(assistant_chat_router)
    # Before quick_actions: that router ends in a catch-all for every FSM
    # state (TransferFlow's included), which would otherwise take this
    # flow's text steps first.
    dp.include_router(transfer_flow_router)
    dp.include_router(buyback_flow_router)
    dp.include_router(sale_flow_router)
    dp.include_router(quick_actions_router)
    dp.include_router(repair_actions_router)
    dp.include_router(repair_attachments_router)
    dp.include_router(purchase_photo_router)
    # Last: whatever nobody above took (see bot/fallback.py).
    dp.include_router(fallback_router)
    # Button taps arrive with or without an emoji variation selector —
    # normalised before any filter looks (bot.quick_actions).
    dp.message.outer_middleware(ButtonTextMiddleware())
    return dp


_ASSISTANT_TICK_SECONDS = 600


async def _assistant_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(core_assistant.run_once)
        except Exception:  # a reminder must never take the bot down
            logging.getLogger(__name__).exception("assistant digest failed")
        await asyncio.sleep(_ASSISTANT_TICK_SECONDS)


async def main() -> None:
    # Schema ready before the first update arrives — matters if this process
    # starts before the web one ever has. One base for every точка (06.10).
    init_db(load_stores()[0].db_path)
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
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
    # Помощник (core.assistant): the daily «Проблемы» digest, for точки
    # that switched it on. The reference is kept so the task isn't
    # garbage-collected mid-flight.
    assistant_task = asyncio.create_task(_assistant_loop())
    try:
        await dp.start_polling(bot)
    finally:
        assistant_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
