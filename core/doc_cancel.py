"""Отмена проведённого документа (06.10). A document is never deleted and
never edited into something else: a mistake is cancelled — its effects on
stock and money are reversed by NEW ledger entries / flags, the document
stays in the journal marked отменён with who, when and why
(core.documents.mark_cancelled).

Separate from core.documents because reversing needs the business modules
(inventory, cash), and those register their documents through
core.documents — keeping the reversers there would be an import cycle.

What can be cancelled so far: a продажа (stock goes back to the cells it
left, its income stops counting on every account it was paid into, the
order is marked cancelled), the two manual cash documents (расход из кассы,
корректировка), обмен валют and перемещение денег; and since партии
(Заход 3) — приход, the manual stock documents (оприходование, списание,
перемещение между ячейками) and a перемещение между складами that is still
«в пути». A stock document can only be undone while everything it brought
in is still where it put it. Покупка gets its cancel with its rebuild
(Заход 4).
"""
from __future__ import annotations

import sqlite3

from core import cash as _cash
from core import documents as _documents
from core import inventory as _inventory
from core import stock_transfers as _stock_transfers
from core.documents import DocumentError

CANCELLABLE_TYPES = (
    "sale", "cash_out", "cash_adjust", "exchange", "money_transfer",
    "receipt", "stock_in", "writeoff", "transfer",
)


def _cancel_sale(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    order_id = doc["ref_id"]
    movements = conn.execute(
        "SELECT * FROM stock_movements WHERE ref_type = 'sales_order' AND ref_id = ? AND reason = 'sale'",
        (order_id,),
    ).fetchall()
    for movement in movements:
        # Back into the very партия it left — a returned phone is the same
        # IMEI at the same cost, not a new anonymous unit.
        _inventory.record_movement(
            conn, movement["product_id"], movement["qty"], "adjustment", staff_id,
            to_cell_id=movement["from_cell_id"], ref_type="sale_cancel", ref_id=order_id,
            comment=f"Отмена {_documents.doc_label(doc)}", batch_id=movement["batch_id"],
        )
    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = 'sales_order' AND ref_id = ?", (order_id,)
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    conn.execute("UPDATE sales_orders SET status = 'cancelled' WHERE id = ?", (order_id,))


def _cancel_cash(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    _cash.cancel_transaction(conn, doc["ref_id"])


def _cancel_money_rows(conn: sqlite3.Connection, ref_type: str, ref_id: int) -> None:
    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = ? AND ref_id = ?", (ref_type, ref_id)
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])


def _cancel_exchange(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    """Both sides stop counting: the money is back where it came from."""
    _cancel_money_rows(conn, "exchange", doc["ref_id"])


def _cancel_transfer(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    """Whether it was still «в пути» or already received — every row it
    posted stops counting, and it can no longer be received."""
    _cancel_money_rows(conn, "money_transfer", doc["ref_id"])
    conn.execute("UPDATE money_transfers SET status = 'cancelled' WHERE id = ?", (doc["ref_id"],))


def _reverse_movements(conn: sqlite3.Connection, movements, staff_id: int, ref_type: str, ref_id: int, comment: str) -> None:
    """Undo stock movements exactly: each one's quantity goes back the way
    it came, in ITS партия. Checks that every bit of it is still where the
    movement left it BEFORE writing anything — goods that have since been
    sold, used or moved on can't be un-received, and the cancel is refused
    whole rather than applied in part."""
    for m in movements:
        if m["to_cell_id"] is None:
            continue
        held = conn.execute(
            "SELECT qty FROM batch_stock WHERE batch_id = ? AND cell_id = ?", (m["batch_id"], m["to_cell_id"])
        ).fetchone()
        if not held or held["qty"] < m["qty"]:
            raise DocumentError(
                "Товар из этого документа уже частично продан, списан или перемещён — отменить его целиком нельзя."
            )
    for m in movements:
        _inventory.record_movement(
            conn, m["product_id"], m["qty"], "adjustment" if (m["from_cell_id"] is None or m["to_cell_id"] is None) else "transfer",
            staff_id, from_cell_id=m["to_cell_id"], to_cell_id=m["from_cell_id"],
            ref_type=ref_type, ref_id=ref_id, batch_id=m["batch_id"], comment=comment,
        )


def _cancel_receipt(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    """Un-receive a приход: every unit it brought in leaves its партия
    again — only possible while all of it is still in the cells it arrived in."""
    movements = conn.execute(
        "SELECT * FROM stock_movements WHERE ref_type = 'goods_receipt' AND ref_id = ? AND reason = 'receipt'",
        (doc["ref_id"],),
    ).fetchall()
    _reverse_movements(conn, movements, staff_id, "receipt_cancel", doc["ref_id"], f"Отмена {_documents.doc_label(doc)}")


def _cancel_manual_stock(conn: sqlite3.Connection, doc: sqlite3.Row, staff_id: int) -> None:
    """ОП / СП / the old cell-to-cell ПМ: one movement (or one per партия
    it spanned — they share the document's first movement id onward with
    no ref_type), reversed."""
    if doc["ref_table"] == "stock_transfers":
        try:
            _stock_transfers.cancel_sent(conn, doc["ref_id"], staff_id)
        except _stock_transfers.TransferError as exc:
            raise DocumentError(str(exc)) from exc
        return
    first = conn.execute("SELECT * FROM stock_movements WHERE id = ?", (doc["ref_id"],)).fetchone()
    if not first:
        raise DocumentError("Движение этого документа не найдено.")
    # Rows of the same operation: written back to back, same author,
    # reason, cells and timestamp — one per партия the quantity came from.
    movements = conn.execute(
        """SELECT * FROM stock_movements
           WHERE id >= ? AND ref_type IS NULL AND reason = ? AND staff_id IS ? AND product_id = ?
             AND from_cell_id IS ? AND to_cell_id IS ? AND created_at = ?
             AND id < COALESCE((SELECT MIN(d.ref_id) FROM documents d
                                WHERE d.ref_table = 'stock_movements' AND d.ref_id > ?), 9223372036854775807)
           ORDER BY id""",
        (first["id"], first["reason"], first["staff_id"], first["product_id"],
         first["from_cell_id"], first["to_cell_id"], first["created_at"], first["id"]),
    ).fetchall()
    _reverse_movements(conn, movements, staff_id, "stock_doc_cancel", doc["ref_id"], f"Отмена {_documents.doc_label(doc)}")


_REVERSERS = {
    "receipt": _cancel_receipt, "stock_in": _cancel_manual_stock, "writeoff": _cancel_manual_stock,
    "transfer": _cancel_manual_stock,
    "sale": _cancel_sale, "cash_out": _cancel_cash, "cash_adjust": _cancel_cash,
    "exchange": _cancel_exchange, "money_transfer": _cancel_transfer,
}


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
