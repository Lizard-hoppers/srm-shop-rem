"""Best-effort staff notifications via the raw Telegram Bot API.

Kept independent of bot/ (a separate long-running polling process) — the web
panel process posts directly to api.telegram.org so it never needs IPC with
the bot process to reach the staff group.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")
_STAFF_GROUP_CHAT_ID = os.environ.get("CRM_STAFF_GROUP_CHAT_ID")


def notify_staff_group(text: str) -> None:
    """Post an HTML-formatted message to the staff group, if one is
    configured. Never raises — a failed/unconfigured notification must not
    break the request that triggered it (e.g. accepting a repair)."""
    if not _STAFF_GROUP_CHAT_ID or not _BOT_TOKEN:
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
            json={"chat_id": _STAFF_GROUP_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=5,
        )
        if resp.status_code != 200:
            logger.warning("staff group notify failed: %s %s", resp.status_code, resp.text)
    except httpx.HTTPError:
        logger.warning("staff group notify failed", exc_info=True)
