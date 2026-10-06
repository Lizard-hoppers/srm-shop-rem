"""Заказы клиентов (Заход 6): goods we have are put aside for a client.

  reserved   the lines are held («резерв») until reserved_until — 24
             hours by default. A held line can't be sold, moved or put
             into a repair by anyone else.
  expired    the hold ran out and nobody extended it: the goods are free
             again («автоснятие»). Nothing is lost — the заказ can be
             taken up again if the goods are still there.
  issued     «Выдать»: the заказ became a sale (ПД) of exactly the lines
             it held.
  cancelled

«Оплатить» and «Выдать» are separate steps: a payment goes onto the
client's balance as an аванс towards this заказ (core.settlements); the
handover charges him the total. Whatever is unpaid at handover is his
debt; whatever he paid and never collected stays his аванс.
"""
from __future__ import annotations

import sqlite3

from core import cash as _cash
from core import documents as _documents
from core import inventory as _inventory
from core import locations as _locations
from core import sales as _sales
from core import settlements as _settlements
from core.accounts import money

STATUS_LABELS = {
    "reserved": "В резерве",
    "expired": "Резерв истёк",
    "issued": "Выдан",
    "cancelled": "Отменён",
}
RESERVE_HOURS = 24
_REF = "client_order"


class OrderError(Exception):
    """An order step that can't go ahead — message is shown to staff as-is."""


def _ref(order_id: int) -> tuple[str, int]:
    return (_REF, order_id)


_SELECT = """SELECT client_orders.*, clients.name AS client_name, clients.phone AS client_phone,
                    staff.name AS staff_name, locations.name AS location_name,
                    (SELECT COALESCE(SUM(qty * price), 0) FROM client_order_items
                     WHERE order_id = client_orders.id) AS total
             FROM client_orders
             JOIN clients ON clients.id = client_orders.client_id
             LEFT JOIN staff ON staff.id = client_orders.staff_id
             LEFT JOIN locations ON locations.id = client_orders.location_id"""


def get_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE client_orders.id = ?", (order_id,)).fetchone()


def list_orders(
    conn: sqlite3.Connection, *, statuses: tuple[str, ...] | None = None, location_id: int | None = None,
    client_id: int | None = None, limit: int = 200,
) -> list[sqlite3.Row]:
    query, params = _SELECT + " WHERE 1=1", []
    if statuses:
        query += f" AND client_orders.status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    if location_id is not None:
        query += " AND client_orders.location_id = ?"
        params.append(location_id)
    if client_id is not None:
        query += " AND client_orders.client_id = ?"
        params.append(client_id)
    return conn.execute(query + " ORDER BY client_orders.id DESC LIMIT ?", [*params, limit]).fetchall()


def get_items(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT client_order_items.*, products.name AS product_name, products.unit, batches.imei AS imei,
                  storage_cells.code AS cell_code
           FROM client_order_items
           JOIN products ON products.id = client_order_items.product_id
           JOIN batches ON batches.id = client_order_items.batch_id
           JOIN storage_cells ON storage_cells.id = client_order_items.cell_id
           WHERE client_order_items.order_id = ? ORDER BY client_order_items.id""",
        (order_id,),
    ).fetchall()


def numbers(conn: sqlite3.Connection, order_id: int) -> dict:
    """Сумма / оплачено / осталось."""
    order = get_order(conn, order_id)
    paid = _settlements.paid_for(conn, _REF, order_id)
    return {"total": money(order["total"]), "paid": paid, "left": money(max(order["total"] - paid, 0))}


def _until(conn: sqlite3.Connection, hours: int) -> str:
    return conn.execute("SELECT datetime('now', ?) AS t", (f"+{int(hours)} hours",)).fetchone()["t"]


def _free_lines(conn: sqlite3.Connection, product_id: int, location_id: int, batch_id: int | None) -> list[dict]:
    """The точка's own free stock of a product (not a master's, not «в
    пути»), oldest партия first — optionally just one партия."""
    lines = []
    for line in _inventory.stock_lines(conn, location_id=location_id, product_id=product_id):
        if line["warehouse_kind"] != "point" or (batch_id and line["batch_id"] != batch_id):
            continue
        free = line["qty"] - line["reserved"]
        if free > 0:
            lines.append({"batch_id": line["batch_id"], "cell_id": line["cell_id"], "free": free})
    return sorted(lines, key=lambda l: l["batch_id"])


def create_order(
    conn: sqlite3.Connection, client_id: int, items: list[tuple], staff_id: int, *,
    location_id: int | None = None, comment: str | None = None, key: str | None = None,
    hours: int = RESERVE_HOURS,
) -> int:
    """items: (product_id, qty, price) or (product_id, qty, price,
    batch_id) — a serial product must name its unit. Every line is held
    from this moment; if any of it isn't free, nothing is created."""
    if not items:
        raise OrderError("Добавьте в заказ хотя бы один товар.")
    location_id = _locations.resolve(conn, location_id)
    plan = []  # (product_id, batch_id, cell_id, qty, price)
    for item in items:
        product_id, qty, price, batch_id = (tuple(item) + (None,))[:4]
        product = _inventory.get_product(conn, product_id)
        if not product:
            raise OrderError("Товар не найден.")
        if qty <= 0:
            raise OrderError(f"«{product['name']}»: количество должно быть больше нуля.")
        if product["is_serial"] and (not batch_id or qty != 1):
            raise OrderError(f"«{product['name']}» — серийный товар: выберите IMEI, одна единица — одна строка.")
        left = qty
        for line in _free_lines(conn, product_id, location_id, batch_id):
            take = min(left, line["free"])
            plan.append((product_id, line["batch_id"], line["cell_id"], take, price))
            left -= take
            if not left:
                break
        if left:
            raise OrderError(f"«{product['name']}»: свободно только {qty - left}, в заказ нужно {qty}.")

    until = _until(conn, hours)
    order_id = conn.execute(
        "INSERT INTO client_orders (client_id, location_id, reserved_until, comment, staff_id) VALUES (?, ?, ?, ?, ?)",
        (client_id, location_id, until, (comment or "").strip() or None, staff_id),
    ).lastrowid
    for product_id, batch_id, cell_id, qty, price in plan:
        conn.execute(
            "INSERT INTO client_order_items (order_id, product_id, batch_id, cell_id, qty, price) VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, product_id, batch_id, cell_id, qty, price),
        )
        _inventory.reserve(conn, batch_id, cell_id, qty, _REF, order_id, staff_id, expires_at=until)
    _documents.register(
        conn, "client_order", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="client_orders", ref_id=order_id, amount=money(sum(q * p for _a, _b, _c, q, p in plan)), key=key,
    )
    return order_id


def _event(conn: sqlite3.Connection, order_id: int, event: str, staff_id: int | None, note: str | None = None) -> None:
    doc = _documents.get_for(conn, "client_order", order_id)
    if doc:
        _documents.add_event(conn, doc["id"], event, staff_id, note)


def expire_due(conn: sqlite3.Connection) -> list[int]:
    """«Автоснятие резерва»: every заказ whose hold has run out becomes
    expired. (The hold itself stops counting the moment it expires —
    this only brings the заказ's status in line.) Cheap; called wherever
    заказы are shown or acted on."""
    rows = conn.execute(
        "SELECT id FROM client_orders WHERE status = 'reserved' AND reserved_until <= datetime('now')"
    ).fetchall()
    for row in rows:
        _inventory.release(conn, _REF, row["id"])
        conn.execute("UPDATE client_orders SET status = 'expired' WHERE id = ?", (row["id"],))
        _event(conn, row["id"], "reserve_expired", None)
    return [row["id"] for row in rows]


def _require(conn: sqlite3.Connection, order_id: int, *statuses: str) -> sqlite3.Row:
    expire_due(conn)
    order = get_order(conn, order_id)
    if not order:
        raise OrderError("Заказ не найден.")
    if order["status"] not in statuses:
        raise OrderError(f"Заказ в статусе «{STATUS_LABELS[order['status']]}» — это действие сейчас недоступно.")
    return order


def extend(conn: sqlite3.Connection, order_id: int, staff_id: int, hours: int = RESERVE_HOURS) -> None:
    """«Продлить резерв» — another 24 hours from now. Also brings an
    expired заказ back, if every line of it is still free."""
    _require(conn, order_id, "reserved", "expired")
    items = get_items(conn, order_id)
    for item in items:
        if _inventory.available_qty(conn, item["batch_id"], item["cell_id"], reserved_for=_ref(order_id)) < item["qty"]:
            raise OrderError(f"«{item['product_name']}» уже нет в свободном остатке — заказ не возобновить.")
    until = _until(conn, hours)
    _inventory.release(conn, _REF, order_id)
    for item in items:
        _inventory.reserve(conn, item["batch_id"], item["cell_id"], item["qty"], _REF, order_id, staff_id, expires_at=until)
    conn.execute("UPDATE client_orders SET status = 'reserved', reserved_until = ? WHERE id = ?", (until, order_id))
    _event(conn, order_id, "reserve_extended", staff_id)


def pay(
    conn: sqlite3.Connection, order_id: int, payments: list[tuple[int, object, object]], staff_id: int, *,
    key: str | None = None,
) -> int:
    """«Оплатить»: any part of what is left, onto any of the точка's
    accounts. Returns the ledger row id of the payment."""
    order = _require(conn, order_id, "reserved")
    resolved = _cash.resolve_parts(conn, order["location_id"], payments or [])
    amount = _cash.paid_uah(resolved)
    left = numbers(conn, order_id)["left"]
    if not resolved or amount <= 0:
        raise OrderError("Укажите сумму оплаты.")
    if amount > left + _cash.PAYMENT_TOLERANCE_UAH:
        raise OrderError(f"К оплате осталось {left} грн — внесено больше ({amount} грн).")
    entry_id = _settlements.receive_money(
        conn, order["client_id"], payments, staff_id=staff_id, location_id=order["location_id"],
        comment=f"оплата {_documents.label('client_order', order_id)}", key=key, ref=_ref(order_id),
    )
    _event(conn, order_id, "paid", staff_id, f"{amount} грн")
    return entry_id


def issue(
    conn: sqlite3.Connection, order_id: int, staff_id: int, *,
    payments: list[tuple[int, object, object]] | None = None, key: str | None = None,
) -> int:
    """«Выдать»: the заказ becomes a sale of exactly the lines it held.
    `payments` — what the client pays right now, at most what is left;
    anything still unpaid after that is his debt. Returns the sale id."""
    order = _require(conn, order_id, "reserved")
    left = numbers(conn, order_id)["left"]
    paying = _cash.paid_uah(_cash.resolve_parts(conn, order["location_id"], payments or []))
    if paying > left + _cash.PAYMENT_TOLERANCE_UAH:
        raise OrderError(f"К оплате осталось {left} грн — внесено больше ({paying} грн).")
    items = [(i["product_id"], i["qty"], i["price"], i["batch_id"], i["cell_id"]) for i in get_items(conn, order_id)]
    # The sale charges the client the whole total; what he prepaid is
    # already on his balance, so only today's money is «paid at the sale».
    sale_id = _sales.create_sale(
        conn, order["client_id"], "offline", staff_id, items, location_id=order["location_id"], key=key,
        payments=payments or [], allow_debt=True, reserved_for=_ref(order_id),
    )
    _inventory.release(conn, _REF, order_id)
    conn.execute("UPDATE client_orders SET status = 'issued', sale_id = ? WHERE id = ?", (sale_id, order_id))
    _event(conn, order_id, "issued", staff_id, _documents.label("sale", sale_id))
    return sale_id


def cancel(conn: sqlite3.Connection, order_id: int, staff_id: int, *, mark_document: bool = True) -> None:
    """Let the goods go. Money already paid towards it stays on the
    client's balance as an аванс — return it with «Выдать деньги»."""
    _require(conn, order_id, "reserved", "expired")
    _inventory.release(conn, _REF, order_id)
    conn.execute("UPDATE client_orders SET status = 'cancelled' WHERE id = ?", (order_id,))
    if mark_document:
        doc = _documents.get_for(conn, "client_order", order_id)
        if doc:
            _documents.mark_cancelled(conn, doc["id"], staff_id, "заказ отменён")
