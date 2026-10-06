"""Отмена проведённого документа (06.10). A document is never deleted and
never edited into something else: a mistake is cancelled — its effects on
stock and money are reversed by NEW ledger entries / flags, the document
stays in the journal marked отменён with who, when and why
(core.documents.mark_cancelled).

Separate from core.documents because reversing needs the business modules
(inventory, cash), and those register their documents through
core.documents — keeping the reversers there would be an import cycle.

What can be cancelled so far: a продажа (stock goes back to the cells it
left, its касса income stops counting, the order is marked cancelled) and
the two manual cash documents (расход из кассы, корректировка). Приход,
покупка and the stock documents get theirs together with партии and
денежные счета — reversing them properly needs both.
"""
from __future__ import annotations

import sqlite3

from core import cash as _cash
from core import documents as _documents
from core import inventory as _inventory
from core.documents import DocumentError

CANCELLABLE_TYPES = ("sale", "cash_out", "cash_adjust")


def _cancel_sale(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    order_id = doc["ref_id"]
    movements = conn.execute(
        "SELECT * FROM stock_movements WHERE ref_type = 'sales_order' AND ref_id = ? AND reason = 'sale'",
        (order_id,),
    ).fetchall()
    for movement in movements:
        _inventory.record_movement(
            conn, movement["product_id"], movement["qty"], "adjustment", staff_id,
            to_cell_id=movement["from_cell_id"], ref_type="sale_cancel", ref_id=order_id,
            comment=f"Отмена {_documents.doc_label(doc)}",
        )
    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = 'sales_order' AND ref_id = ?", (order_id,)
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    conn.execute("UPDATE sales_orders SET status = 'cancelled' WHERE id = ?", (order_id,))


def _cancel_cash(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    _cash.cancel_transaction(conn, doc["ref_id"])


_REVERSERS = {"sale": _cancel_sale, "cash_out": _cancel_cash, "cash_adjust": _cancel_cash}


def cancel_document(conn: sqlite3.Connection, doc_id: int, staff_id: int, reason: str) -> sqlite3.Row:
    """Reverse the document's effects and mark it отменён. Raises
    DocumentError (staff-readable) when it can't. Returns the document as
    it was before cancelling (callers need its type/ref to sync whatever
    hangs off it, e.g. a sales-channel card)."""
    reason = (reason or "").strip()
    doc = _documents.get(conn, doc_id)
    if not doc:
        raise DocumentError("Документ не найден.")
    if doc["status"] == "cancelled":
        raise DocumentError("Документ уже отменён.")
    if doc["doc_type"] not in _REVERSERS:
        raise DocumentError("Этот тип документа пока нельзя отменить из журнала.")
    if not reason:
        raise DocumentError("Укажите причину отмены.")
    _REVERSERS[doc["doc_type"]](conn, doc, staff_id)
    _documents.mark_cancelled(conn, doc_id, staff_id, reason)
    return doc
