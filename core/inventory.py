"""Products, storage cells, stock levels and movements.

`stock_movements` is the audit ledger; `stock` (product_id, cell_id) -> qty
is a cache kept in sync by _apply_stock_delta so reads stay cheap.
"""
from __future__ import annotations

import sqlite3


class InsufficientStockError(Exception):
    pass


# ---- products ----

def list_products(conn: sqlite3.Connection, search: str | None = None) -> list[sqlite3.Row]:
    if search:
        like = f"%{search}%"
        return conn.execute(
            "SELECT * FROM products WHERE active = 1 AND (name LIKE ? OR sku LIKE ?) ORDER BY name",
            (like, like),
        ).fetchall()
    return conn.execute("SELECT * FROM products WHERE active = 1 ORDER BY name").fetchall()


def get_product(conn: sqlite3.Connection, product_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()


def create_product(
    conn: sqlite3.Connection,
    name: str,
    sku: str | None,
    category: str | None,
    unit: str,
    is_repair_part: bool,
    is_sellable: bool,
    min_qty: int,
    price: int | None,
) -> int:
    cur = conn.execute(
        """INSERT INTO products (name, sku, category, unit, is_repair_part, is_sellable, min_qty, price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, sku or None, category, unit, int(is_repair_part), int(is_sellable), min_qty, price),
    )
    return cur.lastrowid


def product_stock_by_cell(conn: sqlite3.Connection, product_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT stock.cell_id, storage_cells.code, stock.qty
           FROM stock JOIN storage_cells ON storage_cells.id = stock.cell_id
           WHERE stock.product_id = ? AND stock.qty != 0
           ORDER BY storage_cells.code""",
        (product_id,),
    ).fetchall()


def product_total_qty(conn: sqlite3.Connection, product_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(qty), 0) AS total FROM stock WHERE product_id = ?", (product_id,)
    ).fetchone()
    return row["total"]


def list_products_with_stock(
    conn: sqlite3.Connection, search: str | None = None, low_stock_only: bool = False
) -> list[sqlite3.Row]:
    query = """SELECT products.*, COALESCE(SUM(stock.qty), 0) AS total_qty
               FROM products
               LEFT JOIN stock ON stock.product_id = products.id
               WHERE products.active = 1"""
    params: list = []
    if search:
        query += " AND (products.name LIKE ? OR products.sku LIKE ?)"
        like = f"%{search}%"
        params += [like, like]
    query += " GROUP BY products.id"
    if low_stock_only:
        query += " HAVING total_qty <= products.min_qty"
    query += " ORDER BY products.name"
    return conn.execute(query, params).fetchall()


def low_stock_report(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT products.*, COALESCE(SUM(stock.qty), 0) AS total_qty
           FROM products
           LEFT JOIN stock ON stock.product_id = products.id
           WHERE products.active = 1
           GROUP BY products.id
           HAVING total_qty <= products.min_qty
           ORDER BY total_qty ASC"""
    ).fetchall()


# ---- storage cells ----

def list_cells(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM storage_cells ORDER BY code").fetchall()


def create_cell(conn: sqlite3.Connection, code: str, zone: str | None, note: str | None) -> int:
    cur = conn.execute(
        "INSERT INTO storage_cells (code, zone, note) VALUES (?, ?, ?)", (code, zone, note)
    )
    return cur.lastrowid


# ---- stock movements ----

def _apply_stock_delta(conn: sqlite3.Connection, product_id: int, cell_id: int, delta: int) -> None:
    row = conn.execute(
        "SELECT qty FROM stock WHERE product_id = ? AND cell_id = ?", (product_id, cell_id)
    ).fetchone()
    current = row["qty"] if row else 0
    new_qty = current + delta
    if new_qty < 0:
        raise InsufficientStockError(
            f"Недостаточно товара на ячейке (есть {current}, требуется списать {-delta})"
        )
    if row:
        conn.execute(
            "UPDATE stock SET qty = ? WHERE product_id = ? AND cell_id = ?",
            (new_qty, product_id, cell_id),
        )
    else:
        conn.execute(
            "INSERT INTO stock (product_id, cell_id, qty) VALUES (?, ?, ?)",
            (product_id, cell_id, new_qty),
        )


def record_movement(
    conn: sqlite3.Connection,
    product_id: int,
    qty: int,
    reason: str,
    staff_id: int,
    from_cell_id: int | None = None,
    to_cell_id: int | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    comment: str | None = None,
) -> int:
    """Apply a stock movement and write the audit row. qty is always positive."""
    if qty <= 0:
        raise ValueError("qty должен быть положительным")
    if from_cell_id is None and to_cell_id is None:
        raise ValueError("нужна хотя бы одна ячейка (from или to)")

    if from_cell_id is not None:
        _apply_stock_delta(conn, product_id, from_cell_id, -qty)
    if to_cell_id is not None:
        _apply_stock_delta(conn, product_id, to_cell_id, qty)

    cur = conn.execute(
        """INSERT INTO stock_movements
           (product_id, from_cell_id, to_cell_id, qty, reason, ref_type, ref_id, staff_id, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, from_cell_id, to_cell_id, qty, reason, ref_type, ref_id, staff_id, comment),
    )
    return cur.lastrowid


def receive_stock(conn, product_id: int, cell_id: int, qty: int, staff_id: int, comment=None) -> int:
    return record_movement(conn, product_id, qty, "receipt", staff_id, to_cell_id=cell_id, comment=comment)


def write_off_stock(conn, product_id: int, cell_id: int, qty: int, staff_id: int, comment=None) -> int:
    return record_movement(conn, product_id, qty, "adjustment", staff_id, from_cell_id=cell_id, comment=comment)


def transfer_stock(conn, product_id: int, from_cell_id: int, to_cell_id: int, qty: int, staff_id: int, comment=None) -> int:
    return record_movement(
        conn, product_id, qty, "transfer", staff_id, from_cell_id=from_cell_id, to_cell_id=to_cell_id, comment=comment
    )


def list_movements(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT stock_movements.*, products.name AS product_name,
                  fc.code AS from_cell_code, tc.code AS to_cell_code
           FROM stock_movements
           JOIN products ON products.id = stock_movements.product_id
           LEFT JOIN storage_cells fc ON fc.id = stock_movements.from_cell_id
           LEFT JOIN storage_cells tc ON tc.id = stock_movements.to_cell_id
           ORDER BY stock_movements.created_at DESC, stock_movements.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
