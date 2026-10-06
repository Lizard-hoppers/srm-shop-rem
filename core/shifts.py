"""Смены (Заход 2, 06.10). An employee starts the day at a точка by
opening their shift: the system shows that точка's balances — every
account, plus what the whole business holds in goods — and they answer
«всё совпадает» or «есть расхождение». Either way the shift opens; a
discrepancy is written down with the shift (and lands in the journal and,
later, the owner's «Проблемы») — it is a signal, not a correction: «видят
все, корректируют только владельцы».

One shift is one person at one точка for one Kyiv calendar day. Yesterday's
shift that nobody closed simply stops being current; opening today's closes
it for the record.
"""
from __future__ import annotations

import json
import sqlite3

from core import accounts as _accounts
from core import documents as _documents
from core import inventory as _inventory
from core.timefmt import kyiv_date_range_utc, kyiv_today


def snapshot(conn: sqlite3.Connection, location_id: int) -> dict:
    """What «сверить остатки» shows: the точка's account balances and the
    value of goods across the whole business."""
    return {
        "accounts": [
            {"id": a["id"], "name": a["name"], "kind": a["kind"], "currency": a["currency"],
             "currency_label": a["currency_label"], "balance": a["balance"]}
            for a in _accounts.balances(conn, location_id)
        ],
        "stock_value": _inventory.stock_value(conn),
    }


def current_shift(conn: sqlite3.Connection, staff_id: int, location_id: int) -> sqlite3.Row | None:
    """This person's open shift at this точка, if it was opened today."""
    today = kyiv_today()
    day_start, day_end = kyiv_date_range_utc(today, today)
    return conn.execute(
        """SELECT * FROM shifts
           WHERE staff_id = ? AND location_id = ? AND closed_at IS NULL AND opened_at >= ? AND opened_at < ?
           ORDER BY id DESC LIMIT 1""",
        (staff_id, location_id, day_start, day_end),
    ).fetchone()


def open_shift(
    conn: sqlite3.Connection, staff_id: int, location_id: int, discrepancy_note: str | None = None,
    key: str | None = None,
) -> int:
    """Open today's shift (returns the already-open one if there is one —
    a second tap changes nothing)."""
    existing = current_shift(conn, staff_id, location_id)
    if existing:
        return existing["id"]
    conn.execute(
        "UPDATE shifts SET closed_at = datetime('now') WHERE staff_id = ? AND closed_at IS NULL", (staff_id,)
    )
    note = (discrepancy_note or "").strip() or None
    shift_id = conn.execute(
        "INSERT INTO shifts (location_id, staff_id, discrepancy_note, snapshot_json) VALUES (?, ?, ?, ?)",
        (location_id, staff_id, note, json.dumps(snapshot(conn, location_id), ensure_ascii=False)),
    ).lastrowid
    _documents.register(
        conn, "shift", staff_id=staff_id, location_id=location_id, ref_table="shifts", ref_id=shift_id,
        title=f"Расхождение: {note}" if note else "Остатки совпадают", key=key,
    )
    return shift_id


def close_shift(conn: sqlite3.Connection, shift_id: int, staff_id: int) -> None:
    shift = conn.execute("SELECT * FROM shifts WHERE id = ?", (shift_id,)).fetchone()
    if not shift or shift["closed_at"]:
        return
    conn.execute(
        "UPDATE shifts SET closed_at = datetime('now'), closing_snapshot_json = ? WHERE id = ?",
        (json.dumps(snapshot(conn, shift["location_id"]), ensure_ascii=False), shift_id),
    )
    doc = _documents.get_for(conn, "shift", shift_id)
    if doc:
        _documents.add_event(conn, doc["id"], "closed", staff_id)


def list_discrepancies(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    """Shifts opened with «есть расхождение», newest first."""
    return conn.execute(
        """SELECT shifts.*, staff.name AS staff_name, locations.name AS location_name
           FROM shifts
           JOIN staff ON staff.id = shifts.staff_id
           JOIN locations ON locations.id = shifts.location_id
           WHERE shifts.discrepancy_note IS NOT NULL
           ORDER BY shifts.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
