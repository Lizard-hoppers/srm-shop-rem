"""Best-effort staff notifications via the raw Telegram Bot API.

Kept independent of bot/ (a separate long-running polling process) — the web
panel process posts directly to api.telegram.org so it never needs IPC with
the bot process to reach staff chats.

Two separate destinations exist for a new repair, and both are optional —
each is skipped silently if its env var isn't set:
  - the main staff forum group, topic "Ремонт техники" (CRM_STAFF_GROUP_CHAT_ID
    + CRM_REPAIR_TOPIC_ID) — see OPERATIONS.md for the full topic map (some
    topics are informational-only for now, nothing posts into them yet).
  - a separate plain group for all repair masters (CRM_MASTERS_GROUP_CHAT_ID)
    — one shared chat for now, not per-master; masters pick up whichever new
    job from there.
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


def _send(chat_id: str | int | None, text: str, message_thread_id: str | int | None = None) -> None:
    """Post an HTML-formatted message to `chat_id`. Never raises — a
    failed/unconfigured notification must not break the request that
    triggered it (e.g. accepting a repair)."""
    if not chat_id or not _BOT_TOKEN:
        return
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff notify failed: %s %s", resp.status_code, resp.text)
    except httpx.HTTPError:
        logger.warning("staff notify failed", exc_info=True)


def notify_staff_group(text: str, message_thread_id: str | int | None = None) -> None:
    """Post to the main staff forum group. `message_thread_id` targets a
    specific topic; omit for the group's General feed."""
    _send(_STAFF_GROUP_CHAT_ID, text, message_thread_id=message_thread_id)


def notify_repair_card(text: str) -> None:
    """Post a new-repair card everywhere staff expect to see one: the
    'Ремонт техники' topic in the main staff group, and the separate
    masters group."""
    notify_staff_group(text, message_thread_id=_REPAIR_TOPIC_ID)
    _send(_MASTERS_GROUP_CHAT_ID, text)
