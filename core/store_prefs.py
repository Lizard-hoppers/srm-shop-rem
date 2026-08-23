"""Per-Telegram-ID "last used store" preference (Фаза B, 23.08).

Staff identity is spread across each store's own SQLite file (see
core/stores.py) — there's no single one of them where "which store did
this Telegram user use last" naturally belongs. It lives in its own tiny
standalone SQLite file instead, a sibling of the store DBs, independent of
any one store's schema/lifecycle/gitignore entry (`*.sqlite3` already
covers it).

The default path is re-read from CRM_STORE_PREFS_PATH on every call, not
cached at import time — same reasoning as core.stores._stores_config_path():
core.store_access.pick_default_store() calls get_last_store() with no
explicit db_path, so a module-level constant frozen at first import would
silently ignore a test (or a future caller) pointing CRM_STORE_PREFS_PATH
somewhere else afterward.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS store_prefs (
    telegram_id INTEGER PRIMARY KEY,
    store_id TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def default_db_path() -> str:
    return os.environ.get(
        "CRM_STORE_PREFS_PATH",
        os.path.join(os.path.dirname(__file__), "..", "store_prefs.sqlite3"),
    )


def init_db(db_path: str | None = None) -> None:
    conn = sqlite3.connect(db_path or default_db_path())
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _connect(db_path: str | None):
    conn = sqlite3.connect(db_path or default_db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_last_store(telegram_id: int, db_path: str | None = None) -> str | None:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT store_id FROM store_prefs WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()
    return row["store_id"] if row else None


def set_last_store(telegram_id: int, store_id: str, db_path: str | None = None) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO store_prefs (telegram_id, store_id, updated_at) VALUES (?, ?, datetime('now')) "
            "ON CONFLICT(telegram_id) DO UPDATE SET store_id = excluded.store_id, updated_at = excluded.updated_at",
            (telegram_id, store_id),
        )
