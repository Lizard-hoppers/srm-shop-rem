"""Store registry — «store» in code is a точка of the business.

Since 06.10.2026 every точка lives in ONE base (see OPERATIONS.md «Единая
база»): the registry is the `locations` table, not stores.json, and every
StoreConfig carries the same db_path. The earlier layout — a separate
SQLite file per store, listed in stores.json (Фаза A, 23.08) — couldn't
express what the business actually needs: a transfer between точки, one
client history by phone number, a reserve held at another точка. StoreConfig
keeps its old shape (id as a string, db_path, the Telegram group ids) so
the ~100 call sites that say `store.db_path` / `store.id` didn't have to
change; `location_id` is the same id as the int the tables store.

Which base to read the registry from is re-resolved from CRM_DB_PATH on
every call (not cached at import time), so tests can point different
scenarios at different bases within one process.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass

from core import storage as _storage


@dataclass(frozen=True)
class StoreConfig:
    id: str
    name: str
    db_path: str
    staff_group_chat_id: int | None = None
    repair_topic_id: int | None = None
    masters_group_chat_id: int | None = None
    # Forum topic of the staff group for «Купить» leads from the sales
    # channel (bot/channel_orders.py); None -> the group's General feed.
    sales_topic_id: int | None = None

    @property
    def location_id(self) -> int:
        """The same id as an int — what location_id columns hold."""
        return int(self.id)


def registry_db_path() -> str:
    return os.environ.get("CRM_DB_PATH") or _storage.DB_PATH


def _fallback_store(db_path: str) -> list[StoreConfig]:
    """A base that hasn't been through init_db yet (first start, a bare
    CLI call) — one точка, so default_store_id() always has an answer."""
    return [StoreConfig(id="1", name="Магазин", db_path=db_path)]


def load_stores() -> list[StoreConfig]:
    db_path = registry_db_path()
    if not os.path.exists(db_path):
        return _fallback_store(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM locations WHERE active = 1 ORDER BY id").fetchall()
    except sqlite3.OperationalError:
        return _fallback_store(db_path)
    finally:
        conn.close()
    if not rows:
        return _fallback_store(db_path)
    return [
        StoreConfig(
            id=str(row["id"]),
            name=row["name"],
            db_path=db_path,
            staff_group_chat_id=row["staff_group_chat_id"],
            repair_topic_id=row["repair_topic_id"],
            masters_group_chat_id=row["masters_group_chat_id"],
            sales_topic_id=row["sales_topic_id"],
        )
        for row in rows
    ]


def get_store(store_id: str) -> StoreConfig:
    for store in load_stores():
        if store.id == str(store_id):
            return store
    raise KeyError(f"Unknown store_id: {store_id!r}")


def default_store_id() -> str:
    return load_stores()[0].id


def store_for_chat_id(chat_id: int | str) -> StoreConfig | None:
    """Which точка a Telegram group belongs to — a group message/button
    press unambiguously identifies its точка this way, unlike a DM (see
    core.store_access.accessible_stores/pick_default_store for that case,
    which resolves by the sender's identity instead)."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    for store in load_stores():
        if store.staff_group_chat_id == chat_id or store.masters_group_chat_id == chat_id:
            return store
    return None
