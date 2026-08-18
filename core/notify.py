"""Best-effort staff notifications via the raw Telegram Bot API.

Kept independent of bot/ (a separate long-running polling process) — the web
panel process posts directly to api.telegram.org so it never needs IPC with
the bot process to reach staff chats. Also independent of core/storage.py —
this module only knows how to send/edit Telegram messages; callers own
looking up which chat_id/message_id pairs belong to which repair order
(see core.repairs.save_order_messages / get_order_messages).

Two separate destinations exist for a new repair, and both are optional —
each is skipped silently if its env var isn't set:
  - the main staff forum group, topic "Ремонт техники" (CRM_STAFF_GROUP_CHAT_ID
    + CRM_REPAIR_TOPIC_ID) — see OPERATIONS.md for the full topic map (some
    topics are informational-only for now, nothing posts into them yet).
  - a separate plain group for all repair masters (CRM_MASTERS_GROUP_CHAT_ID)
    — one shared chat for now, not per-master; masters pick up whichever new
    job from there.

Reply keyboards are passed around as plain dicts matching the Telegram Bot
API's InlineKeyboardMarkup JSON shape (`{"inline_keyboard": [[{"text":
..., "callback_data": ...}]]}`) rather than an aiogram type, so this module
stays usable from both the aiogram bot process and the plain FastAPI web
process without pulling aiogram into the web process's notify path.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
_STAFF_GROUP_CHAT_ID = os.environ.get("CRM_STAFF_GROUP_CHAT_ID")
_REPAIR_TOPIC_ID = os.environ.get("CRM_REPAIR_TOPIC_ID")
_MASTERS_GROUP_CHAT_ID = os.environ.get("CRM_MASTERS_GROUP_CHAT_ID")


def _send(
    chat_id: str | int | None,
    text: str,
    message_thread_id: str | int | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Post an HTML-formatted message to `chat_id`, returning the sent
    message's id on success or None otherwise. Never raises — a
    failed/unconfigured notification must not break the request that
    triggered it (e.g. accepting a repair)."""
    if not chat_id or not _BOT_TOKEN:
        return None
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff notify failed: %s %s", resp.status_code, resp.text)
            return None
        return resp.json()["result"]["message_id"]
    except httpx.HTTPError:
        logger.warning("staff notify failed", exc_info=True)
        return None


def _send_photo(
    chat_id: str | int | None, photo_bytes: bytes, filename: str, message_thread_id: str | int | None = None
) -> int | None:
    """Post a photo (uploaded directly, not by URL — Telegram's fetch-by-
    URL path for sendPhoto turned out unreliable against our own domain,
    rejecting a perfectly valid, publicly-fetchable JPEG with "wrong type
    of the web page content"; a direct multipart upload has no such
    dependency on Telegram being able to reach/like our server) to
    `chat_id`, no caption. Never raises, same best-effort contract as
    _send(). Not tracked in repair_order_messages: it's never edited
    later, only the text card that follows it is."""
    if not chat_id or not _BOT_TOKEN:
        return None
    data = {"chat_id": chat_id}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendPhoto",
            data=data,
            files={"photo": (filename, photo_bytes, "image/jpeg")},
            timeout=15,
        )
        if resp.status_code != 200:
            logger.warning("staff photo notify failed: %s %s", resp.status_code, resp.text)
            return None
        return resp.json()["result"]["message_id"]
    except httpx.HTTPError:
        logger.warning("staff photo notify failed", exc_info=True)
        return None


def edit_message(chat_id: str | int, message_id: int, text: str, reply_markup: dict | None = None) -> bool:
    """Edit a previously sent message in place — used to reflect a status
    change (claimed / done) without spamming a new message per update.
    Never raises; returns whether the edit went through."""
    if not chat_id or not message_id or not _BOT_TOKEN:
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/editMessageText",
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff notify edit failed: %s %s", resp.status_code, resp.text)
            return False
        return True
    except httpx.HTTPError:
        logger.warning("staff notify edit failed", exc_info=True)
        return False


def notify_staff_group(
    text: str, message_thread_id: str | int | None = None, reply_markup: dict | None = None
) -> int | None:
    """Post to the main staff forum group. `message_thread_id` targets a
    specific topic; omit for the group's General feed."""
    return _send(_STAFF_GROUP_CHAT_ID, text, message_thread_id=message_thread_id, reply_markup=reply_markup)


def notify_repair_card(
    text: str, reply_markup: dict | None = None, photo: tuple[bytes, str] | None = None
) -> list[tuple[str, int, str]]:
    """Post a new-repair card everywhere staff expect to see one: the
    'Ремонт техники' topic in the main staff group, and the separate
    masters group. Returns a (chat_id, message_id, kind) tuple for every
    destination that actually sent, so the caller can persist them (see
    core.repairs.save_order_messages) for later edits via sync_repair_cards.

    If photo (raw bytes, filename) is given — a device photo attached at
    intake, see webapp.routers.repairs.create_view — it goes out first,
    so the card never reaches staff incomplete with the photo trickling
    in as an afterthought once someone happens to open the repair later."""
    if photo:
        photo_bytes, filename = photo
        _send_photo(_STAFF_GROUP_CHAT_ID, photo_bytes, filename, message_thread_id=_REPAIR_TOPIC_ID)
        _send_photo(_MASTERS_GROUP_CHAT_ID, photo_bytes, filename)

    sent: list[tuple[str, int, str]] = []
    topic_message_id = _send(_STAFF_GROUP_CHAT_ID, text, message_thread_id=_REPAIR_TOPIC_ID, reply_markup=reply_markup)
    if topic_message_id:
        sent.append((_STAFF_GROUP_CHAT_ID, topic_message_id, "topic"))
    masters_message_id = _send(_MASTERS_GROUP_CHAT_ID, text, reply_markup=reply_markup)
    if masters_message_id:
        sent.append((_MASTERS_GROUP_CHAT_ID, masters_message_id, "masters_group"))
    return sent


def sync_repair_cards(messages: list[tuple[str, int]], text: str, reply_markup: dict | None = None) -> None:
    """Edit every previously sent card for a repair order to the same new
    text/keyboard — called after any status change, whether it came from a
    button press or from the web app, so the two never drift apart.
    `messages` is a list of (chat_id, message_id) pairs, e.g. from
    core.repairs.get_order_messages()."""
    for chat_id, message_id in messages:
        edit_message(chat_id, message_id, text, reply_markup=reply_markup)
