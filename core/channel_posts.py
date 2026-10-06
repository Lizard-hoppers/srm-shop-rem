"""Витрина в Telegram-канале (02.10): the bot posts a product card to the
store's sales channel and takes it down by itself once the product is
sold out — nobody has to remember to go delete the post by hand.

One card per product: photo (if the product has one) + name/description/
price as the caption + a «Купить» url button that deep-links into the bot
(bot/channel_orders.py turns that tap into a lead for staff). The channel
is per store — store_settings.sales_channel, set in Кабинет магазина; the
bot must be an admin of that channel with the right to post.

sync_product() is the one entry point every stock/price-changing route
calls after its own transaction commits: sold out -> the card is deleted
(or, when Telegram refuses — a bot can only delete its messages for ~48h
— edited into a ПРОДАНО stub with no button); still in stock -> the card's
text is refreshed if the name/price/description changed. A product with no
live card makes it a cheap no-op, so callers don't need to check first.

Like core/notify.py this never raises on a Telegram failure — a sale must
not fail because the channel post couldn't be updated.
"""
from __future__ import annotations

import html
import os
import sqlite3

from core import inventory as _inventory
from core import notify as _notify
from core import store_settings as _store_settings

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static")
# A resale product created by core.buyback keeps its photo under
# buyback_photos/ (same filename stored in products.photo_path), a regular
# product under product_photos/ — look in both.
_PHOTO_DIRS = ("product_photos", "buyback_photos")

BUY_PREFIX = "buy_"
_BUY_BUTTON_TEXT = "🛒 Купить / забронировать"


class ChannelPostError(Exception):
    """A publish that can't go ahead — message is shown to staff as-is."""


def buy_payload(product_id: int, store_id: str) -> str:
    """/start deep-link payload behind the card's button. Product id first:
    it's always digits, while a store id is free-form (and may itself
    contain an underscore), so parse_buy_payload() splits just once."""
    return f"{BUY_PREFIX}{product_id}_{store_id}"


def parse_buy_payload(payload: str | None) -> tuple[int, str] | None:
    if not payload or not payload.startswith(BUY_PREFIX):
        return None
    product_part, _, store_id = payload[len(BUY_PREFIX):].partition("_")
    if not product_part.isdigit() or not store_id:
        return None
    return int(product_part), store_id


def format_price(price: int) -> str:
    """52000 -> «52 000 грн» (non-breaking space, so the amount never
    wraps apart from its thousands or its currency)."""
    return f"{price:,}".replace(",", "\u00a0") + "\u00a0грн"


# «Состояние: идеальное» -> the label part, when a description line is
# shaped like a spec row. Short label only, so an ordinary sentence that
# happens to contain a colon isn't half-bolded.
_SPEC_LABEL_MAX = 24


def _description_block(description: str) -> str:
    """The customer-facing description as a quote block; lines typed as
    «Метка: значение» get the label in bold, so a plain list of specs
    reads as a neat spec sheet without staff typing any markup."""
    rendered = []
    for line in description.splitlines():
        line = line.strip()
        if not line:
            continue
        label, sep, value = line.partition(":")
        if sep and value.strip() and len(label) <= _SPEC_LABEL_MAX:
            rendered.append(f"<b>{html.escape(label.strip())}:</b> {html.escape(value.strip())}")
        else:
            rendered.append(html.escape(line))
    return "<blockquote>" + "\n".join(rendered) + "</blockquote>" if rendered else ""


def build_caption(product: sqlite3.Row, settings: sqlite3.Row) -> str:
    """Card layout: title, spec sheet in a quote block, price on its own
    line, availability, then the shop's contacts. Deliberately sparse on
    emoji and bold — one accent per block (see the «не всё жирным» rule
    for this project's bot texts)."""
    blocks = [f"<b>{html.escape(product['name'])}</b>"]
    description = _description_block(product["description"] or "")
    if description:
        blocks.append(description)
    blocks.append(f"Цена: <b>{format_price(product['price'])}</b>\n✅ В наличии")
    contacts = []
    if settings["address"]:
        contacts.append(f"📍 {html.escape(settings['address'])}")
    if settings["phone"]:
        contacts.append(f"☎️ {html.escape(settings['phone'])}")
    if settings["working_hours"]:
        contacts.append(f"🕒 {html.escape(settings['working_hours'])}")
    if contacts:
        blocks.append("\n".join(contacts))
    return "\n\n".join(blocks)


def build_sold_caption(product: sqlite3.Row) -> str:
    return f"✅ <b>ПРОДАНО</b>\n<s>{html.escape(product['name'])}</s>"


def _buy_keyboard(product_id: int, store_id: str) -> dict | None:
    username = _notify.bot_username()
    if not username:
        return None
    url = f"https://t.me/{username}?start={buy_payload(product_id, store_id)}"
    return {"inline_keyboard": [[{"text": _BUY_BUTTON_TEXT, "url": url, "style": "success"}]]}


def _read_photo(product: sqlite3.Row) -> tuple[bytes, str] | None:
    if not product["photo_path"]:
        return None
    for directory in _PHOTO_DIRS:
        path = os.path.join(_STATIC_DIR, directory, product["photo_path"])
        if os.path.exists(path):
            with open(path, "rb") as f:
                return f.read(), product["photo_path"]
    return None


def get_active_post(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM channel_posts WHERE product_id = ? AND status = 'active' ORDER BY id DESC LIMIT 1",
        (product_id,),
    ).fetchone()


def post_url(post: sqlite3.Row) -> str | None:
    """A t.me link to the card itself — public channels by @username,
    private ones through the /c/ form (opens only for channel members)."""
    chat_id = str(post["chat_id"])
    if chat_id.startswith("@"):
        return f"https://t.me/{chat_id[1:]}/{post['message_id']}"
    if chat_id.startswith("-100"):
        return f"https://t.me/c/{chat_id[4:]}/{post['message_id']}"
    return None


def publish(conn: sqlite3.Connection, product_id: int, store_id: str, staff_id: int) -> int:
    """Post the product's card to this store's sales channel; returns the
    new channel_posts id. Raises ChannelPostError (staff-readable text)
    when it can't."""
    location_id = int(store_id)
    settings = _store_settings.get_settings(conn, location_id)
    channel = settings["sales_channel"]
    if not channel:
        raise ChannelPostError("Канал продаж не указан — задайте его в «Кабинете магазина».")
    product = _inventory.get_product(conn, product_id)
    if not product:
        raise ChannelPostError("Товар не найден.")
    if get_active_post(conn, product_id):
        raise ChannelPostError("Этот товар уже выставлен в канале.")
    if not product["price"]:
        raise ChannelPostError("Укажите цену товара — без неё карточку не выставить.")
    if _inventory.product_total_qty(conn, product_id, location_id) <= 0:
        raise ChannelPostError("Товара нет в наличии — сначала добавьте остаток.")

    caption = build_caption(product, settings)
    photo = _read_photo(product)
    message_id = _notify.send_card(
        channel, caption, photo=photo, reply_markup=_buy_keyboard(product_id, store_id)
    )
    if not message_id:
        raise ChannelPostError(
            "Не удалось отправить в канал. Проверьте, что бот добавлен в канал администратором "
            "с правом публикации и что канал указан верно."
        )
    return conn.execute(
        """INSERT INTO channel_posts (product_id, chat_id, message_id, has_photo, caption, staff_id)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (product_id, channel, message_id, int(bool(photo)), caption, staff_id),
    ).lastrowid


def _edit(post: sqlite3.Row, text: str, reply_markup: dict | None) -> bool:
    if post["has_photo"]:
        return _notify.edit_message_caption(post["chat_id"], post["message_id"], text, reply_markup=reply_markup)
    return _notify.edit_message(post["chat_id"], post["message_id"], text, reply_markup=reply_markup)


def _take_down(conn: sqlite3.Connection, post: sqlite3.Row, product: sqlite3.Row) -> bool:
    """Delete the card; if Telegram won't (older than ~48h), turn it into a
    ПРОДАНО stub with no button instead. False only when neither worked —
    the post then stays 'active' so a later sync/manual removal retries."""
    if _notify.delete_message(post["chat_id"], post["message_id"]):
        status = "removed"
    elif _edit(post, build_sold_caption(product), None):
        status = "sold"
    else:
        return False
    conn.execute(
        "UPDATE channel_posts SET status = ?, closed_at = datetime('now') WHERE id = ?", (status, post["id"])
    )
    return True


def unpublish(conn: sqlite3.Connection, product_id: int) -> bool:
    """Manual «Снять с канала» from the product card. True if there's no
    live card left afterwards."""
    product = _inventory.get_product(conn, product_id)
    post = get_active_post(conn, product_id)
    if not post or not product:
        return True
    return _take_down(conn, post, product)


def sync_product(conn: sqlite3.Connection, product_id: int, store_id: str) -> None:
    """Bring the product's live channel card (if any) in line with the DB —
    see the module docstring. Call after the stock/price change itself has
    been committed."""
    post = get_active_post(conn, product_id)
    if not post:
        return
    location_id = int(store_id)
    product = _inventory.get_product(conn, product_id)
    if not product["active"] or _inventory.product_total_qty(conn, product_id, location_id) <= 0:
        _take_down(conn, post, product)
        return
    if not product["price"]:
        return  # nothing sensible to show; leave the card as it was
    caption = build_caption(product, _store_settings.get_settings(conn, location_id))
    if caption == post["caption"]:
        return
    if _edit(post, caption, _buy_keyboard(product_id, store_id)):
        conn.execute("UPDATE channel_posts SET caption = ? WHERE id = ?", (caption, post["id"]))


def sync_products(product_ids, store_id: str, db_path: str | None = None) -> None:
    """sync_product() for several products on a fresh connection — for a
    route to call once its own `with get_conn()` block has committed."""
    from core.storage import get_conn

    for product_id in set(product_ids):
        with get_conn(db_path) as conn:
            sync_product(conn, product_id, store_id)


def record_lead(
    conn: sqlite3.Connection, product_id: int, telegram_id: int, name: str | None, username: str | None
) -> bool:
    """True the first time this person asks about this product, False on a
    repeat tap — staff are only notified once per (product, person)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO channel_leads (product_id, telegram_id, name, username) VALUES (?, ?, ?, ?)",
        (product_id, telegram_id, name, username),
    )
    return cur.rowcount > 0
