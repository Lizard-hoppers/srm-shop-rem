"""Cross-store Telegram-id lookups (Фаза B, 23.08) — the one place that
knows how to answer "which stores does this Telegram user have a staff
identity in", shared by the login flow (webapp/routers/miniapp.py) and the
store switcher (webapp/routers/store.py) so the store-scanning loop isn't
duplicated between them.
"""
from __future__ import annotations

import sqlite3

from core import auth
from core.storage import get_conn
from core.store_prefs import get_last_store
from core.stores import StoreConfig, load_stores


def accessible_stores(telegram_id: int) -> list[tuple[StoreConfig, sqlite3.Row]]:
    """(store, staff_row) for every configured store where an active staff
    member has this telegram_id — usually just one, but owner/admin get a
    separate identity row in each store's DB (Фаза A), so it can be more."""
    found = []
    for store in load_stores():
        with get_conn(store.db_path) as conn:
            staff = auth.get_staff_by_telegram_id(conn, telegram_id)
        if staff:
            found.append((store, staff))
    return found


def pick_default_store(telegram_id: int, accessible: list[tuple[StoreConfig, sqlite3.Row]]) -> StoreConfig:
    """Which store to land on when telegram_id resolves to more than one
    (only possible for owner/admin). A single match needs no preference.
    Multiple matches fall back to whichever store this Telegram user
    picked last via the switcher; with no preference recorded yet (or a
    stale one pointing at a store they no longer have access to), the
    first configured store wins."""
    stores_only = [s for s, _ in accessible]
    if len(stores_only) <= 1:
        return stores_only[0]
    last_id = get_last_store(telegram_id)
    match = next((s for s in stores_only if s.id == last_id), None)
    return match or stores_only[0]
