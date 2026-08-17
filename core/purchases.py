"""Suppliers and goods receipts (приход) — each item receipt also posts a
stock_movements row via core.inventory.record_movement, so stock and the
purchase paper trail never drift apart.
"""
from __future__ import annotations

import sqlite3

from core.inventory import record_movement


def list_suppliers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()


def create_supplier(conn: sqlite3.Connection, name: str, contact: str | None) -> int:
    return conn.execute(
        "INSERT INTO suppliers (name, contact) VALUES (?, ?)", (name, contact)
    ).lastrowid


def create_receipt(
    conn: sqlite3.Connection,
    supplier_id: int | None,
    invoice_no: str | None,
    staff_id: int,
    items: list[tuple[int, int, int, int | None]],
) -> int:
    """items: list of (product_id, cell_id, qty, unit_cost)."""
    if not items:
        raise ValueError("нужна хотя бы одна позиция в приходе")

    receipt_id = conn.execute(
        "INSERT INTO goods_receipts (supplier_id, invoice_no, staff_id) VALUES (?, ?, ?)",
        (supplier_id, invoice_no, staff_id),
    ).lastrowid

    for product_id, cell_id, qty, unit_cost in items:
        conn.execute(
            "INSERT INTO goods_receipt_items (receipt_id, product_id, cell_id, qty, unit_cost) VALUES (?, ?, ?, ?, ?)",
            (receipt_id, product_id, cell_id, qty, unit_cost),
        )
        record_movement(
            conn, product_id, qty, "receipt", staff_id,
            to_cell_id=cell_id, ref_type="goods_receipt", ref_id=receipt_id,
        )

    return receipt_id


def list_receipts(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT goods_receipts.*, suppliers.name AS supplier_name, staff.name AS staff_name
           FROM goods_receipts
           LEFT JOIN suppliers ON suppliers.id = goods_receipts.supplier_id
           LEFT JOIN staff ON staff.id = goods_receipts.staff_id
           ORDER BY goods_receipts.created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()


def get_receipt(conn: sqlite3.Connection, receipt_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT goods_receipts.*, suppliers.name AS supplier_name, staff.name AS staff_name
           FROM goods_receipts
           LEFT JOIN suppliers ON suppliers.id = goods_receipts.supplier_id
           LEFT JOIN staff ON staff.id = goods_receipts.staff_id
           WHERE goods_receipts.id = ?""",
        (receipt_id,),
    ).fetchone()


def get_receipt_items(conn: sqlite3.Connection, receipt_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT goods_receipt_items.*, products.name AS product_name, storage_cells.code AS cell_code
           FROM goods_receipt_items
           JOIN products ON products.id = goods_receipt_items.product_id
           LEFT JOIN storage_cells ON storage_cells.id = goods_receipt_items.cell_id
           WHERE receipt_id = ?""",
        (receipt_id,),
    ).fetchall()
