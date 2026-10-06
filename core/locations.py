"""Точки бизнеса and their склады (06.10) — the `locations`/`warehouses`
tables behind core.stores' StoreConfig. One base holds every точка now, so
"which store" is a location_id on a row rather than which SQLite file is
open; this module is the small set of lookups the rest of core needs to
turn a location_id into the things that hang off it.

Convention used across core: a `location_id` argument of None on a READ
means "the whole business" (every точка), on a WRITE it means "the first
точка" — what a caller that predates точки (a CLI tool, an old test) gets.
Every web route and bot handler passes the real one.
"""
from __future__ import annotations

import sqlite3

from core import accounts as _accounts


def list_locations(conn: sqlite3.Connection, include_inactive: bool = False) -> list[sqlite3.Row]:
    query = "SELECT * FROM locations"
    if not include_inactive:
        query += " WHERE active = 1"
    return conn.execute(query + " ORDER BY id").fetchall()


def get_location(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM locations WHERE id = ?", (location_id,)).fetchone()


def default_location_id(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()["id"]


def resolve(conn: sqlite3.Connection, location_id: int | None) -> int:
    """The location a WRITE lands on — see the module docstring."""
    return location_id if location_id is not None else default_location_id(conn)


def create_location(conn: sqlite3.Connection, name: str) -> int:
    """A new точка, with its own 'point' склад and its default set of
    денежные счета from the start."""
    location_id = conn.execute("INSERT INTO locations (name) VALUES (?)", (name.strip(),)).lastrowid
    conn.execute(
        "INSERT INTO warehouses (kind, location_id, name) VALUES ('point', ?, 'Основной склад')", (location_id,)
    )
    _accounts.ensure_for_location(conn, location_id)
    return location_id


def point_warehouse_id(conn: sqlite3.Connection, location_id: int | None) -> int:
    """The точка's own склад — where a new cell goes unless told otherwise."""
    row = conn.execute(
        "SELECT id FROM warehouses WHERE kind = 'point' AND location_id = ?", (resolve(conn, location_id),)
    ).fetchone()
    return row["id"]


def cells_clause(location_id: int | None, column: str) -> tuple[str, list]:
    """SQL fragment + params narrowing `column` (a storage cell id) to the
    cells of one точка — "" and no params for the whole business. Every
    per-точка stock figure goes through this one definition of "a cell
    that belongs to this точка"."""
    if location_id is None:
        return "", []
    return (
        f""" AND {column} IN (SELECT storage_cells.id FROM storage_cells
                             JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
                             WHERE warehouses.location_id = ?)""",
        [location_id],
    )
