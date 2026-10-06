"""Единый журнал документов (06.10) — the common envelope around everything
that happens in the business: one row in `documents` per ремонт, продажа,
приход, покупка, списание, расход из кассы…, whichever точка and whoever
did it. The business tables (repair_orders, sales_orders, …) still hold the
substance; a document row adds what they all share — тип и номер, точка,
автор, время, контрагент, сумма, статус — so the whole business reads as
one chronological feed (list_journal) and every operation has a name staff
can say out loud («РК-44», «ПД-7»).

Rules this module exists to hold (презентация «Ядро и логика учёта»):
  - a проведённый document is never deleted — a mistake is cancelled with a
    reason, by a named person, and stays in the feed (core.doc_cancel does
    the actual reversal of stock/money; this module only keeps the status
    and the trail);
  - every document keeps its author, time, точка and history
    (document_events);
  - a repeated click or message must not create a second document — pass
    `key` to register() and call find_by_key() first.

Deliberately a leaf module: imports nothing from core, so core.storage can
call backfill() from init_db and every business module can register here
without an import cycle.
"""
from __future__ import annotations

import sqlite3

# doc_type -> (префикс номера, название). Prefixes are what staff see
# («РК-101» on a repair card); change one here and every screen follows.
DOC_TYPES: dict[str, tuple[str, str]] = {
    "repair": ("РК", "Ремонт клиента"),
    "sale": ("ПД", "Продажа"),
    "buyback": ("ПК", "Покупка"),
    "receipt": ("ПХ", "Приход"),
    "supplier_return": ("ВП", "Возврат поставщику"),
    "stock_in": ("ОП", "Оприходование"),
    "writeoff": ("СП", "Списание"),
    "transfer": ("ПМ", "Перемещение"),
    "cash_out": ("РКО", "Расход из кассы"),
    "cash_adjust": ("КР", "Корректировка кассы"),
    "exchange": ("ОБ", "Обмен валют"),
    "money_transfer": ("ДП", "Перемещение денег"),
    "shift": ("СМ", "Открытие смены"),
}

# Types whose number IS the id of the row they wrap (repair №44 has always
# been called «№44» on its card and in the group chat — its document is
# РК-44, not a second, different number). The rest count up on their own.
_NUMBER_FROM_REF = {
    "repair", "sale", "buyback", "receipt", "supplier_return", "exchange", "money_transfer", "shift",
}

# Where «открыть документ» leads, by type. {id} is ref_id.
_SOURCE_PATHS = {
    "repair": "/repairs/{id}",
    "sale": "/sales/{id}",
    "buyback": "/buyback/{id}",
    "receipt": "/purchases/{id}",
}


class DocumentError(Exception):
    """A document operation that can't go ahead — message is shown to staff as-is."""


def label(doc_type: str, number: int) -> str:
    """«РК-044» — prefix + number, zero-padded to three so a short list
    lines up; longer numbers just grow."""
    prefix = DOC_TYPES.get(doc_type, ("ДОК", ""))[0]
    return f"{prefix}-{number:03d}"


def doc_label(doc: sqlite3.Row) -> str:
    return label(doc["doc_type"], doc["number"])


def type_name(doc_type: str) -> str:
    return DOC_TYPES.get(doc_type, ("", doc_type))[1]


def source_path(doc: sqlite3.Row) -> str | None:
    if doc["doc_type"] == "transfer" and doc["ref_table"] == "stock_transfers":
        return f"/transfers/{doc['ref_id']}"
    template = _SOURCE_PATHS.get(doc["doc_type"])
    return template.format(id=doc["ref_id"]) if template and doc["ref_id"] else None


def _default_location_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
    return row["id"] if row else None


def _next_number(conn: sqlite3.Connection, doc_type: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 AS n FROM documents WHERE doc_type = ?", (doc_type,)
    ).fetchone()
    return row["n"]


def find_by_key(conn: sqlite3.Connection, key: str | None) -> sqlite3.Row | None:
    """The document a previous attempt with this idempotency key already
    created, if any — check before doing the work, redirect to it instead."""
    if not key:
        return None
    return conn.execute("SELECT * FROM documents WHERE idempotency_key = ?", (key,)).fetchone()


def register(
    conn: sqlite3.Connection,
    doc_type: str,
    *,
    staff_id: int | None,
    location_id: int | None = None,
    client_id: int | None = None,
    ref_table: str | None = None,
    ref_id: int | None = None,
    title: str | None = None,
    amount: int | None = None,
    key: str | None = None,
    created_at: str | None = None,
    status: str = "posted",
) -> int:
    """Write the journal row for something that was just проведено, in the
    same transaction as the thing itself. Returns the document id."""
    if doc_type not in DOC_TYPES:
        raise ValueError(f"неизвестный тип документа: {doc_type}")
    if location_id is None:
        location_id = _default_location_id(conn)
    number = ref_id if doc_type in _NUMBER_FROM_REF and ref_id else _next_number(conn, doc_type)
    doc_id = conn.execute(
        """INSERT INTO documents
           (doc_type, number, location_id, staff_id, client_id, ref_table, ref_id, title, amount,
            status, idempotency_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, datetime('now')))""",
        (doc_type, number, location_id, staff_id, client_id, ref_table, ref_id, title, amount,
         status, key, created_at),
    ).lastrowid
    conn.execute(
        "INSERT INTO document_events (document_id, event, staff_id, created_at) "
        "VALUES (?, 'created', ?, COALESCE(?, datetime('now')))",
        (doc_id, staff_id, created_at),
    )
    return doc_id


def get(conn: sqlite3.Connection, doc_id: int) -> sqlite3.Row | None:
    return conn.execute(_JOURNAL_SELECT + " WHERE documents.id = ?", (doc_id,)).fetchone()


def get_for(conn: sqlite3.Connection, doc_type: str, ref_id: int) -> sqlite3.Row | None:
    """The document wrapping a given business row (repair 44 -> РК-44)."""
    return conn.execute(
        _JOURNAL_SELECT + " WHERE documents.doc_type = ? AND documents.ref_id = ?", (doc_type, ref_id)
    ).fetchone()


def update_for(conn: sqlite3.Connection, doc_type: str, ref_id: int, *, amount: int | None = None,
               title: str | None = None) -> None:
    """Keep the envelope in step when the substance changes after the fact
    (a repair's final price is only known at выдача)."""
    if amount is not None:
        conn.execute(
            "UPDATE documents SET amount = ? WHERE doc_type = ? AND ref_id = ?", (amount, doc_type, ref_id)
        )
    if title is not None:
        conn.execute(
            "UPDATE documents SET title = ? WHERE doc_type = ? AND ref_id = ?", (title, doc_type, ref_id)
        )


def add_event(conn: sqlite3.Connection, doc_id: int, event: str, staff_id: int | None,
              comment: str | None = None) -> None:
    conn.execute(
        "INSERT INTO document_events (document_id, event, staff_id, comment) VALUES (?, ?, ?, ?)",
        (doc_id, event, staff_id, comment),
    )


def get_events(conn: sqlite3.Connection, doc_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT document_events.*, staff.name AS staff_name
           FROM document_events LEFT JOIN staff ON staff.id = document_events.staff_id
           WHERE document_id = ? ORDER BY document_events.id""",
        (doc_id,),
    ).fetchall()


def mark_cancelled(conn: sqlite3.Connection, doc_id: int, staff_id: int, reason: str) -> None:
    """Status + trail only — call core.doc_cancel.cancel_document, which
    reverses the document's effects first and then calls this."""
    conn.execute(
        """UPDATE documents SET status = 'cancelled', cancel_reason = ?, cancelled_by = ?,
                                cancelled_at = datetime('now')
           WHERE id = ?""",
        (reason, staff_id, doc_id),
    )
    add_event(conn, doc_id, "cancelled", staff_id, reason)


_JOURNAL_SELECT = """SELECT documents.*, locations.name AS location_name, staff.name AS staff_name,
                            clients.name AS client_name, clients.phone AS client_phone,
                            canceller.name AS cancelled_by_name
                     FROM documents
                     LEFT JOIN locations ON locations.id = documents.location_id
                     LEFT JOIN staff ON staff.id = documents.staff_id
                     LEFT JOIN clients ON clients.id = documents.client_id
                     LEFT JOIN staff AS canceller ON canceller.id = documents.cancelled_by"""


def list_journal(
    conn: sqlite3.Connection,
    *,
    utc_start: str | None = None,
    utc_end: str | None = None,
    location_id: int | None = None,
    staff_id: int | None = None,
    doc_type: str | None = None,
    client_id: int | None = None,
    limit: int = 300,
) -> list[sqlite3.Row]:
    """The feed, newest first. Every filter is optional and they combine —
    «сегодня · все точки · все сотрудники · все документы» is no filters
    but the date."""
    query = _JOURNAL_SELECT + " WHERE 1=1"
    params: list = []
    if utc_start is not None:
        query += " AND documents.created_at >= ?"
        params.append(utc_start)
    if utc_end is not None:
        query += " AND documents.created_at < ?"
        params.append(utc_end)
    if location_id:
        query += " AND documents.location_id = ?"
        params.append(location_id)
    if staff_id:
        query += " AND documents.staff_id = ?"
        params.append(staff_id)
    if doc_type:
        query += " AND documents.doc_type = ?"
        params.append(doc_type)
    if client_id:
        query += " AND documents.client_id = ?"
        params.append(client_id)
    query += " ORDER BY documents.created_at DESC, documents.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


# ---- one-time backfill of everything проведённое before the journal existed ----

def _has(conn: sqlite3.Connection, doc_type: str, ref_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM documents WHERE doc_type = ? AND ref_id = ?", (doc_type, ref_id)
    ).fetchone() is not None


def backfill(conn: sqlite3.Connection) -> None:
    """Give every already-existing repair/sale/receipt/покупка/return/cash
    operation/manual stock movement its journal row, dated when it actually
    happened. Runs from init_db on every start and only ever adds what is
    missing, so it is also the safety net for a row written by code that
    forgot to register."""
    for row in conn.execute(
        """SELECT repair_orders.*, devices.device_type, devices.brand, devices.model,
                  (SELECT changed_by FROM repair_status_history
                   WHERE order_id = repair_orders.id ORDER BY id LIMIT 1) AS author
           FROM repair_orders JOIN devices ON devices.id = repair_orders.device_id"""
    ).fetchall():
        if _has(conn, "repair", row["id"]):
            continue
        title = " ".join(x for x in (row["device_type"], row["brand"], row["model"]) if x)
        register(
            conn, "repair", staff_id=row["author"], location_id=row["location_id"], client_id=row["client_id"],
            ref_table="repair_orders", ref_id=row["id"], title=title,
            amount=row["price_final"] or row["price_estimate"], created_at=row["created_at"],
        )

    for row in conn.execute(
        """SELECT sales_orders.*,
                  (SELECT COALESCE(SUM(qty * price), 0) FROM sales_order_items
                   WHERE order_id = sales_orders.id) AS total
           FROM sales_orders"""
    ).fetchall():
        if _has(conn, "sale", row["id"]):
            continue
        register(
            conn, "sale", staff_id=row["staff_id"], location_id=row["location_id"], client_id=row["client_id"],
            ref_table="sales_orders", ref_id=row["id"], amount=row["total"], created_at=row["created_at"],
            status="cancelled" if row["status"] == "cancelled" else "posted",
        )

    for row in conn.execute(
        """SELECT goods_receipts.*, suppliers.name AS supplier_name, suppliers.client_id AS supplier_client_id,
                  (SELECT COALESCE(SUM(qty * COALESCE(unit_cost, 0)), 0) FROM goods_receipt_items
                   WHERE receipt_id = goods_receipts.id) AS total
           FROM goods_receipts LEFT JOIN suppliers ON suppliers.id = goods_receipts.supplier_id"""
    ).fetchall():
        if _has(conn, "receipt", row["id"]):
            continue
        register(
            conn, "receipt", staff_id=row["staff_id"], location_id=row["location_id"],
            client_id=row["supplier_client_id"], ref_table="goods_receipts", ref_id=row["id"],
            title=row["supplier_name"], amount=row["total"] or None, created_at=row["created_at"],
        )

    for row in conn.execute("SELECT * FROM buyback_orders").fetchall():
        if _has(conn, "buyback", row["id"]):
            continue
        title = " ".join(x for x in (row["device_type"], row["brand"], row["model"]) if x)
        register(
            conn, "buyback", staff_id=row["staff_id"], location_id=row["location_id"], client_id=row["client_id"],
            ref_table="buyback_orders", ref_id=row["id"], title=title, amount=row["purchase_price"],
            created_at=row["created_at"],
        )

    for row in conn.execute(
        """SELECT supplier_returns.*, products.name AS product_name, suppliers.client_id AS supplier_client_id,
                  warehouses.location_id AS location_id
           FROM supplier_returns
           JOIN products ON products.id = supplier_returns.product_id
           LEFT JOIN suppliers ON suppliers.id = supplier_returns.supplier_id
           LEFT JOIN storage_cells ON storage_cells.id = supplier_returns.cell_id
           LEFT JOIN warehouses ON warehouses.id = storage_cells.warehouse_id"""
    ).fetchall():
        if _has(conn, "supplier_return", row["id"]):
            continue
        register(
            conn, "supplier_return", staff_id=row["staff_id"], location_id=row["location_id"],
            client_id=row["supplier_client_id"], ref_table="supplier_returns", ref_id=row["id"],
            title=f"{row['product_name']} × {row['qty']}", created_at=row["created_at"],
        )

    # Manual cash operations: an expense typed into Касса, or «внести/изъять».
    # Income from a sale/repair and the payout of a покупка are part of THAT
    # document (they carry its ref_type) and get no row of their own.
    for row in conn.execute(
        "SELECT * FROM cash_transactions WHERE ref_type IS NULL ORDER BY id"
    ).fetchall():
        doc_type = "cash_adjust" if row["category"] == "adjustment" else "cash_out"
        if doc_type == "cash_out" and row["kind"] != "expense":
            continue
        if _has(conn, doc_type, row["id"]):
            continue
        amount = row["amount"] if row["kind"] == "income" else -row["amount"]
        register(
            conn, doc_type, staff_id=row["staff_id"], location_id=row["location_id"],
            ref_table="cash_transactions", ref_id=row["id"], title=row["comment"], amount=amount,
            created_at=row["created_at"],
        )

    # Manual stock movements (Склад → Движения, «Добавить остаток» on a
    # product card). Movements made BY another document carry its ref_type.
    manual_types = {"receipt": "stock_in", "adjustment": "writeoff", "transfer": "transfer"}
    for row in conn.execute(
        """SELECT stock_movements.*, products.name AS product_name,
                  COALESCE(fw.location_id, tw.location_id) AS location_id
           FROM stock_movements
           JOIN products ON products.id = stock_movements.product_id
           LEFT JOIN storage_cells fc ON fc.id = stock_movements.from_cell_id
           LEFT JOIN warehouses fw ON fw.id = fc.warehouse_id
           LEFT JOIN storage_cells tc ON tc.id = stock_movements.to_cell_id
           LEFT JOIN warehouses tw ON tw.id = tc.warehouse_id
           WHERE stock_movements.ref_type IS NULL ORDER BY stock_movements.id"""
    ).fetchall():
        doc_type = manual_types.get(row["reason"])
        if not doc_type or _has(conn, doc_type, row["id"]):
            continue
        register(
            conn, doc_type, staff_id=row["staff_id"], location_id=row["location_id"],
            ref_table="stock_movements", ref_id=row["id"], title=f"{row['product_name']} × {row['qty']}",
            created_at=row["created_at"],
        )
