"""Offline POS-lite sales. Each sold item deducts stock from whichever cell
has enough of it — the cashier doesn't need to think about warehouse layout
at the register, only the storekeeper does (via Inventory).
"""
from __future__ import annotations

import sqlite3

from core import cash as core_cash
from core import documents as _documents
from core import locations as _locations
from core import settlements as _settlements
from core.accounts import money
from core.inventory import InsufficientStockError, available_qty, get_product, pick_cell_with_stock, record_movement


class SaleError(Exception):
    """A sale that can't go through as entered — message is shown to staff as-is."""


def _batch_cell(conn: sqlite3.Connection, batch_id: int, product_id: int, qty: int, location_id: int) -> int | None:
    """The cell of THIS точка holding enough of the named партия — None if
    it isn't here (sold already, at another точка, with a master, in transit)."""
    clause, params = _locations.cells_clause(location_id, "batch_stock.cell_id")
    row = conn.execute(
        f"""SELECT batch_stock.cell_id FROM batch_stock
            JOIN batches ON batches.id = batch_stock.batch_id
            WHERE batch_stock.batch_id = ? AND batches.product_id = ? AND batch_stock.qty >= ?{clause}
            ORDER BY batch_stock.qty DESC LIMIT 1""",
        [batch_id, product_id, qty, *params],
    ).fetchone()
    if not row:
        return None
    # Physically here, but held for another document (a производство
    # order, a client's заказ) is not sellable either.
    return row["cell_id"] if available_qty(conn, batch_id, row["cell_id"]) >= qty else None


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
    payments: list[tuple[int, object, object]] | None = None,
    allow_debt: bool = False,
    reserved_for: tuple[str, int] | None = None,
) -> int:
    """items: list of (product_id, qty, price) or (product_id, qty, price,
    batch_id) — the 4th element names the exact партия/IMEI being sold,
    required for a serial product (which unit is leaving matters), optional
    otherwise (oldest партия first); a 5th names the cell too (a заказ
    sells exactly the lines it held). allow_debt: the client may pay less
    than the total now — the rest goes onto his balance («нам должны»,
    core.settlements), so it needs a client. reserved_for: the document
    whose резерв this sale is taking from. Each line remembers the cost of what
    it actually took (sales_order_items.unit_cost) — the basis of the
    sale's profit. Records the sale total in
    the касса ledger (core.cash.record_income) same transaction as the
    order itself — a sale and its payment are one atomic event here,
    there's no separate "collect payment later" step in this shop's flow."""
    if not items:
        raise ValueError("нужна хотя бы одна позиция в продаже")
    location_id = _locations.resolve(conn, location_id)

    # How it was paid is settled before anything is written: `payments`
    # is (account_id, amount, rate) per account the client paid into —
    # наличные + ФОП, part in USDT… — and must add up to the total
    # (core.cash.PaymentError otherwise). None = the whole total onto the
    # точка's default account for payment_method, the one-tap path.
    if allow_debt and not client_id:
        raise SaleError("Продажа в долг — только клиенту с номером телефона.")
    items = [tuple(item) + (None,) * (5 - len(item)) for item in items]
    for product_id, qty, _price, batch_id, _cell in items:
        product = get_product(conn, product_id)
        if product and product["is_serial"]:
            if not batch_id:
                raise SaleError(f"«{product['name']}» — серийный товар: выберите IMEI, который продаёте.")
            if qty != 1:
                raise SaleError(f"«{product['name']}»: одно устройство — одна строка (количество 1).")
    # Where each line comes from is settled before anything is written
    # too — a sale that can't be filled must not leave an order behind.
    cells = []
    for product_id, qty, _price, batch_id, named_cell in items:
        if batch_id and named_cell:
            cell_id = named_cell if available_qty(conn, batch_id, named_cell, reserved_for=reserved_for) >= qty else None
        elif batch_id:
            cell_id = _batch_cell(conn, batch_id, product_id, qty, location_id)
        else:
            cell_id = pick_cell_with_stock(conn, product_id, qty, location_id)
        if cell_id is None:
            raise InsufficientStockError(f"Недостаточно товара (product_id={product_id}) ни на одной ячейке этой точки")
        cells.append(cell_id)
    resolved = core_cash.resolve_payments(
        conn, location_id, sum(qty * price for _product_id, qty, price, _batch, _cell in items), payments,
        payment_method, allow_partial=allow_debt,
    )

    order_id = conn.execute(
        """INSERT INTO sales_orders (client_id, channel, status, staff_id, warranty_until, payment_method, location_id)
           VALUES (?, ?, 'completed', ?, ?, ?, ?)""",
        (client_id, channel, staff_id, warranty_until,
         core_cash.payment_method_label(resolved) if resolved else "debt", location_id),
    ).lastrowid

    total = 0
    cost_total, cost_known = 0, True
    for (product_id, qty, price, batch_id, _cell), cell_id in zip(items, cells):
        item_id = conn.execute(
            "INSERT INTO sales_order_items (order_id, product_id, qty, price, batch_id) VALUES (?, ?, ?, ?, ?)",
            (order_id, product_id, qty, price, batch_id),
        ).lastrowid
        # The rows this line is about to write carry each партия's cost
        # (several rows if FIFO spans партии) — everything after this id.
        last_id = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM stock_movements").fetchone()["n"]
        record_movement(
            conn, product_id, qty, "sale", staff_id,
            from_cell_id=cell_id, ref_type="sales_order", ref_id=order_id, batch_id=batch_id,
            reserved_for=reserved_for,
        )
        cost = conn.execute(
            """SELECT SUM(qty * unit_cost) AS total, SUM(CASE WHEN unit_cost IS NULL THEN 1 ELSE 0 END) AS unknown
               FROM stock_movements WHERE id > ? AND ref_type = 'sales_order' AND ref_id = ?""",
            (last_id, order_id),
        ).fetchone()
        if cost["total"] is not None and not cost["unknown"]:
            conn.execute(
                "UPDATE sales_order_items SET unit_cost = ? WHERE id = ?", (round(cost["total"] / qty, 2), item_id)
            )
            cost_total += cost["total"]
        else:
            cost_known = False
        total += qty * price

    core_cash.record_payments(conn, "income", resolved, "sales_order", order_id, staff_id)
    _documents.register(
        conn, "sale", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="sales_orders", ref_id=order_id, amount=total, key=key,
    )
    # «Прибыль по документу»: only when the cost of every line is known.
    if cost_known:
        _documents.set_profit(conn, "sale", order_id, money(total - cost_total))
    # Взаиморасчёты: what the client owed for it and what he paid on the
    # spot — the difference stays on his balance.
    if client_id:
        label = _documents.label("sale", order_id)
        _settlements.post(conn, client_id, total, "sale", ref_type="sales_order", ref_id=order_id,
                          location_id=location_id, staff_id=staff_id, comment=label)
        _settlements.post(conn, client_id, -core_cash.paid_uah(resolved), "payment", ref_type="sales_order",
                          ref_id=order_id, location_id=location_id, staff_id=staff_id, comment=f"Оплата {label}")

    return order_id


def sale_numbers(conn: sqlite3.Connection, order_id: int) -> dict:
    """Сумма / оплачено при продаже / ушло в долг клиента."""
    total = conn.execute(
        "SELECT COALESCE(SUM(qty * price), 0) AS total FROM sales_order_items WHERE order_id = ?", (order_id,)
    ).fetchone()["total"]
    paid = conn.execute(
        """SELECT COALESCE(SUM(amount_uah), 0) AS total FROM cash_transactions
           WHERE ref_type = 'sales_order' AND ref_id = ? AND kind = 'income'""",
        (order_id,),
    ).fetchone()["total"]
    return {"total": money(total), "paid": money(paid), "debt": money(max(total - paid, 0))}


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
            ORDER BY sales_orders.created_at DESC, sales_orders.id DESC
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
           ORDER BY sales_orders.created_at DESC, sales_orders.id DESC""",
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
        """SELECT sales_order_items.*, products.name AS product_name, batches.imei AS imei,
                  suppliers.name AS supplier_name
           FROM sales_order_items
           JOIN products ON products.id = sales_order_items.product_id
           LEFT JOIN batches ON batches.id = sales_order_items.batch_id
           LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
           WHERE order_id = ?""",
        (order_id,),
    ).fetchall()
