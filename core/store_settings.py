"""Точка's own profile, editable from the Mini App («Кабинет магазина») —
name/address/phone/hours and the sales channel. Lives on the точка's row in
`locations` since the bases were merged (06.10); before that it was a
singleton row in each store's own file (the old store_settings table is
left in place, unused, as the record of what was migrated).

location_id None means the first точка — see core.locations.
"""
from __future__ import annotations

import sqlite3

from core import locations as _locations


def get_settings(conn: sqlite3.Connection, location_id: int | None = None) -> sqlite3.Row:
    return _locations.get_location(conn, _locations.resolve(conn, location_id))


def update_settings(
    conn: sqlite3.Connection,
    name: str,
    address: str | None,
    phone: str | None,
    working_hours: str | None,
    location_id: int | None = None,
) -> None:
    conn.execute(
        "UPDATE locations SET name = ?, address = ?, phone = ?, working_hours = ?, "
        "updated_at = datetime('now') WHERE id = ?",
        (
            name.strip(),
            (address or "").strip() or None,
            (phone or "").strip() or None,
            (working_hours or "").strip() or None,
            _locations.resolve(conn, location_id),
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


def set_sales_channel(conn: sqlite3.Connection, sales_channel: str | None, location_id: int | None = None) -> None:
    conn.execute(
        "UPDATE locations SET sales_channel = ?, updated_at = datetime('now') WHERE id = ?",
        (normalize_sales_channel(sales_channel), _locations.resolve(conn, location_id)),
    )
