"""Suppliers and goods receipts (приход) — each item receipt also posts a
stock_movements row via core.inventory.record_movement, so stock and the
purchase paper trail never drift apart.
"""
from __future__ import annotations

import json
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


def list_receipts_for_product(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    """Every delivery of this product on record, newest first — the
    product card's "Поставщики этого товара" section, so staff can tell
    which supplier a batch that's turning out defective likely came from."""
    return conn.execute(
        """SELECT goods_receipt_items.id AS item_id, goods_receipt_items.qty, goods_receipt_items.unit_cost,
                  goods_receipts.id AS receipt_id, goods_receipts.created_at, goods_receipts.invoice_no,
                  goods_receipts.supplier_id, suppliers.name AS supplier_name
           FROM goods_receipt_items
           JOIN goods_receipts ON goods_receipts.id = goods_receipt_items.receipt_id
           LEFT JOIN suppliers ON suppliers.id = goods_receipts.supplier_id
           WHERE goods_receipt_items.product_id = ?
           ORDER BY goods_receipts.created_at DESC, goods_receipts.id DESC""",
        (product_id,),
    ).fetchall()


# ---- returns to a supplier (брак) ----

def create_supplier_return(
    conn: sqlite3.Connection,
    product_id: int,
    supplier_id: int,
    receipt_id: int | None,
    cell_id: int,
    qty: int,
    reason: str | None,
    staff_id: int,
) -> int:
    """Logs a defective-stock return to whichever supplier delivered it
    and writes off the qty from the cell via the normal stock ledger
    (reason='adjustment', tagged ref_type='supplier_return' so the
    movement history and this table stay linked) — raises
    core.inventory.InsufficientStockError same as any other write-off if
    the cell doesn't actually hold that much."""
    return_id = conn.execute(
        """INSERT INTO supplier_returns (product_id, supplier_id, receipt_id, cell_id, qty, reason, staff_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (product_id, supplier_id, receipt_id, cell_id, qty, reason, staff_id),
    ).lastrowid
    record_movement(
        conn, product_id, qty, "adjustment", staff_id,
        from_cell_id=cell_id, ref_type="supplier_return", ref_id=return_id,
        comment=f"Возврат поставщику: {reason}" if reason else "Возврат поставщику",
    )
    return return_id


def list_supplier_returns(conn: sqlite3.Connection, product_id: int | None = None, limit: int = 100) -> list[sqlite3.Row]:
    query = """SELECT supplier_returns.*, products.name AS product_name, products.unit,
                      suppliers.name AS supplier_name, staff.name AS staff_name,
                      storage_cells.code AS cell_code
               FROM supplier_returns
               JOIN products ON products.id = supplier_returns.product_id
               LEFT JOIN suppliers ON suppliers.id = supplier_returns.supplier_id
               LEFT JOIN staff ON staff.id = supplier_returns.staff_id
               LEFT JOIN storage_cells ON storage_cells.id = supplier_returns.cell_id"""
    params: list = []
    if product_id:
        query += " WHERE supplier_returns.product_id = ?"
        params.append(product_id)
    query += " ORDER BY supplier_returns.created_at DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


# ---- photo-of-invoice drafts (Уровень 3) ----

def create_draft(conn: sqlite3.Connection, staff_id: int, items: list[dict]) -> int:
    """items: core.purchase_import.match_items() output — a list of
    {"name_guess", "qty", "unit_cost", "product_id"} dicts."""
    return conn.execute(
        "INSERT INTO purchase_drafts (staff_id, items_json) VALUES (?, ?)",
        (staff_id, json.dumps(items, ensure_ascii=False)),
    ).lastrowid


def get_draft(conn: sqlite3.Connection, draft_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM purchase_drafts WHERE id = ?", (draft_id,)).fetchone()


def get_draft_items(conn: sqlite3.Connection, draft_id: int) -> list[dict]:
    draft = get_draft(conn, draft_id)
    return json.loads(draft["items_json"]) if draft else []


def mark_draft_applied(conn: sqlite3.Connection, draft_id: int) -> None:
    conn.execute("UPDATE purchase_drafts SET status = 'applied' WHERE id = ?", (draft_id,))
