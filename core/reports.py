"""Read-only aggregate queries for the Reports page. No caching/materialized
tables — the shop is small enough that these run fine on demand.

Every function takes an optional location_id: one точка, or the whole
business when None (see core.locations). A cancelled sale never counts.
"""
from __future__ import annotations

import sqlite3


def _loc(location_id: int | None, column: str) -> tuple[str, list]:
    return (f" AND {column} = ?", [location_id]) if location_id is not None else ("", [])


def repairs_by_status(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    clause, params = _loc(location_id, "location_id")
    return conn.execute(
        f"SELECT status, COUNT(*) AS n FROM repair_orders WHERE 1=1{clause} GROUP BY status ORDER BY n DESC",
        params,
    ).fetchall()


def repairs_by_master(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    clause, params = _loc(location_id, "repair_orders.location_id")
    return conn.execute(
        f"""SELECT staff.name AS master_name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN repair_orders.status = 'issued' THEN 1 ELSE 0 END) AS issued
            FROM repair_orders
            JOIN staff ON staff.id = repair_orders.master_id
            WHERE 1=1{clause}
            GROUP BY repair_orders.master_id
            ORDER BY total DESC""",
        params,
    ).fetchall()


def avg_repair_turnaround_days(conn: sqlite3.Connection, location_id: int | None = None) -> float | None:
    clause, params = _loc(location_id, "location_id")
    row = conn.execute(
        f"""SELECT AVG(julianday(issued_at) - julianday(created_at)) AS avg_days
            FROM repair_orders WHERE issued_at IS NOT NULL{clause}""",
        params,
    ).fetchone()
    return round(row["avg_days"], 1) if row["avg_days"] is not None else None


def sales_by_channel(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    clause, params = _loc(location_id, "sales_orders.location_id")
    return conn.execute(
        f"""SELECT sales_orders.channel,
                   COUNT(DISTINCT sales_orders.id) AS orders,
                   COALESCE(SUM(sales_order_items.qty * sales_order_items.price), 0) AS revenue
            FROM sales_orders
            LEFT JOIN sales_order_items ON sales_order_items.order_id = sales_orders.id
            WHERE sales_orders.status != 'cancelled'{clause}
            GROUP BY sales_orders.channel""",
        params,
    ).fetchall()


def repairs_revenue(conn: sqlite3.Connection, location_id: int | None = None) -> int:
    clause, params = _loc(location_id, "location_id")
    row = conn.execute(
        f"SELECT COALESCE(SUM(price_final), 0) AS total FROM repair_orders WHERE status = 'issued'{clause}",
        params,
    ).fetchone()
    return row["total"]
