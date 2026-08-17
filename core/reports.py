"""Read-only aggregate queries for the Reports page. No caching/materialized
tables — the shop is small enough that these run fine on demand.
"""
from __future__ import annotations

import sqlite3


def repairs_by_status(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT status, COUNT(*) AS n FROM repair_orders GROUP BY status ORDER BY n DESC"
    ).fetchall()


def repairs_by_master(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT staff.name AS master_name,
                  COUNT(*) AS total,
                  SUM(CASE WHEN repair_orders.status = 'issued' THEN 1 ELSE 0 END) AS issued
           FROM repair_orders
           JOIN staff ON staff.id = repair_orders.master_id
           GROUP BY repair_orders.master_id
           ORDER BY total DESC"""
    ).fetchall()


def avg_repair_turnaround_days(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        """SELECT AVG(julianday(issued_at) - julianday(created_at)) AS avg_days
           FROM repair_orders WHERE issued_at IS NOT NULL"""
    ).fetchone()
    return round(row["avg_days"], 1) if row["avg_days"] is not None else None


def sales_by_channel(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT sales_orders.channel,
                  COUNT(DISTINCT sales_orders.id) AS orders,
                  COALESCE(SUM(sales_order_items.qty * sales_order_items.price), 0) AS revenue
           FROM sales_orders
           LEFT JOIN sales_order_items ON sales_order_items.order_id = sales_orders.id
           GROUP BY sales_orders.channel"""
    ).fetchall()


def repairs_revenue(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(price_final), 0) AS total FROM repair_orders WHERE status = 'issued'"
    ).fetchone()
    return row["total"]
