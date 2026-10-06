"""Products, storage cells, stock levels and movements.

`stock_movements` is the audit ledger; `stock` (product_id, cell_id) -> qty
is a cache kept in sync by _apply_stock_delta so reads stay cheap.
"""
from __future__ import annotations

import sqlite3

from core import documents as _documents
from core import locations as _locations


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


def get_product_by_sku(conn: sqlite3.Connection, sku: str) -> sqlite3.Row | None:
    """Exact SKU lookup — how a scanned barcode (the product's own SKU
    digits, see core.barcode_label) resolves back to a product, as
    opposed to list_products()'s fuzzy LIKE search for the UI's search box."""
    sku = sku.strip()
    if not sku:
        return None
    return conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()


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


def update_product(
    conn: sqlite3.Connection,
    product_id: int,
    name: str,
    sku: str | None,
    category: str | None,
    unit: str,
    is_repair_part: bool,
    is_sellable: bool,
    min_qty: int,
    price: int | None,
) -> None:
    conn.execute(
        """UPDATE products SET name = ?, sku = ?, category = ?, unit = ?, is_repair_part = ?,
                                is_sellable = ?, min_qty = ?, price = ?
           WHERE id = ?""",
        (name, sku or None, category, unit, int(is_repair_part), int(is_sellable), min_qty, price, product_id),
    )


def set_product_photo(conn: sqlite3.Connection, product_id: int, photo_filename: str | None) -> None:
    conn.execute("UPDATE products SET photo_path = ? WHERE id = ?", (photo_filename, product_id))


def set_product_description(conn: sqlite3.Connection, product_id: int, description: str | None) -> None:
    """Customer-facing text for the sales-channel card (core.channel_posts)
    — condition, memory, what's in the box. Its own setter rather than
    another update_product() argument: that one is a full-replace driven by
    the «Данные товара» form, and this field isn't on that form."""
    conn.execute(
        "UPDATE products SET description = ? WHERE id = ?", ((description or "").strip() or None, product_id)
    )


def product_stock_by_cell(
    conn: sqlite3.Connection, product_id: int, location_id: int | None = None
) -> list[sqlite3.Row]:
    clause, params = _locations.cells_clause(location_id, "stock.cell_id")
    return conn.execute(
        f"""SELECT stock.cell_id, storage_cells.code, stock.qty
            FROM stock JOIN storage_cells ON storage_cells.id = stock.cell_id
            WHERE stock.product_id = ? AND stock.qty != 0{clause}
            ORDER BY storage_cells.code""",
        [product_id, *params],
    ).fetchall()


def product_total_qty(conn: sqlite3.Connection, product_id: int, location_id: int | None = None) -> int:
    clause, params = _locations.cells_clause(location_id, "stock.cell_id")
    row = conn.execute(
        f"SELECT COALESCE(SUM(qty), 0) AS total FROM stock WHERE product_id = ?{clause}", [product_id, *params]
    ).fetchone()
    return row["total"]


def list_products_with_stock(
    conn: sqlite3.Connection, search: str | None = None, low_stock_only: bool = False,
    location_id: int | None = None,
) -> list[sqlite3.Row]:
    """The catalog is one for the whole business; total_qty is what the
    given точка holds (every точка together when location_id is None)."""
    clause, params = _locations.cells_clause(location_id, "stock.cell_id")
    query = f"""SELECT products.*, COALESCE(SUM(stock.qty), 0) AS total_qty
               FROM products
               LEFT JOIN stock ON stock.product_id = products.id{clause}
               WHERE products.active = 1"""
    if search:
        query += " AND (products.name LIKE ? OR products.sku LIKE ?)"
        like = f"%{search}%"
        params += [like, like]
    query += " GROUP BY products.id"
    if low_stock_only:
        query += " HAVING total_qty <= products.min_qty"
    query += " ORDER BY products.name"
    return conn.execute(query, params).fetchall()


def low_stock_report(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    clause, params = _locations.cells_clause(location_id, "stock.cell_id")
    return conn.execute(
        f"""SELECT products.*, COALESCE(SUM(stock.qty), 0) AS total_qty
            FROM products
            LEFT JOIN stock ON stock.product_id = products.id{clause}
            WHERE products.active = 1
            GROUP BY products.id
            HAVING total_qty <= products.min_qty
            ORDER BY total_qty ASC""",
        params,
    ).fetchall()


# ---- storage cells ----

class DuplicateCellError(Exception):
    pass


def list_cells(conn: sqlite3.Connection, location_id: int | None = None) -> list[sqlite3.Row]:
    clause, params = _locations.cells_clause(location_id, "storage_cells.id")
    return conn.execute(f"SELECT * FROM storage_cells WHERE 1=1{clause} ORDER BY code", params).fetchall()


def cell_location_id(conn: sqlite3.Connection, cell_id: int | None) -> int | None:
    """The точка a cell's склад belongs to (None for a master's or the
    «В пути» склад) — how a stock document learns where it happened."""
    if cell_id is None:
        return None
    row = conn.execute(
        """SELECT warehouses.location_id FROM storage_cells
           JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
           WHERE storage_cells.id = ?""",
        (cell_id,),
    ).fetchone()
    return row["location_id"] if row else None


def create_cell(
    conn: sqlite3.Connection, code: str, zone: str | None, note: str | None, location_id: int | None = None
) -> int:
    """A new cell in the точка's own склад. Cell codes are unique across
    the whole business (a schema constraint from the single-store days) —
    DuplicateCellError instead of a raw IntegrityError when one is taken."""
    try:
        cur = conn.execute(
            "INSERT INTO storage_cells (code, zone, note, warehouse_id) VALUES (?, ?, ?, ?)",
            (code, zone, note, _locations.point_warehouse_id(conn, location_id)),
        )
    except sqlite3.IntegrityError as exc:
        raise DuplicateCellError(f"Ячейка с кодом «{code}» уже есть — выберите другой код.") from exc
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


def _latest_unit_cost(conn: sqlite3.Connection, product_id: int) -> int | None:
    """Best-known cost basis for a product right now — the unit_cost of its
    most recent goods receipt. Parts from different suppliers aren't
    segregated by cell (see core.purchases.create_supplier_return's
    docstring — deliberately not batch-tracked), so this is an
    approximation, not the exact cost of the specific unit consumed —
    good enough for a repair's profit estimate (see core.masters), not
    precise accounting."""
    row = conn.execute(
        """SELECT goods_receipt_items.unit_cost
           FROM goods_receipt_items
           JOIN goods_receipts ON goods_receipts.id = goods_receipt_items.receipt_id
           WHERE goods_receipt_items.product_id = ? AND goods_receipt_items.unit_cost IS NOT NULL
           ORDER BY goods_receipts.created_at DESC, goods_receipts.id DESC
           LIMIT 1""",
        (product_id,),
    ).fetchone()
    return row["unit_cost"] if row else None


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
    """Apply a stock movement and write the audit row. qty is always positive.

    A 'repair_use' movement snapshots the product's current unit_cost onto
    the row (see _latest_unit_cost) — core.masters nets this against a
    repair's price_final to estimate a master's profit-based payout.
    Snapshotted at write time, not looked up later, so a subsequent
    purchase-price change never silently reshuffles an already-issued
    repair's numbers."""
    if qty <= 0:
        raise ValueError("qty должен быть положительным")
    if from_cell_id is None and to_cell_id is None:
        raise ValueError("нужна хотя бы одна ячейка (from или to)")

    if from_cell_id is not None:
        _apply_stock_delta(conn, product_id, from_cell_id, -qty)
    if to_cell_id is not None:
        _apply_stock_delta(conn, product_id, to_cell_id, qty)

    unit_cost = _latest_unit_cost(conn, product_id) if reason == "repair_use" else None
    cur = conn.execute(
        """INSERT INTO stock_movements
           (product_id, from_cell_id, to_cell_id, qty, reason, ref_type, ref_id, staff_id, comment, unit_cost)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, from_cell_id, to_cell_id, qty, reason, ref_type, ref_id, staff_id, comment, unit_cost),
    )
    return cur.lastrowid


# The three manual stock operations (Склад → Движения, «Добавить остаток» on
# a product card). Each is its own document in the journal — «остаток нельзя
# править цифрой, только документом» — unlike a movement made BY another
# document (a sale, a приход, a repair), which carries that one's ref_type.

def _manual_movement_document(
    conn, doc_type: str, movement_id: int, product_id: int, qty: int, staff_id: int, cell_id: int, key: str | None,
) -> None:
    product = get_product(conn, product_id)
    _documents.register(
        conn, doc_type, staff_id=staff_id, location_id=cell_location_id(conn, cell_id),
        ref_table="stock_movements", ref_id=movement_id,
        title=f"{product['name']} × {qty}" if product else None, key=key,
    )


def receive_stock(conn, product_id: int, cell_id: int, qty: int, staff_id: int, comment=None, key=None) -> int:
    movement_id = record_movement(conn, product_id, qty, "receipt", staff_id, to_cell_id=cell_id, comment=comment)
    _manual_movement_document(conn, "stock_in", movement_id, product_id, qty, staff_id, cell_id, key)
    return movement_id


def write_off_stock(conn, product_id: int, cell_id: int, qty: int, staff_id: int, comment=None, key=None) -> int:
    movement_id = record_movement(
        conn, product_id, qty, "adjustment", staff_id, from_cell_id=cell_id, comment=comment
    )
    _manual_movement_document(conn, "writeoff", movement_id, product_id, qty, staff_id, cell_id, key)
    return movement_id


def transfer_stock(
    conn, product_id: int, from_cell_id: int, to_cell_id: int, qty: int, staff_id: int, comment=None, key=None,
) -> int:
    movement_id = record_movement(
        conn, product_id, qty, "transfer", staff_id, from_cell_id=from_cell_id, to_cell_id=to_cell_id, comment=comment
    )
    _manual_movement_document(conn, "transfer", movement_id, product_id, qty, staff_id, from_cell_id, key)
    return movement_id


def pick_cell_with_stock(
    conn: sqlite3.Connection, product_id: int, qty: int, location_id: int | None = None
) -> int | None:
    """Find a cell holding at least qty of product_id, for callers that don't
    need the cashier/master to pick a specific cell (sales, repair part use)
    — among the given точка's cells only: a sale at one точка must never
    quietly take stock that is physically at another."""
    clause, params = _locations.cells_clause(location_id, "stock.cell_id")
    row = conn.execute(
        f"SELECT cell_id FROM stock WHERE product_id = ? AND qty >= ?{clause} ORDER BY qty DESC LIMIT 1",
        [product_id, qty, *params],
    ).fetchone()
    return row["cell_id"] if row else None


def default_cell_by_product(conn: sqlite3.Connection, location_id: int | None = None) -> dict[int, int]:
    """For every product, the cell most likely to be 'where it lives' —
    whichever cell currently holds the most of it. Used to pre-fill the
    cell picker on goods-receipt intake so restocking the same item
    doesn't require re-picking its cell every time. No qty threshold (see
    pick_cell_with_stock() for that variant) — a product being restocked
    is often already at 0 everywhere. One query for the whole catalog, for
    embedding into a page's JS context rather than looking up per product."""
    clause1, params1 = _locations.cells_clause(location_id, "s1.cell_id")
    clause2, params2 = _locations.cells_clause(location_id, "s2.cell_id")
    rows = conn.execute(
        f"""SELECT product_id, cell_id FROM stock s1
            WHERE qty = (SELECT MAX(qty) FROM stock s2 WHERE s2.product_id = s1.product_id{clause2}){clause1}
            GROUP BY product_id""",
        [*params2, *params1],
    ).fetchall()
    return {row["product_id"]: row["cell_id"] for row in rows}


def list_movements(conn: sqlite3.Connection, limit: int = 100, location_id: int | None = None) -> list[sqlite3.Row]:
    where, params = "", []
    if location_id is not None:
        from_clause, from_params = _locations.cells_clause(location_id, "stock_movements.from_cell_id")
        to_clause, to_params = _locations.cells_clause(location_id, "stock_movements.to_cell_id")
        where = f"WHERE (1=1{from_clause}) OR (1=1{to_clause})"
        params = [*from_params, *to_params]
    return conn.execute(
        f"""SELECT stock_movements.*, products.name AS product_name,
                   fc.code AS from_cell_code, tc.code AS to_cell_code
            FROM stock_movements
            JOIN products ON products.id = stock_movements.product_id
            LEFT JOIN storage_cells fc ON fc.id = stock_movements.from_cell_id
            LEFT JOIN storage_cells tc ON tc.id = stock_movements.to_cell_id
            {where}
            ORDER BY stock_movements.created_at DESC, stock_movements.id DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
