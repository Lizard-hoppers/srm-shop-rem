"""Products, storage cells, партии, stock levels and movements.

`stock_movements` is the audit ledger; `stock` (product_id, cell_id) -> qty
is the per-cell total kept in sync by record_movement so reads stay cheap,
and `batch_stock` is that same quantity broken down by партия (origin and
cost) — see the «партии» section below.
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
    is_serial: bool = False,
) -> int:
    cur = conn.execute(
        """INSERT INTO products (name, sku, category, unit, is_repair_part, is_sellable, min_qty, price, is_serial)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (name, sku or None, category, unit, int(is_repair_part), int(is_sellable), min_qty, price, int(bool(is_serial))),
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


def stock_value(conn: sqlite3.Connection, location_id: int | None = None):
    """«В товаре» — what the stock on hand cost to buy, партия by партия
    (qty × that партия's гривня cost). A партия with no cost on record
    (manual stock with no purchase history) counts as 0."""
    clause, params = _locations.cells_clause(location_id, "batch_stock.cell_id")
    row = conn.execute(
        f"""SELECT COALESCE(SUM(batch_stock.qty * COALESCE(batches.unit_cost_uah, 0)), 0) AS total
            FROM batch_stock JOIN batches ON batches.id = batch_stock.batch_id
            WHERE batch_stock.qty > 0{clause}""",
        params,
    ).fetchone()
    total = round(row["total"], 2)
    return int(total) if total == int(total) else total


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


# ---- партии ----
#
# Every unit of stock belongs to a партия (see the `batches` table): where
# it came from and what it cost. `stock` is the per-cell total, batch_stock
# the same quantity by партия; record_movement below is the only writer of
# both and keeps them equal.

class DuplicateImeiError(Exception):
    pass


class SerialUnitError(Exception):
    pass


def _latest_unit_cost(conn: sqlite3.Connection, product_id: int):
    """The product's most recent purchase cost in гривня — the price tag
    for stock arriving with no cost of its own (a manual «оприходование»).
    From the newest priced партия; falls back to the newest priced приход
    line for products last bought before партии existed."""
    row = conn.execute(
        "SELECT unit_cost_uah FROM batches WHERE product_id = ? AND unit_cost_uah IS NOT NULL ORDER BY id DESC LIMIT 1",
        (product_id,),
    ).fetchone()
    if row:
        return row["unit_cost_uah"]
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


def normalize_imei(value: str | None) -> str | None:
    """IMEI / serial as typed or scanned -> one canonical form (no spaces,
    upper case), None for blank."""
    cleaned = "".join(str(value or "").split()).upper()
    return cleaned or None


def find_unit_by_imei(conn: sqlite3.Connection, imei: str | None) -> sqlite3.Row | None:
    """The serial unit with this IMEI that is in stock right now, with
    where it is — None if there is no such unit on hand."""
    imei = normalize_imei(imei)
    if not imei:
        return None
    return conn.execute(
        """SELECT batches.*, batch_stock.cell_id, batch_stock.qty, products.name AS product_name
           FROM batches
           JOIN batch_stock ON batch_stock.batch_id = batches.id AND batch_stock.qty > 0
           JOIN products ON products.id = batches.product_id
           WHERE batches.imei = ? ORDER BY batches.id DESC LIMIT 1""",
        (imei,),
    ).fetchone()


def create_batch(
    conn: sqlite3.Connection,
    product_id: int,
    *,
    source: str = "manual",
    supplier_id: int | None = None,
    receipt_id: int | None = None,
    unit_cost=None,
    currency: str = "UAH",
    rate: float = 1,
    imei: str | None = None,
) -> int:
    """A new партия. unit_cost is in `currency`; its гривня value at `rate`
    is what every later document snapshots. An IMEI may exist on only one
    unit in stock at a time (the same phone can come back later — sold,
    bought again — as a new партия)."""
    imei = normalize_imei(imei)
    if imei and find_unit_by_imei(conn, imei):
        raise DuplicateImeiError(f"Устройство с IMEI {imei} уже числится на складе.")
    unit_cost_uah = None if unit_cost is None else round(float(unit_cost) * float(rate or 1), 2)
    return conn.execute(
        """INSERT INTO batches (product_id, supplier_id, receipt_id, source, unit_cost, currency, rate, unit_cost_uah, imei)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, supplier_id, receipt_id, source, unit_cost, currency or "UAH", rate or 1, unit_cost_uah, imei),
    ).lastrowid


def get_batch(conn: sqlite3.Connection, batch_id: int | None) -> sqlite3.Row | None:
    if not batch_id:
        return None
    return conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()


def _apply_batch_delta(conn: sqlite3.Connection, batch_id: int, cell_id: int, delta: int) -> None:
    row = conn.execute(
        "SELECT qty FROM batch_stock WHERE batch_id = ? AND cell_id = ?", (batch_id, cell_id)
    ).fetchone()
    new_qty = (row["qty"] if row else 0) + delta
    if new_qty < 0:
        raise InsufficientStockError(
            f"В этой партии на ячейке только {row['qty'] if row else 0}, требуется списать {-delta}"
        )
    if row:
        conn.execute("UPDATE batch_stock SET qty = ? WHERE batch_id = ? AND cell_id = ?", (new_qty, batch_id, cell_id))
    else:
        conn.execute("INSERT INTO batch_stock (batch_id, cell_id, qty) VALUES (?, ?, ?)", (batch_id, cell_id, new_qty))


def _cover_gap(conn: sqlite3.Connection, product_id: int, cell_id: int) -> None:
    """Self-heal for stock that reached a cell without a партия (a raw
    insert, data from before партии): the uncovered remainder becomes one
    'legacy' партия, same as core.storage's backfill does at startup."""
    total = conn.execute(
        "SELECT qty FROM stock WHERE product_id = ? AND cell_id = ?", (product_id, cell_id)
    ).fetchone()
    covered = conn.execute(
        """SELECT COALESCE(SUM(batch_stock.qty), 0) AS n FROM batch_stock
           JOIN batches ON batches.id = batch_stock.batch_id
           WHERE batches.product_id = ? AND batch_stock.cell_id = ?""",
        (product_id, cell_id),
    ).fetchone()["n"]
    gap = (total["qty"] if total else 0) - covered
    if gap > 0:
        batch_id = create_batch(conn, product_id, source="legacy", unit_cost=_latest_unit_cost(conn, product_id))
        _apply_batch_delta(conn, batch_id, cell_id, gap)


# ---- резерв ----
#
# «Доступно = физический остаток − резерв.» A reservation holds a quantity
# of one партия in one cell for one document (see stock_reservations).
# Everything that takes stock out of a cell goes through record_movement,
# which only lets a document take what is free — or what is held for IT
# (its `reserved_for`).

_ACTIVE_RESERVATION = "released_at IS NULL AND (expires_at IS NULL OR expires_at > datetime('now'))"


def reserved_qty(
    conn: sqlite3.Connection, batch_id: int, cell_id: int, exclude: tuple[str, int] | None = None,
) -> int:
    """How much of this партия in this cell is held — optionally not
    counting what is held for `exclude` (ref_type, ref_id) itself."""
    query = f"SELECT COALESCE(SUM(qty), 0) AS n FROM stock_reservations WHERE batch_id = ? AND cell_id = ? AND {_ACTIVE_RESERVATION}"
    params: list = [batch_id, cell_id]
    if exclude:
        query += " AND NOT (ref_type = ? AND ref_id = ?)"
        params += list(exclude)
    return conn.execute(query, params).fetchone()["n"]


def available_qty(
    conn: sqlite3.Connection, batch_id: int, cell_id: int, reserved_for: tuple[str, int] | None = None,
) -> int:
    held = conn.execute(
        "SELECT qty FROM batch_stock WHERE batch_id = ? AND cell_id = ?", (batch_id, cell_id)
    ).fetchone()
    return (held["qty"] if held else 0) - reserved_qty(conn, batch_id, cell_id, exclude=reserved_for)


def reserve(
    conn: sqlite3.Connection, batch_id: int, cell_id: int, qty: int, ref_type: str, ref_id: int,
    staff_id: int | None = None, expires_at: str | None = None,
) -> int:
    """Hold `qty` of a партия in a cell for a document. Refused
    (InsufficientStockError) if that much isn't free."""
    if qty <= 0:
        raise ValueError("qty должен быть положительным")
    free = available_qty(conn, batch_id, cell_id)
    if qty > free:
        raise InsufficientStockError(f"Свободно только {max(free, 0)} — остальное уже в резерве или списано.")
    return conn.execute(
        """INSERT INTO stock_reservations (batch_id, cell_id, qty, ref_type, ref_id, staff_id, expires_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, cell_id, qty, ref_type, ref_id, staff_id, expires_at),
    ).lastrowid


def release(
    conn: sqlite3.Connection, ref_type: str, ref_id: int, batch_id: int | None = None, cell_id: int | None = None,
) -> None:
    """Let go of everything held for a document (or just one партия/cell of it)."""
    query = "UPDATE stock_reservations SET released_at = datetime('now') WHERE ref_type = ? AND ref_id = ? AND released_at IS NULL"
    params: list = [ref_type, ref_id]
    if batch_id is not None:
        query += " AND batch_id = ?"
        params.append(batch_id)
    if cell_id is not None:
        query += " AND cell_id = ?"
        params.append(cell_id)
    conn.execute(query, params)


def _consume_reservation(conn: sqlite3.Connection, ref: tuple[str, int], batch_id: int, cell_id: int, qty: int) -> None:
    """A document took `qty` of what was held for it — its hold shrinks by
    that much (oldest rows first)."""
    rows = conn.execute(
        f"""SELECT id, qty FROM stock_reservations
            WHERE ref_type = ? AND ref_id = ? AND batch_id = ? AND cell_id = ? AND {_ACTIVE_RESERVATION}
            ORDER BY id""",
        (ref[0], ref[1], batch_id, cell_id),
    ).fetchall()
    left = qty
    for row in rows:
        take = min(left, row["qty"])
        if take == row["qty"]:
            conn.execute("UPDATE stock_reservations SET released_at = datetime('now') WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE stock_reservations SET qty = qty - ? WHERE id = ?", (take, row["id"]))
        left -= take
        if left == 0:
            break


def _allocate(
    conn: sqlite3.Connection, product_id: int, cell_id: int, qty: int, batch_id: int | None,
    reserved_for: tuple[str, int] | None = None,
) -> list[tuple[int, int]]:
    """Which партии an outgoing quantity comes from: the named one, or
    oldest-first (FIFO) across what the cell holds — only what is free (or
    held for `reserved_for`). [(batch_id, qty), …]; raises
    InsufficientStockError if that much isn't available, before anything
    is written."""
    physical = conn.execute(
        "SELECT qty FROM stock WHERE product_id = ? AND cell_id = ?", (product_id, cell_id)
    ).fetchone()
    on_hand = physical["qty"] if physical else 0
    if on_hand < qty:
        # The wording staff have always seen for a plain shortage.
        raise InsufficientStockError(f"Недостаточно товара на ячейке (есть {on_hand}, требуется списать {qty})")
    if batch_id is not None:
        held = conn.execute(
            "SELECT qty FROM batch_stock WHERE batch_id = ? AND cell_id = ?", (batch_id, cell_id)
        ).fetchone()
        if not held or held["qty"] < qty:
            raise InsufficientStockError(
                f"В этой партии на ячейке только {held['qty'] if held else 0}, требуется списать {qty}"
            )
        free = available_qty(conn, batch_id, cell_id, reserved_for)
        if free < qty:
            raise InsufficientStockError(f"Эта партия в резерве: свободно {max(free, 0)}, требуется {qty}.")
        return [(batch_id, qty)]
    rows = conn.execute(
        """SELECT batch_stock.batch_id, batch_stock.qty FROM batch_stock
           JOIN batches ON batches.id = batch_stock.batch_id
           WHERE batches.product_id = ? AND batch_stock.cell_id = ? AND batch_stock.qty > 0
           ORDER BY batch_stock.batch_id""",
        (product_id, cell_id),
    ).fetchall()
    allocations, left = [], qty
    for row in rows:
        free = row["qty"] - reserved_qty(conn, row["batch_id"], cell_id, exclude=reserved_for)
        take = min(left, max(free, 0))
        if take:
            allocations.append((row["batch_id"], take))
            left -= take
        if left == 0:
            break
    if left:
        raise InsufficientStockError(
            f"Недостаточно свободного товара на ячейке: есть {on_hand}, но {on_hand - (qty - left)} в резерве под другие документы."
        )
    return allocations


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
    batch_id: int | None = None,
    reserved_for: tuple[str, int] | None = None,
) -> int:
    """Apply a stock movement and write the audit row(s). qty is always
    positive. The only writer of `stock`, `batch_stock` and
    `stock_movements`.

    Stock leaving a cell comes from a specific партия — `batch_id` if the
    caller names one (a serial unit, a part picked from a given закупка),
    otherwise oldest-first; a quantity spanning several партии is written
    as one movement row per партия. Stock arriving from nowhere (no
    from_cell) lands in `batch_id`, or in a fresh 'manual' партия priced at
    the product's last known cost.

    Every row snapshots its партия's гривня cost (unit_cost) at write time
    — what a repair's or sale's profit is later netted against — so a
    later purchase at a different price never reshuffles the numbers of
    something already done. Returns the id of the first row written."""
    if qty <= 0:
        raise ValueError("qty должен быть положительным")
    if from_cell_id is None and to_cell_id is None:
        raise ValueError("нужна хотя бы одна ячейка (from или to)")

    if from_cell_id is not None:
        # Everything that can refuse is checked BEFORE the first write:
        # several callers catch InsufficientStockError inside their own
        # `with get_conn()` block and render an error page, which commits
        # — a half-applied movement must never be what gets committed.
        _cover_gap(conn, product_id, from_cell_id)
        # Only what is free may leave — or what is held for the very
        # document doing the taking (`reserved_for`).
        allocations = _allocate(conn, product_id, from_cell_id, qty, batch_id, reserved_for)
        _apply_stock_delta(conn, product_id, from_cell_id, -qty)
    else:
        if batch_id is None:
            batch_id = create_batch(conn, product_id, unit_cost=_latest_unit_cost(conn, product_id))
        allocations = [(batch_id, qty)]
    if to_cell_id is not None:
        _apply_stock_delta(conn, product_id, to_cell_id, qty)

    first_id = None
    for allocated_batch, allocated_qty in allocations:
        if from_cell_id is not None:
            _apply_batch_delta(conn, allocated_batch, from_cell_id, -allocated_qty)
            if reserved_for:
                _consume_reservation(conn, reserved_for, allocated_batch, from_cell_id, allocated_qty)
        if to_cell_id is not None:
            _apply_batch_delta(conn, allocated_batch, to_cell_id, allocated_qty)
        batch = get_batch(conn, allocated_batch)
        movement_id = conn.execute(
            """INSERT INTO stock_movements
               (product_id, from_cell_id, to_cell_id, qty, reason, ref_type, ref_id, staff_id, comment,
                unit_cost, batch_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (product_id, from_cell_id, to_cell_id, allocated_qty, reason, ref_type, ref_id, staff_id, comment,
             batch["unit_cost_uah"] if batch else None, allocated_batch),
        ).lastrowid
        first_id = first_id or movement_id
    return first_id


def list_batches(conn: sqlite3.Connection, product_id: int, in_stock_only: bool = True) -> list[sqlite3.Row]:
    """The product's партии with what is left of each and where — «чей
    именно товар»: supplier, приход, cost, and for a serial unit its IMEI."""
    having = "HAVING qty_left > 0" if in_stock_only else ""
    return conn.execute(
        f"""SELECT batches.*, suppliers.name AS supplier_name,
                   COALESCE(SUM(batch_stock.qty), 0) AS qty_left,
                   GROUP_CONCAT(CASE WHEN batch_stock.qty > 0
                                     THEN warehouses.name || ' · ' || storage_cells.code || ' × ' || batch_stock.qty END,
                                '; ') AS where_text
            FROM batches
            LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
            LEFT JOIN batch_stock ON batch_stock.batch_id = batches.id
            LEFT JOIN storage_cells ON storage_cells.id = batch_stock.cell_id
            LEFT JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
            WHERE batches.product_id = ?
            GROUP BY batches.id {having}
            ORDER BY batches.id DESC""",
        (product_id,),
    ).fetchall()


def stock_lines(
    conn: sqlite3.Connection, *, warehouse_id: int | None = None, location_id: int | None = None,
    product_id: int | None = None,
) -> list[sqlite3.Row]:
    """Stock on hand, one row per партия per cell — the picker for
    anything that must name a specific партия/IMEI (перемещение, продажа
    of a serial unit) and the contents list of a склад."""
    query = """SELECT batch_stock.batch_id, batch_stock.cell_id, batch_stock.qty,
                      COALESCE((SELECT SUM(r.qty) FROM stock_reservations r
                                WHERE r.batch_id = batch_stock.batch_id AND r.cell_id = batch_stock.cell_id
                                  AND r.released_at IS NULL
                                  AND (r.expires_at IS NULL OR r.expires_at > datetime('now'))), 0) AS reserved,
                      batches.product_id, batches.imei, batches.unit_cost_uah, batches.receipt_id,
                      products.is_repair_part,
                      products.name AS product_name, products.sku, products.unit, products.is_serial,
                      suppliers.name AS supplier_name, storage_cells.code AS cell_code,
                      warehouses.id AS warehouse_id, warehouses.name AS warehouse_name, warehouses.kind AS warehouse_kind,
                      warehouses.location_id AS location_id
               FROM batch_stock
               JOIN batches ON batches.id = batch_stock.batch_id
               JOIN products ON products.id = batches.product_id
               JOIN storage_cells ON storage_cells.id = batch_stock.cell_id
               JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
               LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
               WHERE batch_stock.qty > 0"""
    params: list = []
    if warehouse_id is not None:
        query += " AND warehouses.id = ?"
        params.append(warehouse_id)
    if location_id is not None:
        query += " AND warehouses.location_id = ?"
        params.append(location_id)
    if product_id is not None:
        query += " AND batches.product_id = ?"
        params.append(product_id)
    return conn.execute(query + " ORDER BY products.name, batch_stock.batch_id", params).fetchall()


def set_product_serial(conn: sqlite3.Connection, product_id: int, is_serial: bool) -> None:
    """Серийный товар (телефон): every unit is its own партия with an
    IMEI. Its own setter, like set_product_description — update_product is
    the full-replace behind the «Данные товара» form's original fields."""
    conn.execute("UPDATE products SET is_serial = ? WHERE id = ?", (int(bool(is_serial)), product_id))


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


def receive_stock(
    conn, product_id: int, cell_id: int, qty: int, staff_id: int, comment=None, key=None, imei: str | None = None,
) -> int:
    """Manual «оприходование» — stock with no supplier document behind it
    (found on a recount, the opening balance). A serial product comes in
    one unit at a time, with its IMEI."""
    batch_id = None
    product = get_product(conn, product_id)
    if product and product["is_serial"]:
        imei = normalize_imei(imei)
        if qty != 1 or not imei:
            raise SerialUnitError("Серийный товар добавляется по одному устройству, с IMEI.")
        batch_id = create_batch(conn, product_id, unit_cost=_latest_unit_cost(conn, product_id), imei=imei)
    movement_id = record_movement(
        conn, product_id, qty, "receipt", staff_id, to_cell_id=cell_id, comment=comment, batch_id=batch_id,
    )
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
        f"""SELECT cell_id,
                   qty - COALESCE((SELECT SUM(r.qty) FROM stock_reservations r
                                   JOIN batches b ON b.id = r.batch_id
                                   WHERE b.product_id = stock.product_id AND r.cell_id = stock.cell_id
                                     AND r.released_at IS NULL
                                     AND (r.expires_at IS NULL OR r.expires_at > datetime('now'))), 0) AS free
            FROM stock WHERE product_id = ?{clause}
            GROUP BY cell_id HAVING free >= ? ORDER BY free DESC LIMIT 1""",
        [product_id, *params, qty],
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
