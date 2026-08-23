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

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
_STAFF_GROUP_CHAT_ID = os.environ.get("CRM_STAFF_GROUP_CHAT_ID")
_REPAIR_TOPIC_ID = os.environ.get("CRM_REPAIR_TOPIC_ID")
_MASTERS_GROUP_CHAT_ID = os.environ.get("CRM_MASTERS_GROUP_CHAT_ID")

# Telegram caps a photo caption at 1024 chars (vs. 4096 for a plain text
# message) — the card itself is normally well under that, but defect
# descriptions are free text with no length limit on the form, so this is
# a safety net, not the expected case.
_MAX_CAPTION_LEN = 1024


def _as_caption(text: str) -> str:
    if len(text) <= _MAX_CAPTION_LEN:
        return text
    return text[: _MAX_CAPTION_LEN - 1] + "…"


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
    chat_id: str | int | None,
    photo_bytes: bytes,
    filename: str,
    message_thread_id: str | int | None = None,
    caption: str | None = None,
    reply_markup: dict | None = None,
) -> int | None:
    """Post a photo (uploaded directly, not by URL — Telegram's fetch-by-
    URL path for sendPhoto turned out unreliable against our own domain,
    rejecting a perfectly valid, publicly-fetchable JPEG with "wrong type
    of the web page content"; a direct multipart upload has no such
    dependency on Telegram being able to reach/like our server) to
    `chat_id`, optionally as the one message carrying both the photo and
    the card text/keyboard (caption), rather than two separate messages.
    Never raises, same best-effort contract as _send()."""
    if not chat_id or not _BOT_TOKEN:
        return None
    data = {"chat_id": chat_id}
    if message_thread_id:
        data["message_thread_id"] = message_thread_id
    if caption:
        data["caption"] = _as_caption(caption)
        data["parse_mode"] = "HTML"
    if reply_markup:
        # multipart/form-data has no native nested-object support — the
        # Bot API accepts reply_markup as a JSON-encoded string field here.
        data["reply_markup"] = json.dumps(reply_markup)
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


def edit_message_caption(
    chat_id: str | int, message_id: int, caption: str, reply_markup: dict | None = None
) -> bool:
    """Same as edit_message(), but for a message that was sent as a photo
    with a caption (editMessageText 400s on those — Telegram requires
    editMessageCaption instead). Never raises; returns whether it went
    through."""
    if not chat_id or not message_id or not _BOT_TOKEN:
        return False
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": _as_caption(caption),
        "parse_mode": "HTML",
        "reply_markup": reply_markup or {"inline_keyboard": []},
    }
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/editMessageCaption",
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff notify caption edit failed: %s %s", resp.status_code, resp.text)
            return False
        return True
    except httpx.HTTPError:
        logger.warning("staff notify caption edit failed", exc_info=True)
        return False


def notify_staff_group(
    text: str,
    message_thread_id: str | int | None = None,
    reply_markup: dict | None = None,
    *,
    staff_group_chat_id: str | int | None = None,
) -> int | None:
    """Post to a staff forum group. `message_thread_id` targets a specific
    topic; omit for the group's General feed. `staff_group_chat_id` lets a
    caller target a specific store's group (Фаза C, 23.08) — omitted (None),
    falls back to the legacy env-configured group. Resolved inside the
    function body, not as a literal default value: a default arg is bound
    once at def time, which would freeze whatever _STAFF_GROUP_CHAT_ID
    happened to be at import — this way a monkeypatched module constant
    (see simulate_tests.py's notify tests) is still picked up per call."""
    if staff_group_chat_id is None:
        staff_group_chat_id = _STAFF_GROUP_CHAT_ID
    return _send(staff_group_chat_id, text, message_thread_id=message_thread_id, reply_markup=reply_markup)


def notify_repair_card(
    text: str,
    reply_markup: dict | None = None,
    photo: tuple[bytes, str] | None = None,
    *,
    staff_group_chat_id: str | int | None = None,
    repair_topic_id: str | int | None = None,
    masters_group_chat_id: str | int | None = None,
) -> list[tuple[str, int, str, bool]]:
    """Post a new-repair card everywhere staff expect to see one: the
    'Ремонт техники' topic in the main staff group, and the separate
    masters group. Returns a (chat_id, message_id, kind, has_photo) tuple
    for every destination that actually sent, so the caller can persist
    them (see core.repairs.save_order_messages) for later edits via
    sync_repair_cards.

    Фаза C (23.08): which groups is now an explicit argument, not a fixed
    env-configured pair — a multi-store deployment has a different group
    per store (core/stores.py::StoreConfig), and the caller (webapp.routers
    .repairs.create_view) already knows which store the request belongs
    to. Omitted (None) keyword args fall back to the legacy env constants,
    resolved inside the function body rather than as literal default
    values (see notify_staff_group's docstring for why) — a single-store
    deployment, or any caller that doesn't pass them, behaves exactly as
    before.

    If photo (raw bytes, filename) is given — a device photo attached at
    intake, see webapp.routers.repairs.create_view — the card goes out as
    ONE message: the photo with the card text as its caption and the
    status keyboard attached, not a bare photo followed by a separate
    text card (that used to read as two disconnected messages in the
    group)."""
    if staff_group_chat_id is None:
        staff_group_chat_id = _STAFF_GROUP_CHAT_ID
    if repair_topic_id is None:
        repair_topic_id = _REPAIR_TOPIC_ID
    if masters_group_chat_id is None:
        masters_group_chat_id = _MASTERS_GROUP_CHAT_ID

    sent: list[tuple[str, int, str, bool]] = []
    if photo:
        photo_bytes, filename = photo
        topic_message_id = _send_photo(
            staff_group_chat_id, photo_bytes, filename,
            message_thread_id=repair_topic_id, caption=text, reply_markup=reply_markup,
        )
        if topic_message_id:
            sent.append((staff_group_chat_id, topic_message_id, "topic", True))
        masters_message_id = _send_photo(
            masters_group_chat_id, photo_bytes, filename, caption=text, reply_markup=reply_markup
        )
        if masters_message_id:
            sent.append((masters_group_chat_id, masters_message_id, "masters_group", True))
        return sent

    topic_message_id = _send(staff_group_chat_id, text, message_thread_id=repair_topic_id, reply_markup=reply_markup)
    if topic_message_id:
        sent.append((staff_group_chat_id, topic_message_id, "topic", False))
    masters_message_id = _send(masters_group_chat_id, text, reply_markup=reply_markup)
    if masters_message_id:
        sent.append((masters_group_chat_id, masters_message_id, "masters_group", False))
    return sent


def sync_repair_cards(messages: list[tuple[str, int, bool]], text: str, reply_markup: dict | None = None) -> None:
    """Edit every previously sent card for a repair order to the same new
    text/keyboard — called after any status change, whether it came from a
    button press or from the web app, so the two never drift apart.
    `messages` is a list of (chat_id, message_id, has_photo) triples, e.g.
    from core.repairs.get_order_messages() — has_photo picks editMessageCaption
    (photo+caption cards) vs editMessageText (plain text cards)."""
    for chat_id, message_id, has_photo in messages:
        if has_photo:
            edit_message_caption(chat_id, message_id, text, reply_markup=reply_markup)
        else:
            edit_message(chat_id, message_id, text, reply_markup=reply_markup)
