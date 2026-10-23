"""Which точки a Telegram user may work in — shared by the login flow
(webapp/routers/miniapp.py), the store switcher (webapp/routers/store.py)
and every bot DM handler.

One base, one staff row per person (06.10): an owner/admin works across
every точка; anyone else is tied to their own (staff.location_id, the first
точка if never set). Before the merge this scanned a separate SQLite file
per store looking for an identity row in each — the (store, staff_row)
return shape is kept from then, so callers didn't change.
"""
from __future__ import annotations

import sqlite3

from core import auth
from core import bots
from core.storage import get_conn
from core.store_prefs import get_last_store
from core.stores import StoreConfig, load_stores, registry_db_path

_ALL_LOCATIONS_ROLES = ("owner", "admin")


def stores_for_staff(staff: sqlite3.Row) -> list[StoreConfig]:
    stores = load_stores()
    if staff["role"] in _ALL_LOCATIONS_ROLES:
        return stores
    home = str(staff["location_id"]) if staff["location_id"] else stores[0].id
    return [s for s in stores if s.id == home]


def accessible_stores(telegram_id: int) -> list[tuple[StoreConfig, sqlite3.Row]]:
    """(store, staff_row) for every точка this Telegram user can work in —
    empty if they aren't active staff at all."""
    with get_conn(registry_db_path()) as conn:
        staff = auth.get_staff_by_telegram_id(conn, telegram_id)
    if not staff:
        return []
    # Through a точка's own bot — that точка only; through the default bot —
    # the точки that have no bot of their own (core.bots, 23.10).
    stores = bots.stores_for_bot(stores_for_staff(staff), bots.CURRENT_BOT_ID.get())
    return [(store, staff) for store in stores]


def home_bot_username(telegram_id: int) -> str | None:
    """Staff who wrote to a bot that doesn't serve their точка: the
    @username of the one that does — so /start says where to go instead of
    taking them for a walk-in client. None if they aren't staff at all."""
    with get_conn(registry_db_path()) as conn:
        staff = auth.get_staff_by_telegram_id(conn, telegram_id)
    if not staff:
        return None
    home = stores_for_staff(staff)
    return bots.username(bots.token_for_location(home[0].location_id)) if home else None


def pick_default_store(telegram_id: int, accessible: list[tuple[StoreConfig, sqlite3.Row]]) -> StoreConfig:
    """Which точка to land on when there is more than one to choose from
    (owner/admin). Falls back to whichever one this Telegram user picked
    last via the switcher; with no preference recorded yet (or a stale one
    pointing at a точка they can no longer reach), the first one wins."""
    stores_only = [s for s, _ in accessible]
    if len(stores_only) <= 1:
        return stores_only[0]
    last_id = get_last_store(telegram_id)
    match = next((s for s in stores_only if s.id == last_id), None)
    return match or stores_only[0]
