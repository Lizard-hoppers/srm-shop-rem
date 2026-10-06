"""«Купить» under a sales-channel card (core.channel_posts) — the button
is a t.me/<bot>?start=buy_<product>_<store> deep link, so the tap lands
here as /start with that payload, from someone who is usually a complete
stranger to the CRM (a channel subscriber, not staff or a known client).

The customer gets a confirmation; staff get one lead message in the
store's staff group — its sales topic if one is configured, see
lead_destinations() — (or, for a store with no group configured, in the DMs
of its owner/admins) with a tap-to-open link to the customer's profile.
Nothing is reserved or written to stock — a human closes the deal, and the
ordinary Продажи flow is what later removes the card from the channel.

Included before bot.handlers' router in bot/bot.py: that one's plain
CommandStart() would otherwise swallow every /start, payload or not.
"""
from __future__ import annotations

import html
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import CommandObject, CommandStart
from aiogram.types import Message

from core import auth as core_auth
from core import channel_posts
from core import clients as core_clients
from core import inventory as core_inventory
from core import store_settings as core_store_settings
from core.storage import get_conn
from core.stores import StoreConfig, get_store

logger = logging.getLogger(__name__)

router = Router()

SOLD_OUT_TEXT = "К сожалению, этот товар уже продан 😔\nЗагляните в канал — там всё, что сейчас в наличии."
NOT_FOUND_TEXT = "Не нашёл этот товар — возможно, объявление устарело. Загляните в канал за актуальными."


def process_buy_request(
    payload: str, telegram_id: int, full_name: str | None, username: str | None
) -> tuple[str, str | None, StoreConfig | None, list[int]]:
    """All the DB work for one «Купить» tap, no Telegram I/O (so
    simulate_tests.py can drive it directly). Returns (reply to the
    customer, lead text for staff or None when there's nothing to tell
    them — unknown/sold product or a repeat tap, the store, telegram ids
    of the store's owner/admins as the fallback destination)."""
    parsed = channel_posts.parse_buy_payload(payload)
    if not parsed:
        return NOT_FOUND_TEXT, None, None, []
    product_id, store_id = parsed
    try:
        store = get_store(store_id)
    except KeyError:
        return NOT_FOUND_TEXT, None, None, []

    with get_conn(store.db_path) as conn:
        product = core_inventory.get_product(conn, product_id)
        if not product:
            return NOT_FOUND_TEXT, None, store, []
        qty = core_inventory.product_total_qty(conn, product_id)
        if qty <= 0 or not product["active"]:
            return SOLD_OUT_TEXT, None, store, []
        settings = core_store_settings.get_settings(conn)
        client = core_clients.get_by_telegram_id(conn, telegram_id)
        is_new = channel_posts.record_lead(conn, product_id, telegram_id, full_name, username)
        managers = [
            row["telegram_id"] for row in core_auth.list_staff(conn)
            if row["role"] in ("owner", "admin") and row["active"] and row["telegram_id"]
        ]

    price = f" — {channel_posts.format_price(product['price'])}" if product["price"] else ""
    product_line = f"<b>{html.escape(product['name'])}</b>{price}"

    reply = [f"✅ Заявка принята!\n{product_line}", ""]
    if username:
        reply.append("Менеджер скоро напишет вам сюда, в Telegram.")
    else:
        reply.append(
            "У вас в Telegram не указано имя пользователя (@username), поэтому менеджер "
            "может не суметь написать вам первым — свяжитесь с нами сами:"
        )
    if settings["phone"]:
        reply.append(f"☎️ {html.escape(settings['phone'])}")
    if settings["address"]:
        reply.append(f"📍 {html.escape(settings['address'])}")
    if settings["working_hours"]:
        reply.append(f"🕒 {html.escape(settings['working_hours'])}")

    if not is_new:
        return "\n".join(reply), None, store, managers

    buyer = f'<a href="tg://user?id={telegram_id}">{html.escape(full_name or "Покупатель")}</a>'
    if username:
        buyer += f" (@{html.escape(username)})"
    lead = ["🛒 <b>Заявка из канала</b>", f"Товар: {product_line} · в наличии {qty} {html.escape(product['unit'])}", f"Покупатель: {buyer}"]
    if client and client["phone"]:
        lead.append(f"Телефон (клиент №{client['id']}): {html.escape(client['phone'])}")
    return "\n".join(reply), "\n".join(lead), store, managers


def lead_destinations(store: StoreConfig, managers: list[int]) -> list[tuple[int, int | None]]:
    """(chat_id, message_thread_id) pairs a lead goes to: the store's staff
    group — in its dedicated sales topic when stores.json names one
    (sales_topic_id), the General feed otherwise — or, for a store with no
    group at all, each owner/admin's DM."""
    if store.staff_group_chat_id:
        return [(store.staff_group_chat_id, store.sales_topic_id)]
    return [(telegram_id, None) for telegram_id in managers]


@router.message(
    CommandStart(magic=F.args.startswith(channel_posts.BUY_PREFIX)),
    F.chat.type == "private",
)
async def buy_from_channel(message: Message, command: CommandObject) -> None:
    user = message.from_user
    reply, lead, store, managers = process_buy_request(command.args, user.id, user.full_name, user.username)
    await message.answer(reply)
    if not lead:
        return
    for chat_id, topic_id in lead_destinations(store, managers):
        try:
            await message.bot.send_message(chat_id, lead, message_thread_id=topic_id)
        except TelegramAPIError:
            # e.g. an owner who never started the bot in DM — the lead is
            # still in channel_leads, and the customer already has the
            # shop's contacts in their own reply.
            logger.warning("channel lead not delivered to %s", chat_id, exc_info=True)
