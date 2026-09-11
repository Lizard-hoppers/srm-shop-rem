"""Deep links straight into a specific CRM web-panel page — reuses the
exact same stateless ?t= session token the webapp itself mints at
/miniapp/auto (core.session_token), so a link built here needs no
separate login step: opening it lands the tapper straight on the target
page, already authenticated as whichever staff row the token was minted
for.

CRM_MINIAPP_URL (bot/config.MINIAPP_URL) points at the SPA's own login
page — .../miniapp — not the site root, so building a page URL under it
naively (as bot/quick_actions.py and bot/purchase_photo.py used to, e.g.
f"{MINIAPP_URL}/repairs/{id}") produced .../miniapp/repairs/{id}, which
404s: every real page (webapp/routers/repairs.py etc.) is mounted at the
site root, not under /miniapp (see webapp/main.py's app.include_router
calls). BASE_URL below strips that suffix once so every caller gets a
working link.

Security note: a token is bound to ONE staff_id at mint time — safe to
embed directly in a DM to that same person (every quick-intake confirm
does exactly that), but NEVER bake one into a message a GROUP chat keeps
around for other viewers to tap later (a repair card lives in a shared
staff group for its whole lifetime) — see bot/repair_actions.py's
open_crm_repair handler for the pattern that avoids this: mint the token
at tap time, for whoever actually tapped, and DM it to them instead of
embedding it in the shared card.
"""
from __future__ import annotations

from core.session_token import make_token

from bot.config import MINIAPP_URL

_MINIAPP_SUFFIX = "/miniapp"
BASE_URL = MINIAPP_URL[: -len(_MINIAPP_SUFFIX)] if MINIAPP_URL.endswith(_MINIAPP_SUFFIX) else MINIAPP_URL


def crm_link(path: str, staff_id: int, store_id: str) -> str:
    """path must start with "/", e.g. "/repairs/42"."""
    token = make_token(staff_id, store_id)
    return f"{BASE_URL}{path}?t={token}"
