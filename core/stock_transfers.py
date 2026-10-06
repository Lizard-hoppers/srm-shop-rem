"""Перемещение товара между складами (Заход 3, 06.10) — «Отправил → В пути
→ Принял».

Sending takes each line out of its cell and puts it into the «В пути»
склад in the same step: from that moment it is not at the source, not
sellable anywhere, and not yet anyone's at the destination. It becomes the
destination's only when its keeper confirms — a master for his own склад
(«мастер принял товар, он за него отвечает»), stock staff for a точка's —
saying how much of each line actually arrived. What didn't arrive stays
«в пути» with the receiver's note on the transfer; an owner then settles
it (write_off_shortage) — nothing is ever silently absorbed.

Every line names its партия (a serial unit — its IMEI), so origin and cost
travel with the goods. One document per transfer (ПМ) in the journal.
"""
from __future__ import annotations

import sqlite3

from core import documents as _documents
from core import inventory as _inventory
from core import warehouses as _warehouses


class TransferError(Exception):
    """A transfer step that can't go ahead — message is shown to staff as-is."""


_SELECT = """SELECT stock_transfers.*,
                    fw.kind AS from_kind, fw.location_id AS from_location_id, fw.staff_id AS from_staff_id,
                    fw.name AS from_wh_name, fl.name AS from_location_name, fs.name AS from_staff_name,
                    tw.kind AS to_kind, tw.location_id AS to_location_id, tw.staff_id AS to_staff_id,
                    tw.name AS to_wh_name, tl.name AS to_location_name, ts.name AS to_staff_name,
                    sender.name AS sent_by_name, receiver.name AS received_by_name
             FROM stock_transfers
             JOIN warehouses fw ON fw.id = stock_transfers.from_warehouse_id
             LEFT JOIN locations fl ON fl.id = fw.location_id
             LEFT JOIN staff fs ON fs.id = fw.staff_id
             JOIN warehouses tw ON tw.id = stock_transfers.to_warehouse_id
             LEFT JOIN locations tl ON tl.id = tw.location_id
             LEFT JOIN staff ts ON ts.id = tw.staff_id
             LEFT JOIN staff sender ON sender.id = stock_transfers.sent_by
             LEFT JOIN staff receiver ON receiver.id = stock_transfers.received_by"""


def _side_name(transfer: sqlite3.Row, side: str) -> str:
    if transfer[f"{side}_kind"] == "point":
        return transfer[f"{side}_location_name"] or transfer[f"{side}_wh_name"]
    if transfer[f"{side}_kind"] == "master":
        return f"Мастер: {transfer[f'{side}_staff_name'] or transfer[f'{side}_wh_name']}"
    return "В пути"


def route(transfer: sqlite3.Row) -> str:
    """«007 → Мастер: Сергей»."""
    return f"{_side_name(transfer, 'from')} → {_side_name(transfer, 'to')}"


def get_transfer(conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE stock_transfers.id = ?", (transfer_id,)).fetchone()


def get_items(conn: sqlite3.Connection, transfer_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT stock_transfer_items.*, products.name AS product_name, products.unit, products.is_serial,
                  batches.imei, batches.unit_cost_uah, suppliers.name AS supplier_name
           FROM stock_transfer_items
           JOIN products ON products.id = stock_transfer_items.product_id
           JOIN batches ON batches.id = stock_transfer_items.batch_id
           LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
           WHERE stock_transfer_items.transfer_id = ? ORDER BY stock_transfer_items.id""",
        (transfer_id,),
    ).fetchall()


def send(
    conn: sqlite3.Connection, from_warehouse_id: int, to_warehouse_id: int,
    lines: list[tuple[int, int, int]], staff_id: int, comment: str | None = None, key: str | None = None,
    reserved_for: tuple[str, int] | None = None,
) -> int:
    """`lines` is [(batch_id, from_cell_id, qty), …] — stock_lines() rows
    of the source склад. Refuses the whole transfer (nothing written) if
    any line isn't in the source склад or asks for more than that партия
    holds in that cell."""
    source = _warehouses.get_warehouse(conn, from_warehouse_id)
    target = _warehouses.get_warehouse(conn, to_warehouse_id)
    if not source or source["kind"] == "transit":
        raise TransferError("Выберите склад, с которого отправляете.")
    if not target or target["kind"] == "transit":
        raise TransferError("Выберите склад, куда отправляете.")
    if source["id"] == target["id"]:
        raise TransferError("Склад-получатель должен быть другим.")
    if not lines:
        raise TransferError("Добавьте хотя бы одну позицию.")

    # Validate everything before the first write (same batch+cell may be
    # listed twice — sum it).
    wanted: dict[tuple[int, int], int] = {}
    for batch_id, cell_id, qty in lines:
        if not qty or qty <= 0:
            raise TransferError("Количество должно быть больше нуля.")
        wanted[(batch_id, cell_id)] = wanted.get((batch_id, cell_id), 0) + qty
    available = {
        (line["batch_id"], line["cell_id"]): line
        for line in _inventory.stock_lines(conn, warehouse_id=source["id"])
    }
    for (batch_id, cell_id), qty in wanted.items():
        line = available.get((batch_id, cell_id))
        if not line:
            raise TransferError("Одной из позиций уже нет на этом складе — обновите список.")
        if qty > line["qty"]:
            raise TransferError(f"«{line['product_name']}»: на складе {line['qty']}, нельзя отправить {qty}.")
        free = _inventory.available_qty(conn, batch_id, cell_id, reserved_for)
        if qty > free:
            raise TransferError(
                f"«{line['product_name']}»: свободно {max(free, 0)}, остальное в резерве под другой документ."
            )

    transfer_id = conn.execute(
        "INSERT INTO stock_transfers (from_warehouse_id, to_warehouse_id, comment, sent_by) VALUES (?, ?, ?, ?)",
        (source["id"], target["id"], comment, staff_id),
    ).lastrowid
    transit_cell = _warehouses.transit_cell_id(conn)
    for (batch_id, cell_id), qty in wanted.items():
        line = available[(batch_id, cell_id)]
        conn.execute(
            "INSERT INTO stock_transfer_items (transfer_id, product_id, batch_id, from_cell_id, qty) VALUES (?, ?, ?, ?, ?)",
            (transfer_id, line["product_id"], batch_id, cell_id, qty),
        )
        _inventory.record_movement(
            conn, line["product_id"], qty, "transfer", staff_id, from_cell_id=cell_id, to_cell_id=transit_cell,
            ref_type="stock_transfer", ref_id=transfer_id, batch_id=batch_id, comment="Отправлено",
            reserved_for=reserved_for,
        )
    transfer = get_transfer(conn, transfer_id)
    total = sum(wanted.values())
    _documents.register(
        conn, "transfer", staff_id=staff_id, location_id=source["location_id"] or target["location_id"],
        ref_table="stock_transfers", ref_id=transfer_id, key=key,
        title=f"{route(transfer)} · {len(wanted)} поз., {total} шт",
    )
    return transfer_id


def default_target_cell(conn: sqlite3.Connection, warehouse_id: int, product_id: int) -> int | None:
    """Where a received line goes when the receiver doesn't say: the cell
    of this склад already holding the most of that product, else its first
    cell. None only for a точка's склад with no cells created yet."""
    row = conn.execute(
        """SELECT stock.cell_id FROM stock
           JOIN storage_cells ON storage_cells.id = stock.cell_id
           WHERE storage_cells.warehouse_id = ? AND stock.product_id = ? AND stock.qty > 0
           ORDER BY stock.qty DESC LIMIT 1""",
        (warehouse_id, product_id),
    ).fetchone()
    if row:
        return row["cell_id"]
    first = conn.execute(
        "SELECT id FROM storage_cells WHERE warehouse_id = ? ORDER BY id LIMIT 1", (warehouse_id,)
    ).fetchone()
    return first["id"] if first else None


def receive(
    conn: sqlite3.Connection, transfer_id: int, staff_id: int,
    received: dict[int, int] | None = None, cell_id: int | None = None, note: str | None = None,
) -> None:
    """«Принял». `received` maps item id -> quantity that actually arrived
    (missing = all of it); `cell_id` is the destination cell for every
    line (missing = default_target_cell per product). Any line short of
    what was sent needs a note; the missing units stay «в пути»."""
    transfer = get_transfer(conn, transfer_id)
    if not transfer:
        raise TransferError("Перемещение не найдено.")
    if transfer["status"] != "sent":
        raise TransferError("Это перемещение уже принято или отменено.")
    items = get_items(conn, transfer_id)
    received = received or {}
    note = (note or "").strip() or None

    plan = []
    short = False
    for item in items:
        qty = received.get(item["id"], item["qty"])
        if qty < 0 or qty > item["qty"]:
            raise TransferError(f"«{item['product_name']}»: отправлено {item['qty']}, нельзя принять {qty}.")
        if qty < item["qty"]:
            short = True
        target_cell = cell_id or default_target_cell(conn, transfer["to_warehouse_id"], item["product_id"])
        if not target_cell:
            raise TransferError("На складе-получателе нет ни одной ячейки — сначала создайте ячейку.")
        plan.append((item, qty, target_cell))
    if cell_id:
        owner = conn.execute("SELECT warehouse_id FROM storage_cells WHERE id = ?", (cell_id,)).fetchone()
        if not owner or owner["warehouse_id"] != transfer["to_warehouse_id"]:
            raise TransferError("Выбранная ячейка не принадлежит складу-получателю.")
    if short and not note:
        raise TransferError("Принято меньше, чем отправлено — напишите, чего не хватает.")

    transit_cell = _warehouses.transit_cell_id(conn)
    for item, qty, target_cell in plan:
        if qty:
            _inventory.record_movement(
                conn, item["product_id"], qty, "transfer", staff_id, from_cell_id=transit_cell,
                to_cell_id=target_cell, ref_type="stock_transfer", ref_id=transfer_id, batch_id=item["batch_id"],
                comment="Принято",
            )
        conn.execute(
            "UPDATE stock_transfer_items SET received_qty = ?, to_cell_id = ? WHERE id = ?",
            (qty, target_cell if qty else None, item["id"]),
        )
    conn.execute(
        """UPDATE stock_transfers SET status = 'received', received_by = ?, received_at = datetime('now'),
                                      discrepancy_note = ? WHERE id = ?""",
        (staff_id, note if short else None, transfer_id),
    )
    doc = _document(conn, transfer_id)
    if doc:
        _documents.add_event(conn, doc["id"], "discrepancy" if short else "received", staff_id,
                             f"недостача: {note}" if short else None)


def document(conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
    return _document(conn, transfer_id)


def _document(conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
    """The transfer's ПМ document. 'transfer' is also the type of the old
    cell-to-cell documents (ref_table stock_movements), so ref_id alone
    isn't enough to find it."""
    return conn.execute(
        "SELECT * FROM documents WHERE doc_type = 'transfer' AND ref_table = 'stock_transfers' AND ref_id = ?",
        (transfer_id,),
    ).fetchone()


def shortage(conn: sqlite3.Connection, transfer_id: int) -> list[sqlite3.Row]:
    """Lines of a received transfer whose missing units are still «в пути»."""
    transit_cell = _warehouses.transit_cell_id(conn)
    result = []
    for item in get_items(conn, transfer_id):
        if item["received_qty"] is None or item["received_qty"] >= item["qty"]:
            continue
        held = conn.execute(
            "SELECT qty FROM batch_stock WHERE batch_id = ? AND cell_id = ?", (item["batch_id"], transit_cell)
        ).fetchone()
        if held and held["qty"] > 0:
            result.append(item)
    return result


def write_off_shortage(conn: sqlite3.Connection, transfer_id: int, staff_id: int, reason: str) -> None:
    """Owner's decision on a недостача: what never arrived is written off
    from «В пути» (a document of its own per line, like any списание)."""
    reason = (reason or "").strip()
    if not reason:
        raise TransferError("Укажите причину списания недостачи.")
    lines = shortage(conn, transfer_id)
    if not lines:
        raise TransferError("По этому перемещению нет недостачи в пути.")
    transit_cell = _warehouses.transit_cell_id(conn)
    transfer = get_transfer(conn, transfer_id)
    for item in lines:
        missing = item["qty"] - item["received_qty"]
        movement_id = _inventory.record_movement(
            conn, item["product_id"], missing, "adjustment", staff_id, from_cell_id=transit_cell,
            ref_type="transfer_shortage", ref_id=transfer_id, batch_id=item["batch_id"],
            comment=f"Недостача при перемещении №{transfer_id}: {reason}",
        )
        _documents.register(
            conn, "writeoff", staff_id=staff_id, location_id=transfer["from_location_id"] or transfer["to_location_id"],
            ref_table="stock_movements", ref_id=movement_id,
            title=f"Недостача при перемещении №{transfer_id}: {item['product_name']} × {missing}",
        )


def cancel_sent(conn: sqlite3.Connection, transfer_id: int, staff_id: int) -> None:
    """Undo a transfer nobody has received yet: every line goes from «В
    пути» back to the cell it left. (Called by core.doc_cancel.)"""
    transfer = get_transfer(conn, transfer_id)
    if not transfer or transfer["status"] != "sent":
        raise TransferError("Отменить можно только перемещение, которое ещё в пути.")
    transit_cell = _warehouses.transit_cell_id(conn)
    for item in get_items(conn, transfer_id):
        _inventory.record_movement(
            conn, item["product_id"], item["qty"], "transfer", staff_id, from_cell_id=transit_cell,
            to_cell_id=item["from_cell_id"], ref_type="stock_transfer_cancel", ref_id=transfer_id,
            batch_id=item["batch_id"], comment="Отмена перемещения",
        )
    conn.execute("UPDATE stock_transfers SET status = 'cancelled' WHERE id = ?", (transfer_id,))


def list_transfers(
    conn: sqlite3.Connection, *, status: str | None = None, to_warehouse_ids: list[int] | None = None,
    from_warehouse_ids: list[int] | None = None, limit: int = 100,
) -> list[sqlite3.Row]:
    query = _SELECT + " WHERE 1=1"
    params: list = []
    if status:
        query += " AND stock_transfers.status = ?"
        params.append(status)
    for column, ids in (("to_warehouse_id", to_warehouse_ids), ("from_warehouse_id", from_warehouse_ids)):
        if ids is not None:
            if not ids:
                return []
            query += f" AND stock_transfers.{column} IN ({','.join('?' for _ in ids)})"
            params += ids
    query += " ORDER BY stock_transfers.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def list_discrepancies(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        _SELECT + " WHERE stock_transfers.discrepancy_note IS NOT NULL ORDER BY stock_transfers.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
