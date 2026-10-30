"""Suppliers and goods receipts (приход) — each item receipt also posts a
stock_movements row via core.inventory.record_movement, so stock and the
purchase paper trail never drift apart.
"""
from __future__ import annotations

import json
import sqlite3

from core import documents as _documents
from core import locations as _locations
from core import inventory as _inventory
from core import warehouses as _warehouses
from core.inventory import cell_location_id, record_movement


class ReceiptError(Exception):
    """A приход that can't be проведён as entered — message is shown to staff as-is."""


def list_suppliers(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM suppliers ORDER BY name").fetchall()


def create_supplier(conn: sqlite3.Connection, name: str, contact: str | None) -> int:
    return conn.execute(
        "INSERT INTO suppliers (name, contact) VALUES (?, ?)", (name, contact)
    ).lastrowid


def _supplier(conn: sqlite3.Connection, supplier_id: int | None) -> sqlite3.Row | None:
    if not supplier_id:
        return None
    return conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()


def create_receipt(
    conn: sqlite3.Connection,
    supplier_id: int | None,
    invoice_no: str | None,
    staff_id: int,
    items: list[tuple],
    *,
    location_id: int | None = None,
    key: str | None = None,
    currency: str = "UAH",
    rate: float = 1,
) -> int:
    """items: list of (product_id, cell_id, qty, unit_cost) or, for a
    serial product, (product_id, cell_id, qty, unit_cost, [imei, …]).

    Every line becomes a партия — this supplier, this приход, this cost —
    and the stock arrives into it. unit_cost is in the receipt's `currency`
    at `rate` гривня per unit of it (one currency per накладная). A serial
    product's line becomes one партия PER UNIT, each with its IMEI: the
    list must hold exactly `qty` distinct IMEIs, none already in stock.
    ReceiptError before anything is written otherwise."""
    if not items:
        raise ValueError("нужна хотя бы одна позиция в приходе")
    location_id = _locations.resolve(conn, location_id)
    currency = currency or "UAH"
    rate = float(rate or 1) if currency != "UAH" else 1

    # Validate serial lines up front — nothing must be half-received.
    normalized = []
    seen_imeis: set[str] = set()
    for item in items:
        product_id, cell_id, qty, unit_cost = item[:4]
        imeis = [_inventory.normalize_imei(x) for x in (item[4] if len(item) > 4 and item[4] else [])]
        imeis = [x for x in imeis if x]
        product = _inventory.get_product(conn, product_id)
        if product and product["is_serial"]:
            if len(imeis) != qty:
                raise ReceiptError(
                    f"«{product['name']}» — серийный товар: нужно {qty} IMEI, указано {len(imeis)}."
                )
            for imei in imeis:
                if imei in seen_imeis:
                    raise ReceiptError(f"IMEI {imei} указан дважды.")
                if _inventory.find_unit_by_imei(conn, imei):
                    raise ReceiptError(f"Устройство с IMEI {imei} уже числится на складе.")
                seen_imeis.add(imei)
        else:
            imeis = []
        normalized.append((product_id, cell_id, qty, unit_cost, imeis))

    receipt_id = conn.execute(
        "INSERT INTO goods_receipts (supplier_id, invoice_no, staff_id, location_id, currency, rate) VALUES (?, ?, ?, ?, ?, ?)",
        (supplier_id, invoice_no, staff_id, location_id, currency, rate),
    ).lastrowid

    for product_id, cell_id, qty, unit_cost, imeis in normalized:
        conn.execute(
            "INSERT INTO goods_receipt_items (receipt_id, product_id, cell_id, qty, unit_cost) VALUES (?, ?, ?, ?, ?)",
            (receipt_id, product_id, cell_id, qty, unit_cost),
        )
        for imei, unit_qty in ([(imei, 1) for imei in imeis] or [(None, qty)]):
            batch_id = _inventory.create_batch(
                conn, product_id, source="receipt", supplier_id=supplier_id, receipt_id=receipt_id,
                unit_cost=unit_cost, currency=currency, rate=rate, imei=imei,
            )
            record_movement(
                conn, product_id, unit_qty, "receipt", staff_id,
                to_cell_id=cell_id, ref_type="goods_receipt", ref_id=receipt_id, batch_id=batch_id,
            )

    supplier = _supplier(conn, supplier_id)
    total = round(sum(qty * (unit_cost or 0) * rate for _p, _c, qty, unit_cost, _i in normalized), 2)
    total = int(total) if total == int(total) else total
    _documents.register(
        conn, "receipt", staff_id=staff_id, location_id=location_id,
        client_id=supplier["client_id"] if supplier else None,
        ref_table="goods_receipts", ref_id=receipt_id, title=supplier["name"] if supplier else None,
        amount=total or None, key=key,
    )
    return receipt_id


def list_receipts(conn: sqlite3.Connection, limit: int = 100, location_id: int | None = None) -> list[sqlite3.Row]:
    where, params = ("WHERE goods_receipts.location_id = ?", [location_id]) if location_id is not None else ("", [])
    return conn.execute(
        f"""SELECT goods_receipts.*, suppliers.name AS supplier_name, staff.name AS staff_name
            FROM goods_receipts
            LEFT JOIN suppliers ON suppliers.id = goods_receipts.supplier_id
            LEFT JOIN staff ON staff.id = goods_receipts.staff_id
            {where}
            ORDER BY goods_receipts.created_at DESC
            LIMIT ?""",
        [*params, limit],
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


def get_receipt_batches(conn: sqlite3.Connection, receipt_id: int) -> list[sqlite3.Row]:
    """The партии this приход created, with what is left of each — where
    its goods went is one glance away."""
    return conn.execute(
        """SELECT batches.*, products.name AS product_name, products.unit,
                  COALESCE((SELECT SUM(qty) FROM batch_stock WHERE batch_id = batches.id), 0) AS qty_left
           FROM batches JOIN products ON products.id = batches.product_id
           WHERE batches.receipt_id = ? ORDER BY batches.id""",
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


# ---- один товар из бота (30.10) ----

def receive_one(
    conn: sqlite3.Connection,
    *,
    staff_id: int,
    location_id: int,
    warehouse_id: int,
    qty: int,
    price: int,
    product_id: int | None = None,
    name: str | None = None,
    sku: str | None = None,
    key: str | None = None,
) -> dict:
    """«📦 Приход → ➕ Добавить товар» в боте: один товар — один ПХ.

    `price` — цена ПРОДАЖИ за штуку (её спрашивает бот), она становится
    ценой товара. Закупка неизвестна — партия без себестоимости. Товар
    находится по product_id или штрихкоду, иначе заводится новый (на
    продажу, «шт»). Ложится в первую ячейку выбранного склада точки.
    Повтор с тем же `key` ничего не проводит и возвращает уже сделанное.
    ReceiptError (текст для сотрудника) — до записи чего-либо."""
    done = _documents.find_by_key(conn, key)
    if done:
        return _receive_one_result(conn, done)
    if not isinstance(qty, int) or not 1 <= qty <= 9999:
        raise ReceiptError("Количество — целое число от 1 до 9999.")
    if not price or price <= 0:
        raise ReceiptError("Укажите цену продажи больше нуля.")
    warehouse = _warehouses.get_warehouse(conn, warehouse_id)
    if not warehouse or warehouse["kind"] != "point" or warehouse["location_id"] != location_id:
        raise ReceiptError("Этот склад не относится к вашей точке.")
    cell_id = _warehouses.receive_cell_id(conn, warehouse_id)
    if not cell_id:
        raise ReceiptError("У склада нет ячейки — заведите её в приложении.")

    sku = (sku or "").strip() or None
    product = _inventory.get_product(conn, product_id) if product_id else None
    if not product and sku:
        product = _inventory.get_product_by_sku(conn, sku)
    if product and product["is_serial"]:
        raise ReceiptError(f"«{product['name']}» — товар с IMEI, его приходуют в приложении.")
    if product:
        conn.execute("UPDATE products SET price = ? WHERE id = ?", (price, product["id"]))
        product_id = product["id"]
    else:
        name = " ".join((name or "").split())
        if not name:
            raise ReceiptError("Нужно название товара.")
        product_id = _inventory.create_product(
            conn, name=name, sku=sku, category=None, unit="шт",
            is_repair_part=False, is_sellable=True, min_qty=0, price=price,
        )

    receipt_id = create_receipt(conn, None, None, staff_id, [(product_id, cell_id, qty, None)],
                                location_id=location_id, key=key)
    return _receive_one_result(conn, _documents.get_for(conn, "receipt", receipt_id), created=product is None)


def _receive_one_result(conn: sqlite3.Connection, doc: sqlite3.Row, created: bool = False) -> dict:
    item = conn.execute(
        """SELECT goods_receipt_items.*, products.name, products.price, storage_cells.warehouse_id
           FROM goods_receipt_items
           JOIN products ON products.id = goods_receipt_items.product_id
           JOIN storage_cells ON storage_cells.id = goods_receipt_items.cell_id
           WHERE goods_receipt_items.receipt_id = ? ORDER BY goods_receipt_items.id LIMIT 1""",
        (doc["ref_id"],),
    ).fetchone()
    in_warehouse = conn.execute(
        """SELECT COALESCE(SUM(stock.qty), 0) AS n FROM stock
           JOIN storage_cells ON storage_cells.id = stock.cell_id
           WHERE stock.product_id = ? AND storage_cells.warehouse_id = ?""",
        (item["product_id"], item["warehouse_id"]),
    ).fetchone()["n"]
    return {
        "doc_id": doc["id"], "label": _documents.doc_label(doc), "receipt_id": doc["ref_id"],
        "product_id": item["product_id"], "name": item["name"], "qty": item["qty"], "price": item["price"],
        "warehouse_id": item["warehouse_id"], "in_warehouse": in_warehouse, "created": created,
    }


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
    *,
    key: str | None = None,
    batch_id: int | None = None,
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
        batch_id=batch_id or _return_batch(conn, product_id, supplier_id, receipt_id, cell_id, qty),
    )
    supplier = _supplier(conn, supplier_id)
    product = conn.execute("SELECT name FROM products WHERE id = ?", (product_id,)).fetchone()
    _documents.register(
        conn, "supplier_return", staff_id=staff_id, location_id=cell_location_id(conn, cell_id),
        client_id=supplier["client_id"] if supplier else None,
        ref_table="supplier_returns", ref_id=return_id,
        title=f"{product['name']} × {qty}" if product else None, key=key,
    )
    return return_id


def _return_batch(
    conn: sqlite3.Connection, product_id: int, supplier_id: int, receipt_id: int | None, cell_id: int, qty: int,
) -> int | None:
    """Which партия a return comes out of when the caller didn't name one:
    that приход's own партия if the cell still holds enough of it, else
    any партия of that supplier that does — so the брак is charged to the
    supplier who actually delivered it. None (plain FIFO) if neither."""
    conditions = []
    if receipt_id:
        conditions.append(("batches.receipt_id = ?", receipt_id))
    conditions.append(("batches.supplier_id = ?", supplier_id))
    for clause, value in conditions:
        row = conn.execute(
            f"""SELECT batch_stock.batch_id FROM batch_stock
                JOIN batches ON batches.id = batch_stock.batch_id
                WHERE batches.product_id = ? AND batch_stock.cell_id = ? AND batch_stock.qty >= ? AND {clause}
                ORDER BY batch_stock.batch_id LIMIT 1""",
            (product_id, cell_id, qty, value),
        ).fetchone()
        if row:
            return row["batch_id"]
    return None


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
