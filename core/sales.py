"""Offline POS-lite sales. Each sold item deducts stock from whichever cell
has enough of it — the cashier doesn't need to think about warehouse layout
at the register, only the storekeeper does (via Inventory).
"""
from __future__ import annotations

import sqlite3

from core import cash as core_cash
from core import documents as _documents
from core import locations as _locations
from core.inventory import InsufficientStockError, pick_cell_with_stock, record_movement


def create_sale(
    conn: sqlite3.Connection,
    client_id: int | None,
    channel: str,
    staff_id: int,
    items: list[tuple[int, int, int]],
    warranty_until: str | None = None,
    payment_method: str = "cash",
    *,
    location_id: int | None = None,
    key: str | None = None,
) -> int:
    """items: list of (product_id, qty, price). Records the sale total in
    the касса ledger (core.cash.record_income) same transaction as the
    order itself — a sale and its payment are one atomic event here,
    there's no separate "collect payment later" step in this shop's flow."""
    if not items:
        raise ValueError("нужна хотя бы одна позиция в продаже")
    location_id = _locations.resolve(conn, location_id)

    order_id = conn.execute(
        """INSERT INTO sales_orders (client_id, channel, status, staff_id, warranty_until, payment_method, location_id)
           VALUES (?, ?, 'completed', ?, ?, ?, ?)""",
        (client_id, channel, staff_id, warranty_until, payment_method, location_id),
    ).lastrowid

    total = 0
    for product_id, qty, price in items:
        cell_id = pick_cell_with_stock(conn, product_id, qty, location_id)
        if cell_id is None:
            raise InsufficientStockError(f"Недостаточно товара (product_id={product_id}) ни на одной ячейке этой точки")
        conn.execute(
            "INSERT INTO sales_order_items (order_id, product_id, qty, price) VALUES (?, ?, ?, ?)",
            (order_id, product_id, qty, price),
        )
        record_movement(
            conn, product_id, qty, "sale", staff_id,
            from_cell_id=cell_id, ref_type="sales_order", ref_id=order_id,
        )
        total += qty * price

    core_cash.record_income(
        conn, payment_method, total, "sales_order", order_id, staff_id, location_id=location_id
    )
    _documents.register(
        conn, "sale", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="sales_orders", ref_id=order_id, amount=total, key=key,
    )

    return order_id


def list_sales(
    conn: sqlite3.Connection, limit: int = 100, location_id: int | None = None, include_cancelled: bool = True,
) -> list[sqlite3.Row]:
    """Newest first. A cancelled sale (status='cancelled', see
    core.doc_cancel) stays in the list for the page to mark — pass
    include_cancelled=False wherever sales are being COUNTED or summed."""
    where, params = [], []
    if location_id is not None:
        where.append("sales_orders.location_id = ?")
        params.append(location_id)
    if not include_cancelled:
        where.append("sales_orders.status != 'cancelled'")
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    return conn.execute(
        f"""SELECT sales_orders.*, clients.name AS client_name, staff.name AS staff_name,
                   (SELECT COALESCE(SUM(qty * price), 0) FROM sales_order_items WHERE order_id = sales_orders.id) AS total
            FROM sales_orders
            LEFT JOIN clients ON clients.id = sales_orders.client_id
            LEFT JOIN staff ON staff.id = sales_orders.staff_id
            {where_sql}
            ORDER BY sales_orders.created_at DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()


def list_sales_by_client(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT sales_orders.*, staff.name AS staff_name,
                  (SELECT COALESCE(SUM(qty * price), 0) FROM sales_order_items WHERE order_id = sales_orders.id) AS total
           FROM sales_orders
           LEFT JOIN staff ON staff.id = sales_orders.staff_id
           WHERE sales_orders.client_id = ?
           ORDER BY sales_orders.created_at DESC""",
        (client_id,),
    ).fetchall()


def get_sale(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT sales_orders.*, clients.name AS client_name, staff.name AS staff_name
           FROM sales_orders
           LEFT JOIN clients ON clients.id = sales_orders.client_id
           LEFT JOIN staff ON staff.id = sales_orders.staff_id
           WHERE sales_orders.id = ?""",
        (order_id,),
    ).fetchone()


def get_sale_items(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT sales_order_items.*, products.name AS product_name
           FROM sales_order_items
           JOIN products ON products.id = sales_order_items.product_id
           WHERE order_id = ?""",
        (order_id,),
    ).fetchall()
