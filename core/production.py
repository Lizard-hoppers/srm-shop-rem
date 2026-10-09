"""Производство / «наш ремонт» (Заход 5): a phone the business owns goes
to a master together with parts from our склады and comes back worth more.
«Производство не создаёт прибыль — оно формирует фактическую себестоимость
готового товара»: nothing here is income or an expense; what changes is
where the goods are, what the phone has cost us so far, and what we owe
the master. One document (ПР) per order.

The order's life, as on the макет «Карточка и производство»:

  draft     the phone is picked and held; parts are chosen from ANY склад
            of the business — each a specific партия, so its supplier and
            cost are known — and reserved («доступно = остаток − резерв»);
            a master (штатный or аутсорс) is assigned.
  in_work   «Передать в производство»: the phone and the parts move to the
            master's склад (a перемещение confirmed at handover) and stay
            held for this order — «передача ≠ расход».
  reported  the master's report: which of our parts he actually used, what
            he added of his own («шлейф мастера — 400 грн»), the price of
            the work.
  done      «Принять результат»: used parts are written off, the phone's
            cost becomes  было + наши детали + детали мастера + работа,
            the master is owed работа + his own parts, and the phone with
            every unused part is sent back to the точка («Мастер → В пути
            → Точка»).

Everything that can refuse is checked before the first write.
"""
from __future__ import annotations

import sqlite3

from core import accounts as _accounts
from core import documents as _documents
from core import inventory as _inventory
from core import masters as _masters
from core import stock_transfers as _transfers
from core import warehouses as _warehouses
from core.accounts import money

STATUS_LABELS = {
    "draft": "Подбор деталей",
    "in_work": "У мастера",
    "reported": "Отчёт сдан",
    "done": "Готов к продаже",
    "cancelled": "Отменён",
}
ACTIVE_STATUSES = ("draft", "in_work", "reported")

_REF = "production"


class ProductionError(Exception):
    """A производство step that can't go ahead — message is shown to staff as-is."""


def _ref(order_id: int) -> tuple[str, int]:
    return (_REF, order_id)


_SELECT = """SELECT production_orders.*, products.name AS product_name, batches.imei AS imei,
                    batches.unit_cost_uah AS unit_cost_now, staff.name AS master_name,
                    staff.master_kind AS master_kind, staff.skills AS master_skills,
                    locations.name AS location_name, creator.name AS created_by_name
             FROM production_orders
             JOIN products ON products.id = production_orders.product_id
             JOIN batches ON batches.id = production_orders.batch_id
             LEFT JOIN staff ON staff.id = production_orders.master_id
             LEFT JOIN locations ON locations.id = production_orders.location_id
             LEFT JOIN staff AS creator ON creator.id = production_orders.created_by"""


def get_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE production_orders.id = ?", (order_id,)).fetchone()


def list_orders(
    conn: sqlite3.Connection, *, statuses: tuple[str, ...] | None = None, master_id: int | None = None,
    location_id: int | None = None, limit: int = 200,
) -> list[sqlite3.Row]:
    query = _SELECT + " WHERE 1=1"
    params: list = []
    if statuses:
        query += f" AND production_orders.status IN ({','.join('?' for _ in statuses)})"
        params += list(statuses)
    if master_id:
        query += " AND production_orders.master_id = ?"
        params.append(master_id)
    if location_id is not None:
        query += " AND production_orders.location_id = ?"
        params.append(location_id)
    query += " ORDER BY production_orders.id DESC LIMIT ?"
    params.append(limit)
    return conn.execute(query, params).fetchall()


def active_order_for_batch(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _SELECT + f" WHERE production_orders.batch_id = ? AND production_orders.status IN ({','.join('?' for _ in ACTIVE_STATUSES)})",
        (batch_id, *ACTIVE_STATUSES),
    ).fetchone()


def get_parts(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT production_parts.*, products.name AS product_name, products.unit,
                  batches.unit_cost_uah AS unit_cost_uah, suppliers.name AS supplier_name,
                  batches.receipt_id AS receipt_id, storage_cells.code AS cell_code,
                  warehouses.id AS warehouse_id, warehouses.kind AS warehouse_kind, warehouses.name AS warehouse_name,
                  locations.name AS location_name, wstaff.name AS staff_name
           FROM production_parts
           JOIN products ON products.id = production_parts.product_id
           JOIN batches ON batches.id = production_parts.batch_id
           LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
           JOIN storage_cells ON storage_cells.id = production_parts.cell_id
           JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
           LEFT JOIN locations ON locations.id = warehouses.location_id
           LEFT JOIN staff AS wstaff ON wstaff.id = warehouses.staff_id
           WHERE production_parts.order_id = ? ORDER BY production_parts.id""",
        (order_id,),
    ).fetchall()


def get_extras(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM production_extras WHERE order_id = ? ORDER BY id", (order_id,)
    ).fetchall()


def _unit_line(conn: sqlite3.Connection, batch_id: int) -> sqlite3.Row | None:
    """Where the phone physically is (its one stock line), or None."""
    return conn.execute(
        """SELECT batch_stock.cell_id, batch_stock.qty, storage_cells.warehouse_id, warehouses.kind AS warehouse_kind
           FROM batch_stock
           JOIN storage_cells ON storage_cells.id = batch_stock.cell_id
           JOIN warehouses ON warehouses.id = storage_cells.warehouse_id
           WHERE batch_stock.batch_id = ? AND batch_stock.qty > 0 LIMIT 1""",
        (batch_id,),
    ).fetchone()


def create_order(
    conn: sqlite3.Connection, *, imei: str, staff_id: int, location_id: int, task: str | None = None,
    key: str | None = None,
) -> int:
    """Start an order for the phone with this IMEI: it must be ours, on a
    склад (not «в пути»), free, and not already in another order. The
    phone is held for the order from this moment."""
    unit = _inventory.find_unit_by_imei(conn, imei)
    if not unit:
        raise ProductionError("Телефон с таким IMEI не числится на складе.")
    line = _unit_line(conn, unit["id"])
    if not line or line["warehouse_kind"] == "transit":
        raise ProductionError("Телефон сейчас в пути — сначала примите перемещение.")
    existing = active_order_for_batch(conn, unit["id"])
    if existing:
        raise ProductionError(f"Этот телефон уже в заказе {_documents.label('production', existing['id'])}.")
    if _inventory.available_qty(conn, unit["id"], line["cell_id"]) < 1:
        raise ProductionError("Телефон в резерве под другой документ.")
    buyback = conn.execute(
        "SELECT id FROM buyback_orders WHERE batch_id = ? ORDER BY id DESC LIMIT 1", (unit["id"],)
    ).fetchone()

    order_id = conn.execute(
        """INSERT INTO production_orders
           (location_id, product_id, batch_id, buyback_order_id, task, cost_before, created_by)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (location_id, unit["product_id"], unit["id"], buyback["id"] if buyback else None,
         (task or "").strip() or None, unit["unit_cost_uah"] or 0, staff_id),
    ).lastrowid
    _inventory.reserve(conn, unit["id"], line["cell_id"], 1, _REF, order_id, staff_id)
    _documents.register(
        conn, "production", staff_id=staff_id, location_id=location_id, ref_table="production_orders",
        ref_id=order_id, title=f"{unit['product_name']} · IMEI {unit['imei']}", amount=unit["unit_cost_uah"], key=key,
    )
    return order_id


def _require(conn: sqlite3.Connection, order_id: int, *statuses: str) -> sqlite3.Row:
    order = get_order(conn, order_id)
    if not order:
        raise ProductionError("Заказ не найден.")
    if order["status"] not in statuses:
        raise ProductionError(f"Заказ в статусе «{STATUS_LABELS[order['status']]}» — это действие сейчас недоступно.")
    return order


def available_parts(conn: sqlite3.Connection, order_id: int, search: str | None = None) -> list[dict]:
    """«Детали со всех складов»: every free repair-part stock line of the
    business — точки and masters alike, not «в пути» — each a specific
    партия with its supplier, приход and cost. `free` counts what this
    order itself already holds as still pickable."""
    needle = (search or "").strip().lower()
    result = []
    for line in _inventory.stock_lines(conn):
        if not line["is_repair_part"] or line["warehouse_kind"] == "transit":
            continue
        if needle and needle not in line["product_name"].lower():
            continue
        free = _inventory.available_qty(conn, line["batch_id"], line["cell_id"], reserved_for=_ref(order_id))
        if free <= 0:
            continue
        result.append({**dict(line), "free": free})
    return result


def set_parts(conn: sqlite3.Connection, order_id: int, lines: list[tuple[int, int, int]], staff_id: int) -> None:
    """«Зарезервировать»: the order's parts become exactly `lines`
    ([(batch_id, cell_id, qty), …]). Whatever it held before is let go
    first, so re-picking is just picking again."""
    _require(conn, order_id, "draft")
    wanted: dict[tuple[int, int], int] = {}
    for batch_id, cell_id, qty in lines:
        if qty and qty > 0:
            wanted[(batch_id, cell_id)] = wanted.get((batch_id, cell_id), 0) + qty
    pickable = {(l["batch_id"], l["cell_id"]): l for l in available_parts(conn, order_id)}
    for key, qty in wanted.items():
        line = pickable.get(key)
        if not line:
            raise ProductionError("Одной из выбранных деталей уже нет в свободном остатке — обновите список.")
        if qty > line["free"]:
            raise ProductionError(f"«{line['product_name']}»: свободно {line['free']}, нельзя зарезервировать {qty}.")

    for part in get_parts(conn, order_id):
        _inventory.release(conn, _REF, order_id, batch_id=part["batch_id"], cell_id=part["cell_id"])
    conn.execute("DELETE FROM production_parts WHERE order_id = ?", (order_id,))
    for (batch_id, cell_id), qty in wanted.items():
        _inventory.reserve(conn, batch_id, cell_id, qty, _REF, order_id, staff_id)
        conn.execute(
            "INSERT INTO production_parts (order_id, product_id, batch_id, cell_id, qty) VALUES (?, ?, ?, ?, ?)",
            (order_id, pickable[(batch_id, cell_id)]["product_id"], batch_id, cell_id, qty),
        )


def assign_master(conn: sqlite3.Connection, order_id: int, master_id: int | None, work_price=None) -> None:
    """Who does the job and what the work itself will cost («работа
    мастера — фиксированная стоимость»). The price can still be corrected
    in the report."""
    _require(conn, order_id, "draft")
    if master_id:
        master = conn.execute(
            "SELECT id FROM staff WHERE id = ? AND role = 'master' AND active = 1", (master_id,)
        ).fetchone()
        if not master:
            raise ProductionError("Выберите действующего мастера.")
    conn.execute(
        "UPDATE production_orders SET master_id = ?, work_price = ? WHERE id = ?",
        (master_id or None, _accounts.parse_amount(work_price), order_id),
    )


def hand_over(conn: sqlite3.Connection, order_id: int, staff_id: int) -> list[int]:
    """«Передать в производство»: the phone and every reserved part move
    to the master's склад — one перемещение per склад they come from,
    confirmed right here at handover — and stay held for the order there.
    Returns the ids of the transfers made."""
    order = _require(conn, order_id, "draft")
    if not order["master_id"]:
        raise ProductionError("Сначала выберите мастера.")
    master_wh = _warehouses.master_warehouse(conn, order["master_id"])
    if not master_wh:
        raise ProductionError("У мастера нет склада — откройте его карточку в «Мастерах» и сохраните.")
    master_cell = _warehouses.cells(conn, master_wh["id"])[0]["id"]
    unit = _unit_line(conn, order["batch_id"])
    if not unit or unit["warehouse_kind"] == "transit":
        raise ProductionError("Телефон сейчас не на складе — передать нечего.")

    # (source warehouse) -> [(batch, cell, qty)] for everything not already with the master.
    by_source: dict[int, list[tuple[int, int, int]]] = {}
    if unit["warehouse_id"] != master_wh["id"]:
        by_source.setdefault(unit["warehouse_id"], []).append((order["batch_id"], unit["cell_id"], 1))
    parts = get_parts(conn, order_id)
    for part in parts:
        if part["warehouse_id"] != master_wh["id"]:
            by_source.setdefault(part["warehouse_id"], []).append((part["batch_id"], part["cell_id"], part["qty"]))

    label = _documents.label("production", order_id)
    transfer_ids = []
    for source_id, lines in by_source.items():
        transfer_id = _transfers.send(
            conn, source_id, master_wh["id"], lines, staff_id, comment=f"{label}: в производство",
            reserved_for=_ref(order_id),
        )
        _transfers.receive(conn, transfer_id, staff_id, cell_id=master_cell)
        transfer_ids.append(transfer_id)

    # Held for the order at the master's склад too: his other jobs (a
    # client repair's «указать запчасть») must not take these.
    _inventory.release(conn, _REF, order_id)
    _inventory.reserve(conn, order["batch_id"], master_cell, 1, _REF, order_id, staff_id)
    for part in parts:
        _inventory.reserve(conn, part["batch_id"], master_cell, part["qty"], _REF, order_id, staff_id)
    conn.execute("UPDATE production_parts SET cell_id = ? WHERE order_id = ?", (master_cell, order_id))
    conn.execute(
        "UPDATE production_orders SET status = 'in_work', handed_at = datetime('now') WHERE id = ?", (order_id,)
    )
    doc = _documents.get_for(conn, "production", order_id)
    if doc:
        _documents.add_event(conn, doc["id"], "handed", staff_id, f"мастеру: {order['master_name']}")
    return transfer_ids


def submit_report(
    conn: sqlite3.Connection, order_id: int, staff_id: int, *, used: dict[int, int] | None = None,
    extras: list[tuple[str, object]] | None = None, work_price=None, note: str | None = None,
) -> None:
    """«Отчёт мастера». `used` maps a production_parts id to how many of
    it actually went into the phone (missing = all); `extras` is what he
    added of his own — [(название, сумма), …]; work_price is the work
    itself. Can be re-submitted until the result is accepted."""
    _require(conn, order_id, "in_work", "reported")
    used = used or {}
    parts = get_parts(conn, order_id)
    for part in parts:
        qty = used.get(part["id"], part["qty"])
        if qty < 0 or qty > part["qty"]:
            raise ProductionError(f"«{part['product_name']}»: передано {part['qty']}, нельзя отчитаться о {qty}.")
    clean_extras = []
    for title, amount in extras or []:
        title = (title or "").strip()
        value = _accounts.parse_amount(amount)
        if not title and not str(amount or "").strip():
            continue
        if not title or not value:
            raise ProductionError("У каждой своей детали мастера нужны название и сумма.")
        clean_extras.append((title, value))
    work = None
    if str(work_price or "").strip():
        work = _accounts.parse_amount(work_price)
        if work is None:
            raise ProductionError("Проверьте стоимость работы.")

    for part in parts:
        conn.execute(
            "UPDATE production_parts SET used_qty = ? WHERE id = ?", (used.get(part["id"], part["qty"]), part["id"])
        )
    conn.execute("DELETE FROM production_extras WHERE order_id = ?", (order_id,))
    for title, value in clean_extras:
        conn.execute(
            "INSERT INTO production_extras (order_id, title, amount) VALUES (?, ?, ?)", (order_id, title, value)
        )
    conn.execute(
        """UPDATE production_orders SET status = 'reported', reported_at = datetime('now'),
                                         work_price = COALESCE(?, work_price), report_note = ?
           WHERE id = ?""",
        (work, (note or "").strip() or None, order_id),
    )
    doc = _documents.get_for(conn, "production", order_id)
    if doc:
        _documents.add_event(conn, doc["id"], "reported", staff_id)


def preview(conn: sqlite3.Connection, order_id: int) -> dict:
    """The «Результат» numbers as they stand: what the phone cost before,
    our parts (those reported used, or all of them before any report),
    the master's own parts, the work, the total — and what the master is
    owed. After accept the stored cost_* columns are returned as they are."""
    order = get_order(conn, order_id)
    if order["status"] == "done":
        before, parts, extras, work = (order["cost_before"] or 0, order["cost_parts"] or 0,
                                       order["cost_extras"] or 0, order["cost_work"] or 0)
    else:
        before = order["cost_before"] or 0
        parts = sum(
            (p["used_qty"] if p["used_qty"] is not None else p["qty"]) * (p["unit_cost_uah"] or 0)
            for p in get_parts(conn, order_id)
        )
        extras = sum(e["amount"] for e in get_extras(conn, order_id))
        work = order["work_price"] or 0
    return {
        "before": money(before), "parts": money(parts), "extras": money(extras), "work": money(work),
        "total": money(before + parts + extras + work), "to_master": money(extras + work),
    }


def accept(conn: sqlite3.Connection, order_id: int, staff_id: int) -> int | None:
    """«Принять результат». Returns the id of the перемещение that takes
    the phone (and unused parts) back to the точка, or None if there was
    nothing to send."""
    order = _require(conn, order_id, "reported")
    master_wh = _warehouses.master_warehouse(conn, order["master_id"])
    point = _warehouses.point_warehouse(conn, order["location_id"])
    unit = _unit_line(conn, order["batch_id"])
    if not master_wh or not unit or unit["warehouse_id"] != master_wh["id"]:
        raise ProductionError("Телефон не на складе мастера — принять результат нельзя.")
    numbers = preview(conn, order_id)
    parts = get_parts(conn, order_id)
    label = _documents.label("production", order_id)

    back = [(order["batch_id"], unit["cell_id"], 1)]
    for part in parts:
        used = part["used_qty"] if part["used_qty"] is not None else part["qty"]
        if used:
            _inventory.record_movement(
                conn, part["product_id"], used, "repair_use", staff_id, from_cell_id=part["cell_id"],
                ref_type="production_order", ref_id=order_id, batch_id=part["batch_id"],
                reserved_for=_ref(order_id), comment=f"{label}: установлено",
            )
        if part["qty"] - used:
            back.append((part["batch_id"], part["cell_id"], part["qty"] - used))

    # The phone now carries everything that went into it.
    conn.execute("UPDATE batches SET unit_cost_uah = ? WHERE id = ?", (numbers["total"], order["batch_id"]))
    _masters.accrue(
        conn, order["master_id"], "production", "production_order", order_id, numbers["to_master"],
        comment=f"{label}: работа {numbers['work']} + свои детали {numbers['extras']}",
    )

    # «Мастер → В пути → Точка»: the finished phone and what wasn't used.
    transfer_id = _transfers.send(
        conn, master_wh["id"], point["id"], back, staff_id, comment=f"{label}: из производства",
        reserved_for=_ref(order_id),
    ) if point else None
    _inventory.release(conn, _REF, order_id)
    conn.execute(
        """UPDATE production_orders SET status = 'done', done_at = datetime('now'), cost_parts = ?, cost_extras = ?,
                                         cost_work = ?, cost_after = ?, return_transfer_id = ?
           WHERE id = ?""",
        (numbers["parts"], numbers["extras"], numbers["work"], numbers["total"], transfer_id, order_id),
    )
    _documents.update_for(conn, "production", order_id, amount=numbers["total"])
    doc = _documents.get_for(conn, "production", order_id)
    if doc:
        _documents.add_event(conn, doc["id"], "accepted", staff_id, f"себестоимость {numbers['total']} грн")
    return transfer_id


def cancel(conn: sqlite3.Connection, order_id: int, staff_id: int, *, mark_document: bool = True) -> int | None:
    """Call the job off — any time before the result is accepted.

    A draft: everything held is simply let go. Already with the master
    (in_work / reported): the phone and EVERY part handed over go back
    «Мастер → В пути → Точка» in one перемещение — nothing is written
    off, the phone's cost doesn't change, nothing is accrued, and a report
    already filed is disregarded. Returns that перемещение's id (None for
    a draft); the точка still has to receive it.

    An accepted order (done) can't be cancelled: the parts are spent and
    the phone carries its new cost."""
    order = _require(conn, order_id, "draft", "in_work", "reported")
    transfer_id = None
    if order["status"] != "draft":
        master_wh = _warehouses.master_warehouse(conn, order["master_id"])
        point = _warehouses.point_warehouse(conn, order["location_id"])
        unit = _unit_line(conn, order["batch_id"])
        if not master_wh or not point or not unit or unit["warehouse_id"] != master_wh["id"]:
            raise ProductionError("Телефон не на складе мастера — вернуть его перемещением не получится.")
        back = [(order["batch_id"], unit["cell_id"], 1)]
        for part in get_parts(conn, order_id):
            if _inventory.available_qty(conn, part["batch_id"], part["cell_id"], reserved_for=_ref(order_id)) < part["qty"]:
                raise ProductionError(f"«{part['product_name']}» уже нет на складе мастера в нужном количестве.")
            back.append((part["batch_id"], part["cell_id"], part["qty"]))
        transfer_id = _transfers.send(
            conn, master_wh["id"], point["id"], back, staff_id,
            comment=f"{_documents.label('production', order_id)}: отмена, возврат от мастера", reserved_for=_ref(order_id),
        )
    _inventory.release(conn, _REF, order_id)
    conn.execute(
        "UPDATE production_orders SET status = 'cancelled', return_transfer_id = ? WHERE id = ?", (transfer_id, order_id),
    )
    if mark_document:
        doc = _documents.get_for(conn, "production", order_id)
        if doc:
            _documents.mark_cancelled(
                conn, doc["id"], staff_id,
                "заказ отменён, телефон и детали возвращаются от мастера" if transfer_id else "заказ отменён до передачи мастеру",
            )
    return transfer_id
