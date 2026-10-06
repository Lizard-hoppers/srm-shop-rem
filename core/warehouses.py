"""Склады (Заход 3, 06.10): «где товар и кто за него отвечает». Three
kinds — a точка's own склад ('point'), a master's материально-
ответственный склад ('master': what he has at home is on HIS books, not
lost), and the single «В пути» ('transit') goods sit in between «Передал»
and «Принял». Stock itself always lives in cells (core.inventory); a склад
is what groups cells and names who answers for them.
"""
from __future__ import annotations

import sqlite3

from core import storage as _storage

KIND_LABELS = {"point": "Точка", "master": "Мастер", "transit": "В пути"}

# Who may send from / receive into a точка's склад.
STOCK_ROLES = ("owner", "admin", "storekeeper")
_EVERYWHERE_ROLES = ("owner", "admin")


_SELECT = """SELECT warehouses.*, locations.name AS location_name, staff.name AS staff_name
             FROM warehouses
             LEFT JOIN locations ON locations.id = warehouses.location_id
             LEFT JOIN staff ON staff.id = warehouses.staff_id"""


def display_name(warehouse) -> str:
    """«007» for a точка's склад, «Мастер: Сергей» for a master's, «В пути»."""
    if warehouse["kind"] == "point":
        return warehouse["location_name"] or warehouse["name"]
    if warehouse["kind"] == "master":
        return f"Мастер: {warehouse['staff_name'] or warehouse['name']}"
    return "В пути"


def list_warehouses(conn: sqlite3.Connection, include_inactive_masters: bool = False) -> list[sqlite3.Row]:
    """Every склад: точки first, then masters, «В пути» last. A deactivated
    master's склад is listed only while it still holds something."""
    rows = conn.execute(
        _SELECT + """ WHERE warehouses.active = 1
                      ORDER BY CASE warehouses.kind WHEN 'point' THEN 0 WHEN 'master' THEN 1 ELSE 2 END,
                               warehouses.location_id, staff.name"""
    ).fetchall()
    if include_inactive_masters:
        return rows
    result = []
    for row in rows:
        if row["kind"] == "master":
            master = conn.execute("SELECT active FROM staff WHERE id = ?", (row["staff_id"],)).fetchone()
            if master and not master["active"] and total_qty(conn, row["id"]) == 0:
                continue
        result.append(row)
    return result


def get_warehouse(conn: sqlite3.Connection, warehouse_id: int | None) -> sqlite3.Row | None:
    if not warehouse_id:
        return None
    return conn.execute(_SELECT + " WHERE warehouses.id = ?", (warehouse_id,)).fetchone()


def point_warehouse(conn: sqlite3.Connection, location_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _SELECT + " WHERE warehouses.kind = 'point' AND warehouses.location_id = ?", (location_id,)
    ).fetchone()


def master_warehouse(conn: sqlite3.Connection, staff_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _SELECT + " WHERE warehouses.kind = 'master' AND warehouses.staff_id = ?", (staff_id,)
    ).fetchone()


def transit_cell_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """SELECT storage_cells.id FROM storage_cells
           JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
           WHERE warehouses.kind = 'transit' ORDER BY storage_cells.id LIMIT 1"""
    ).fetchone()
    return row["id"]


def cells(conn: sqlite3.Connection, warehouse_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM storage_cells WHERE warehouse_id = ? ORDER BY code", (warehouse_id,)
    ).fetchall()


def ensure_for_master(conn: sqlite3.Connection, staff_id: int, name: str) -> int:
    """Called whenever a master is added or renamed — every master has a
    склад from the moment he exists."""
    return _storage.ensure_master_warehouse(conn, staff_id, name)


def total_qty(conn: sqlite3.Connection, warehouse_id: int) -> int:
    row = conn.execute(
        """SELECT COALESCE(SUM(stock.qty), 0) AS n FROM stock
           JOIN storage_cells ON storage_cells.id = stock.cell_id
           WHERE storage_cells.warehouse_id = ?""",
        (warehouse_id,),
    ).fetchone()
    return row["n"]


def summary(conn: sqlite3.Connection) -> list[dict]:
    """Every склад with how much it holds and what that cost — the «Склады»
    overview («товар не пропадает у мастера»)."""
    totals = {
        row["warehouse_id"]: row
        for row in conn.execute(
            """SELECT storage_cells.warehouse_id AS warehouse_id, SUM(batch_stock.qty) AS qty,
                      SUM(batch_stock.qty * COALESCE(batches.unit_cost_uah, 0)) AS value,
                      COUNT(DISTINCT batches.product_id) AS products
               FROM batch_stock
               JOIN batches ON batches.id = batch_stock.batch_id
               JOIN storage_cells ON storage_cells.id = batch_stock.cell_id
               WHERE batch_stock.qty > 0 GROUP BY storage_cells.warehouse_id"""
        ).fetchall()
    }
    result = []
    for warehouse in list_warehouses(conn):
        row = totals.get(warehouse["id"])
        value = round(row["value"], 2) if row else 0
        result.append({
            **dict(warehouse), "display_name": display_name(warehouse),
            "qty": row["qty"] if row else 0, "products": row["products"] if row else 0,
            "value": int(value) if value == int(value) else value,
        })
    return result


def sendable_from(conn: sqlite3.Connection, staff: sqlite3.Row, location_id: int) -> list[sqlite3.Row]:
    """Склады this person may send goods OUT of: a master — only his own;
    a storekeeper — the склад of the точка they work at; owner/admin —
    any (except «В пути», which nobody sends from by hand)."""
    if staff["role"] == "master":
        own = master_warehouse(conn, staff["id"])
        return [own] if own else []
    if staff["role"] in _EVERYWHERE_ROLES:
        return [w for w in list_warehouses(conn) if w["kind"] != "transit"]
    if staff["role"] in STOCK_ROLES:
        own = point_warehouse(conn, location_id)
        return [own] if own else []
    return []


def may_receive(conn: sqlite3.Connection, staff: sqlite3.Row, warehouse: sqlite3.Row, staff_location_ids: set[int]) -> bool:
    """«Принял» is said by whoever now answers for the goods: the master
    himself for his склад, stock staff of that точка for a точка's —
    and an owner/admin for any."""
    if staff["role"] in _EVERYWHERE_ROLES:
        return True
    if warehouse["kind"] == "master":
        return warehouse["staff_id"] == staff["id"]
    if warehouse["kind"] == "point":
        return staff["role"] in STOCK_ROLES and warehouse["location_id"] in staff_location_ids
    return False
