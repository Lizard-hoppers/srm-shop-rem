"""Best-effort staff notifications via the raw Telegram Bot API.

Kept independent of bot/ (a separate long-running polling process) — the web
panel process posts directly to api.telegram.org so it never needs IPC with
the bot process to reach the staff group.

The staff group is a Telegram forum (topics enabled). Each business event
gets its own topic instead of dumping everything into the group's General
feed — see CRM_*_TOPIC_ID below and OPERATIONS.md for the full topic map
(some topics are informational-only for now, with nothing posting into them
yet: online-marketplace orders, offline special orders).
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
_STAFF_GROUP_CHAT_ID = os.environ.get("CRM_STAFF_GROUP_CHAT_ID")
_REPAIR_TOPIC_ID = os.environ.get("CRM_REPAIR_TOPIC_ID")


def notify_staff_group(text: str, message_thread_id: str | int | None = None) -> None:
    """Post an HTML-formatted message to the staff group, if one is
    configured. Never raises — a failed/unconfigured notification must not
    break the request that triggered it (e.g. accepting a repair).
    `message_thread_id` targets a specific forum topic; omit for the
    group's General feed."""
    if not _STAFF_GROUP_CHAT_ID or not _BOT_TOKEN:
        return
    payload = {"chat_id": _STAFF_GROUP_CHAT_ID, "text": text, "parse_mode": "HTML"}
    if message_thread_id:
        payload["message_thread_id"] = message_thread_id
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff group notify failed: %s %s", resp.status_code, resp.text)
    except httpx.HTTPError:
        logger.warning("staff group notify failed", exc_info=True)


def notify_repair_card(text: str) -> None:
    """Post a new-repair card to the 'Ремонт техники' forum topic."""
    notify_staff_group(text, message_thread_id=_REPAIR_TOPIC_ID)
