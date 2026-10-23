import asyncio
import logging

from aiogram import BaseMiddleware, Bot, Dispatcher
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
from bot import reminder_flow
from bot.assistant_chat import router as assistant_chat_router
from bot.buyback_flow import router as buyback_flow_router
from bot.fallback import router as fallback_router
from bot.channel_orders import router as channel_orders_router
from bot.handlers import router
from bot.purchase_photo import router as purchase_photo_router
from bot.quick_actions import ButtonTextMiddleware
from bot.quick_actions import router as quick_actions_router
from bot.quick_actions import cancel_router
from bot.repair_actions import router as repair_actions_router
from bot.part_add_flow import router as part_add_router
from bot.repair_attachments import router as repair_attachments_router
from bot.repairs_button import router as repairs_button_router
from bot.sale_flow import router as sale_flow_router
from bot.simple_card import router as simple_card_router
from bot.supplier_chat import router as supplier_chat_router
from bot.transfer_flow import router as transfer_flow_router
from core import assistant as core_assistant
from core import bots as core_bots
from core import tg_journal
from core.storage import get_conn
from core.storage import init_db
from core.stores import load_stores, store_for_chat_id

logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO — and a Bot API URL contains the
# bot token (core.notify posts through httpx). Keep it out of journald.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _chat_of(update):
    for event in (update.message, update.edited_message, update.my_chat_member, update.chat_member):
        if event is not None:
            return event.chat
    if update.callback_query is not None and update.callback_query.message is not None:
        return update.callback_query.message.chat
    return None


def _journal(bot_id: int, update) -> None:
    """Журнал Telegram (core.tg_journal): who wrote or pressed what.
    Never lets a hiccup here cost the update itself."""
    if update.message is not None and update.message.from_user and not update.message.from_user.is_bot:
        user, chat = update.message.from_user, update.message.chat
        kind = "private" if chat.type == "private" else "group"
    elif update.callback_query is not None:
        user = update.callback_query.from_user
        chat = update.callback_query.message.chat if update.callback_query.message else None
        kind = "callback"
    else:
        return
    try:
        with get_conn(load_stores()[0].db_path) as conn:
            tg_journal.record(conn, bot_id, user, chat, kind)
    except Exception:
        logging.getLogger(__name__).warning("journal not written", exc_info=True)


class BotScopeMiddleware(BaseMiddleware):
    """Свой бот у точки (core.bots, 23.10). The bot an update came through
    is the точка: it is kept in CURRENT_BOT_ID while the update is handled
    (core.store_access narrows the точки by it). In a group that belongs to
    a точка only that точка's bot acts — with both bots in one group nothing
    is answered twice."""

    async def __call__(self, handler, event, data):
        bot = data["bot"]
        chat = _chat_of(event)
        if chat is not None and chat.type in ("group", "supergroup"):
            store = store_for_chat_id(chat.id)
            if store is not None and core_bots.bot_id(core_bots.token_for_location(store.location_id)) != bot.id:
                return None
        await asyncio.to_thread(_journal, bot.id, event)
        mark = core_bots.CURRENT_BOT_ID.set(bot.id)
        try:
            return await handler(event, data)
        finally:
            core_bots.CURRENT_BOT_ID.reset(mark)


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
    # First: a group tied to a supplier belongs to this router whole — the
    # supplier sits there, so nothing of it may reach the assistant or the
    # repair handlers below (bot/supplier_chat.py).
    dp.include_router(supplier_chat_router)
    # «❌ Отмена» before every flow router — each of them takes any text as
    # an answer to its current step (bot.quick_actions.cancel_router).
    dp.include_router(cancel_router)
    # Before `router`: its bare CommandStart() matches every /start, deep
    # link or not — see bot/channel_orders.py.
    dp.include_router(channel_orders_router)
    dp.include_router(router)
    # Простой режим (core.simple_flow): the sum typed after «💰 Выдача» is a
    # reply to the bot, not to a card — ahead of the note handler, which
    # would swallow it.
    dp.include_router(simple_card_router)
    # «Бот, …» — a question to the bot itself (bot/assistant_chat.py): ahead
    # of the note handler, so it is answered even when sent as a reply.
    # «🔧 Ремонты», the button docked in the groups (bot/repairs_button.py).
    dp.include_router(repairs_button_router)
    dp.include_router(assistant_chat_router)
    dp.include_router(reminder_flow.router)
    # Before quick_actions: that router ends in a catch-all for every FSM
    # state (TransferFlow's included), which would otherwise take this
    # flow's text steps first.
    dp.include_router(transfer_flow_router)
    dp.include_router(part_add_router)
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
    dp.update.outer_middleware(BotScopeMiddleware())
    return dp


_ASSISTANT_TICK_SECONDS = 600
_REMINDER_TICK_SECONDS = 30


async def _reminder_loop(bots: list[Bot]) -> None:
    """Напоминания (bot/reminder_flow.py): whatever has come due is
    posted — checked twice a minute, so «в 10:20» means 10:20. Each through
    the bot of its chat."""
    by_token = {b.token: b for b in bots}

    def bot_for(chat_id):
        return by_token.get(core_bots.token_for_chat(chat_id)) or bots[0]

    while True:
        try:
            await reminder_flow.deliver_due(bots[0], bot_for)
        except Exception:  # one bad reminder must not stop the rest, or the bot
            logging.getLogger(__name__).exception("reminder delivery failed")
        await asyncio.sleep(_REMINDER_TICK_SECONDS)


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
    # The default bot (CRM_BOT_TOKEN) and the точки's own (core.bots) —
    # one dispatcher polls them all.
    bots = [Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
            for token in (core_bots.all_tokens() or [BOT_TOKEN])]
    dp = build_dispatcher()
    for bot in bots:
        await _set_up(bot)
    # Помощник (core.assistant): the daily «Проблемы» digest, for точки
    # that switched it on. The reference is kept so the task isn't
    # garbage-collected mid-flight.
    assistant_task = asyncio.create_task(_assistant_loop())
    reminder_task = asyncio.create_task(_reminder_loop(bots))
    try:
        await dp.start_polling(*bots)
    finally:
        assistant_task.cancel()
        reminder_task.cancel()


async def _set_up(bot: Bot) -> None:
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


if __name__ == "__main__":
    asyncio.run(main())
