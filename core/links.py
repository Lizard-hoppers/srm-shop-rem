"""Deep links into the Mini App for messages sent from the WEB process
(core.notify) — the bot process has its own builder, bot/miniapp_links.py,
which this mirrors: the page URL with a ?t= session token minted for the
ONE person the message goes to. Only ever put such a link into a private
chat with that person, never into a group (see bot/miniapp_links.py).
"""
from __future__ import annotations

import os

from core.session_token import make_token

_SUFFIX = "/miniapp"


def miniapp_link(path: str, staff_id: int, store_id: str) -> str | None:
    """None when CRM_MINIAPP_URL isn't configured (tests, a dev machine) —
    callers then just leave the button out."""
    base = os.environ.get("CRM_MINIAPP_URL") or ""
    if not base:
        return None
    if base.endswith(_SUFFIX):
        base = base[: -len(_SUFFIX)]
    return f"{base}{path}?t={make_token(staff_id, store_id)}"
