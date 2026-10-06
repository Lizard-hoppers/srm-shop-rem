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


def normalize_sales_channel(value: str | None) -> str | None:
    """What staff type into Кабинет магазина -> what the Bot API accepts as
    chat_id: a public channel's @username (also accepted pasted as a
    t.me/... link or without the @), or a private channel's numeric
    -100... id. Empty -> None (channel publishing off for this store)."""
    value = (value or "").strip()
    if not value:
        return None
    for prefix in ("https://t.me/", "http://t.me/", "t.me/"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    value = value.strip("/")
    if value.lstrip("-").isdigit():
        return value
    return "@" + value.lstrip("@")


def set_sales_channel(conn: sqlite3.Connection, sales_channel: str | None) -> None:
    conn.execute(
        "UPDATE store_settings SET sales_channel = ?, updated_at = datetime('now') WHERE id = 1",
        (normalize_sales_channel(sales_channel),),
    )
