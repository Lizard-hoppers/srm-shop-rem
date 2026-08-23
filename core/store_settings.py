"""Store profile editable from the Mini App (Фаза B, 23.08) — a shop's own
name/address/phone/hours, distinct from stores.json (infra-only: db_path +
Telegram group ids, see core/stores.py, deployed by hand, never edited from
the app). One singleton row (id=1) per store's own DB — core.storage.init_db()
already creates the table and seeds the row.
"""
from __future__ import annotations

import sqlite3


def get_settings(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM store_settings WHERE id = 1").fetchone()


def update_settings(
    conn: sqlite3.Connection,
    name: str,
    address: str | None,
    phone: str | None,
    working_hours: str | None,
) -> None:
    conn.execute(
        "UPDATE store_settings SET name = ?, address = ?, phone = ?, working_hours = ?, "
        "updated_at = datetime('now') WHERE id = 1",
        (
            name.strip(),
            (address or "").strip() or None,
            (phone or "").strip() or None,
            (working_hours or "").strip() or None,
        ),
    )
