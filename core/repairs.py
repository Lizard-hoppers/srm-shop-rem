"""Repair intake, master assignment, status pipeline, parts usage."""
from __future__ import annotations

import html
import os
import sqlite3
import uuid

from core import cash as _cash
from core import clients as _clients
from core import device_catalog as _device_catalog
from core import documents as _documents
from core import repair_notes as _notes
from core import inventory as _inventory
from core import locations as _locations
from core import masters as _masters
from core import notify as _notify
from core import photos as _photos
from core.storage import get_conn as _get_conn
from core.timefmt import kyiv_datetime

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "device_photos")

STATUS_LABELS = {
    "new": "Новый",
    "in_progress": "В работе",
    "ready": "Готов к выдаче",
    "issued": "Выдан",
    "cancelled": "Отменён",
}
STATUSES = list(STATUS_LABELS)

_TIMESTAMP_COLUMN = {
    "in_progress": "started_at",
    "ready": "completed_at",
    "issued": "issued_at",
}

_STATUS_TIMESTAMP_LABEL = {
    "in_progress": "🕐 Взял в работу",
    "ready": "✅ Готово",
    "issued": "📦 Выдан",
}


def list_repairs(
    conn: sqlite3.Connection, status: str | None = None, master_id: int | None = None,
    location_id: int | None = None,
) -> list[sqlite3.Row]:
    query = """SELECT repair_orders.*, clients.name AS client_name, devices.device_type,
                      devices.brand, devices.model, devices.photo_path AS device_photo_path,
                      staff.name AS master_name
               FROM repair_orders
               JOIN clients ON clients.id = repair_orders.client_id
               JOIN devices ON devices.id = repair_orders.device_id
               LEFT JOIN staff ON staff.id = repair_orders.master_id
               WHERE 1=1"""
    params: list = []
    if status:
        query += " AND repair_orders.status = ?"
        params.append(status)
    if master_id:
        query += " AND repair_orders.master_id = ?"
        params.append(master_id)
    if location_id is not None:
        query += " AND repair_orders.location_id = ?"
        params.append(location_id)
    query += " ORDER BY repair_orders.created_at DESC"
    return conn.execute(query, params).fetchall()


def list_repairs_by_client(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    """Every device/visit this client has ever brought in — the client card's history."""
    return conn.execute(
        """SELECT repair_orders.*, devices.device_type, devices.brand, devices.model,
                  devices.serial_number, devices.defect_description, staff.name AS master_name
           FROM repair_orders
           JOIN devices ON devices.id = repair_orders.device_id
           LEFT JOIN staff ON staff.id = repair_orders.master_id
           WHERE repair_orders.client_id = ?
           ORDER BY repair_orders.created_at DESC""",
        (client_id,),
    ).fetchall()


def get_repair(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT repair_orders.*, clients.name AS client_name, clients.phone AS client_phone,
                  devices.device_type, devices.brand, devices.model, devices.serial_number,
                  devices.defect_description, devices.photo_path AS device_photo_path,
                  staff.name AS master_name
           FROM repair_orders
           JOIN clients ON clients.id = repair_orders.client_id
           JOIN devices ON devices.id = repair_orders.device_id
           LEFT JOIN staff ON staff.id = repair_orders.master_id
           WHERE repair_orders.id = ?""",
        (order_id,),
    ).fetchone()


def create_repair(
    conn: sqlite3.Connection,
    client_id: int,
    device_type: str,
    brand: str | None,
    model: str | None,
    serial_number: str | None,
    defect_description: str | None,
    channel: str,
    master_id: int | None,
    price_estimate: int | None,
    staff_id: int,
    *,
    location_id: int | None = None,
    key: str | None = None,
) -> int:
    location_id = _locations.resolve(conn, location_id)
    device_id = conn.execute(
        """INSERT INTO devices (client_id, device_type, brand, model, serial_number, defect_description)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, device_type, brand, model, serial_number, defect_description),
    ).lastrowid

    order_id = conn.execute(
        """INSERT INTO repair_orders (device_id, client_id, master_id, status, channel, price_estimate, location_id)
           VALUES (?, ?, ?, 'new', ?, ?, ?)""",
        (device_id, client_id, master_id, channel, price_estimate, location_id),
    ).lastrowid

    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, 'new', ?, 'Принят')",
        (order_id, staff_id),
    )
    _documents.register(
        conn, "repair", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="repair_orders", ref_id=order_id,
        title=" ".join(x for x in (device_type, brand, model) if x), amount=price_estimate, key=key,
    )
    return order_id


def update_status(conn: sqlite3.Connection, order_id: int, new_status: str, staff_id: int, comment: str | None = None) -> None:
    if new_status not in STATUS_LABELS:
        raise ValueError(f"неизвестный статус: {new_status}")
    previous = conn.execute("SELECT status FROM repair_orders WHERE id = ?", (order_id,)).fetchone()
    previous_status = previous["status"] if previous else None

    timestamp_col = _TIMESTAMP_COLUMN.get(new_status)
    if timestamp_col:
        conn.execute(
            f"UPDATE repair_orders SET status = ?, {timestamp_col} = datetime('now') WHERE id = ?",
            (new_status, order_id),
        )
    else:
        conn.execute("UPDATE repair_orders SET status = ? WHERE id = ?", (new_status, order_id))

    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, ?, ?, ?)",
        (order_id, new_status, staff_id, comment),
    )
    # «Стадия» described the repair as it stood under the old status
    # («ждём запчасть») — once the status moves, it no longer does.
    if previous_status != new_status:
        conn.execute("UPDATE repair_orders SET stage_note = NULL WHERE id = ?", (order_id,))
    # «Выдан» is when the repair earns: the master's share is accrued and
    # the document gets its profit. Moving it back out of «Выдан» undoes both.
    if new_status == "issued" and previous_status != "issued":
        settle(conn, order_id)
    elif previous_status == "issued" and new_status != "issued":
        unsettle(conn, order_id)


# ---- запчасти и прибыль клиентского ремонта (Заход 5) ----
#
# «Клиентский телефон не является товаром фирмы» — the device itself never
# enters stock; only the parts put into it leave it. A part comes from a
# specific партия on the склад of whoever is doing the repair: the
# master's own материально-ответственный склад (what he was handed is what
# he may install), or — when the repair is done by someone who isn't a
# master (an owner at the counter) — the склад of the точка that took the
# repair in.

class RepairPartError(Exception):
    """A part operation that can't go ahead — message is shown to staff as-is."""


def parts_warehouse(conn: sqlite3.Connection, repair: sqlite3.Row) -> sqlite3.Row | None:
    """The склад this repair's parts are taken from (see above)."""
    from core import warehouses as _warehouses  # local: warehouses imports storage only, kept out of module import order

    if repair["master_id"]:
        master = conn.execute("SELECT role FROM staff WHERE id = ?", (repair["master_id"],)).fetchone()
        if master and master["role"] == "master":
            own = _warehouses.master_warehouse(conn, repair["master_id"])
            if own:
                return own
    return _warehouses.point_warehouse(conn, repair["location_id"] or _locations.default_location_id(conn))


def available_parts(conn: sqlite3.Connection, order_id: int) -> list[dict]:
    """What can be installed into this repair right now: repair-part
    stock lines of its parts склад, with the quantity that is free (not
    held for a производство order). One row per партия per cell."""
    repair = get_repair(conn, order_id)
    warehouse = parts_warehouse(conn, repair) if repair else None
    if not warehouse:
        return []
    lines = []
    for line in _inventory.stock_lines(conn, warehouse_id=warehouse["id"]):
        free = line["qty"] - line["reserved"]
        if line["is_repair_part"] and free > 0:
            lines.append({**dict(line), "free": free})
    return lines


def use_part(conn: sqlite3.Connection, order_id: int, batch_id: int, cell_id: int, qty: int, staff_id: int) -> int:
    """Install `qty` of a specific партия into the repair: it leaves the
    parts склад for good, with that партия's cost snapshotted on the
    movement — the cost the repair's profit is netted against."""
    repair = get_repair(conn, order_id)
    if not repair:
        raise RepairPartError("Ремонт не найден.")
    if repair["status"] in ("issued", "cancelled"):
        raise RepairPartError("Ремонт уже закрыт — запчасть списать нельзя.")
    if not qty or qty <= 0:
        raise RepairPartError("Укажите количество.")
    line = next(
        (l for l in available_parts(conn, order_id) if l["batch_id"] == batch_id and l["cell_id"] == cell_id), None
    )
    if not line:
        raise RepairPartError("Этой детали нет на складе исполнителя — выберите из списка.")
    if qty > line["free"]:
        raise RepairPartError(f"«{line['product_name']}»: свободно {line['free']}, нельзя списать {qty}.")
    movement_id = _inventory.record_movement(
        conn, line["product_id"], qty, "repair_use", staff_id, from_cell_id=cell_id,
        ref_type="repair_order", ref_id=order_id, batch_id=batch_id,
    )
    conn.execute("UPDATE repair_orders SET no_parts = 0 WHERE id = ?", (order_id,))
    return movement_id


def declare_no_parts(conn: sqlite3.Connection, order_id: int) -> None:
    """«Без запчасти» — the repair needed none (a cleaning, a firmware).
    Said explicitly, so that «запчасть не указана» always means «forgot»."""
    conn.execute("UPDATE repair_orders SET no_parts = 1 WHERE id = ?", (order_id,))


def needs_part(conn: sqlite3.Connection, repair: sqlite3.Row) -> bool:
    """In work or done, and nobody has said what was installed (or that
    nothing was) — the «Нужна запчасть» flag."""
    if repair["status"] not in ("in_progress", "ready") or repair["no_parts"]:
        return False
    return not get_used_parts(conn, repair["id"])


def finance(conn: sqlite3.Connection, order_id: int) -> dict:
    """The repair's money, as on the презентация's example: клиент
    заплатил 3000 − запчасть 1200 = база 1800 → мастеру 33% = 594 →
    прибыль фирмы 1206. Uses the final price (the estimate until there is
    one) and the parts' snapshotted costs."""
    repair = get_repair(conn, order_id)
    price = repair["price_final"] or repair["price_estimate"] or 0
    row = conn.execute(
        """SELECT COALESCE(SUM(qty * COALESCE(unit_cost, 0)), 0) AS cost FROM stock_movements
           WHERE ref_type = 'repair_order' AND ref_id = ? AND reason = 'repair_use'""",
        (order_id,),
    ).fetchone()
    parts_cost = round(row["cost"], 2)
    parts_cost = int(parts_cost) if parts_cost == int(parts_cost) else parts_cost
    base = price - parts_cost
    master = conn.execute(
        "SELECT pay_type, pay_value FROM staff WHERE id = ?", (repair["master_id"],)
    ).fetchone() if repair["master_id"] else None
    share = _masters.repair_share(base, master["pay_type"], master["pay_value"]) if master else 0
    return {
        "price": price, "parts_cost": parts_cost, "base": base, "master_share": share,
        "firm_profit": base - share,
        "pay_type": master["pay_type"] if master else None, "pay_value": master["pay_value"] if master else None,
        "is_final": bool(repair["price_final"]),
    }


def settle(conn: sqlite3.Connection, order_id: int) -> None:
    """The repair is выдан: accrue the master's share and write the
    document's profit. Safe to call again — it re-does both from scratch."""
    repair = get_repair(conn, order_id)
    numbers = finance(conn, order_id)
    _masters.cancel_accruals(conn, "repair_order", order_id)
    if repair["master_id"]:
        _masters.accrue(
            conn, repair["master_id"], "repair", "repair_order", order_id, numbers["master_share"],
            comment=f"{_documents.label('repair', order_id)}: {numbers['base']} × ставка",
        )
    _documents.set_profit(conn, "repair", order_id, numbers["firm_profit"])


def unsettle(conn: sqlite3.Connection, order_id: int) -> None:
    _masters.cancel_accruals(conn, "repair_order", order_id)
    _documents.set_profit(conn, "repair", order_id, None)


def assign_master(conn: sqlite3.Connection, order_id: int, master_id: int | None) -> None:
    conn.execute("UPDATE repair_orders SET master_id = ? WHERE id = ?", (master_id, order_id))


def set_device_photo(conn: sqlite3.Connection, device_id: int, photo_filename: str | None) -> None:
    conn.execute("UPDATE devices SET photo_path = ? WHERE id = ?", (photo_filename, device_id))


def write_device_photo(device_id: int, data: bytes, ext: str) -> str:
    """Compress (core.photos) and write a device photo to disk, returning
    the filename to hand to set_device_photo. Shared by the web intake
    form and the bot's quick-intake FSM (bot/quick_actions.py) — used to
    be a private helper in webapp.routers.repairs, moved here so both
    entry points write photos identically."""
    compressed = _photos.compress_photo(data)
    if compressed is not None:
        data, ext = compressed, ".jpg"

    os.makedirs(PHOTO_DIR, exist_ok=True)
    filename = f"{device_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(PHOTO_DIR, filename), "wb") as f:
        f.write(data)
    return filename


def create_repair_intake(
    conn: sqlite3.Connection,
    *,
    client_name: str,
    client_phone: str,
    device_type: str,
    brand: str | None,
    model: str | None,
    serial_number: str | None,
    defect_description: str | None,
    channel: str,
    master_id: int | None,
    price_estimate: int | None,
    staff_id: int,
    photo: tuple[bytes, str] | None,
    location_id: int | None = None,
    key: str | None = None,
) -> tuple[int, str, dict | None, tuple[bytes, str] | None]:
    """One client (reused by phone, or created) + one repair order + its
    device photo (if given) + catalog remember — the exact sequence
    webapp.routers.repairs.create_view used to do inline, now shared with
    the bot's quick-intake FSM so the two entry points can never drift on
    required fields or on how a photo gets compressed/stored.

    Returns (order_id, card_text, keyboard, photo_for_notify) — everything
    a caller needs to post the Telegram card via notify_and_save, which is
    deliberately a separate step (not done here): a bare DB-write
    connection should never sit open across a Telegram HTTP call, and a
    web request wants to defer/background that call while a bot handler
    can just await it inline right after this returns."""
    client_id = _clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel)
    order_id = create_repair(
        conn, client_id, device_type, brand, model, serial_number,
        defect_description, channel, master_id, price_estimate, staff_id,
        location_id=location_id, key=key,
    )
    _device_catalog.remember(conn, device_type, brand, model)

    photo_for_notify = None
    if photo:
        data, ext = photo
        repair = get_repair(conn, order_id)
        filename = write_device_photo(repair["device_id"], data, ext)
        set_device_photo(conn, repair["device_id"], filename)
        photo_for_notify = (data, filename)

    card_text, keyboard = card(conn, order_id)
    return order_id, card_text, keyboard, photo_for_notify


def notify_and_save(
    store, order_id: int, card_text: str, keyboard: dict | None, photo: tuple[bytes, str] | None,
) -> None:
    """Posts the new-repair card to a store's groups (core.notify) and
    persists the resulting message ids (save_order_messages — needed for
    later status-change edits via sync_repair_cards). Opens its own
    short-lived connection against store.db_path, same as the FastAPI
    BackgroundTask this used to be inlined into (webapp.routers.repairs)
    — safe to call from a bot handler the same way, right after
    create_repair_intake."""
    sent = _notify.notify_repair_card(
        card_text, reply_markup=keyboard, photo=photo,
        staff_group_chat_id=store.staff_group_chat_id, repair_topic_id=store.repair_topic_id,
        masters_group_chat_id=store.masters_group_chat_id,
    )
    if sent:
        with _get_conn(store.db_path) as conn:
            save_order_messages(conn, order_id, sent)


def claim_repair(conn: sqlite3.Connection, order_id: int, staff_id: int) -> bool:
    """Atomically assign `staff_id` as master and move to in_progress — the
    "Взять в работу" button. Only succeeds from status 'new', and only if
    nobody else is already the assigned master (a master preset at intake
    blocks everyone but themselves). The WHERE-guarded UPDATE + rowcount
    check is what makes two simultaneous button presses resolve safely —
    the second one simply loses, rather than silently overwriting the
    first's claim."""
    cur = conn.execute(
        """UPDATE repair_orders SET status = 'in_progress', stage_note = NULL, master_id = ?, started_at = datetime('now')
           WHERE id = ? AND status = 'new' AND (master_id IS NULL OR master_id = ?)""",
        (staff_id, order_id, staff_id),
    )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, 'in_progress', ?, 'Взял в работу (кнопка в группе)')",
        (order_id, staff_id),
    )
    return True


def complete_repair(conn: sqlite3.Connection, order_id: int, staff_id: int, override: bool = False) -> bool:
    """Atomically move to 'ready' — the "Готово" button. Only succeeds from
    'in_progress', and only by the master who claimed it — unless
    `override` (admin/owner closing on someone else's behalf)."""
    if override:
        cur = conn.execute(
            "UPDATE repair_orders SET status = 'ready', stage_note = NULL, completed_at = datetime('now') WHERE id = ? AND status = 'in_progress'",
            (order_id,),
        )
    else:
        cur = conn.execute(
            """UPDATE repair_orders SET status = 'ready', stage_note = NULL, completed_at = datetime('now')
               WHERE id = ? AND status = 'in_progress' AND master_id = ?""",
            (order_id, staff_id),
        )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, 'ready', ?, 'Готово (кнопка в группе)')",
        (order_id, staff_id),
    )
    return True


def cancel_repair(conn: sqlite3.Connection, order_id: int, staff_id: int, override: bool = False) -> bool:
    """Terminal: the device didn't get fixed and goes back to the client
    as-is — the "❌ Не удалось починить" button. Deliberately NOT a
    release back to the queue (that was this button's behavior until
    21.08 — Павел wants a real "failed" outcome here, not "try someone
    else"); master_id is left as-is so the history still shows who
    attempted it. Only succeeds from 'in_progress', and only by the
    master who claimed it — unless `override` (admin/owner closing on
    someone else's behalf)."""
    if override:
        cur = conn.execute(
            "UPDATE repair_orders SET status = 'cancelled', stage_note = NULL WHERE id = ? AND status = 'in_progress'",
            (order_id,),
        )
    else:
        cur = conn.execute(
            "UPDATE repair_orders SET status = 'cancelled', stage_note = NULL WHERE id = ? AND status = 'in_progress' AND master_id = ?",
            (order_id, staff_id),
        )
    if cur.rowcount == 0:
        return False
    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, 'cancelled', ?, 'Не удалось починить (кнопка в группе)')",
        (order_id, staff_id),
    )
    return True


def save_order_messages(conn: sqlite3.Connection, order_id: int, messages: list[tuple[str, int, str, bool]]) -> None:
    """Persist (chat_id, message_id, kind, has_photo) for a repair's posted
    Telegram cards, so a later status change can edit them in place instead
    of spamming a new message per update. has_photo picks editMessageCaption
    vs editMessageText in core.notify.sync_repair_cards()."""
    conn.executemany(
        "INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind, has_photo) VALUES (?, ?, ?, ?, ?)",
        [(order_id, chat_id, message_id, kind, has_photo) for chat_id, message_id, kind, has_photo in messages],
    )


def get_order_messages(conn: sqlite3.Connection, order_id: int) -> list[tuple[str, int, bool]]:
    """(chat_id, message_id, has_photo) triples for every card posted for
    this repair — what core.notify.sync_repair_cards() needs to edit them
    all in place."""
    rows = conn.execute(
        "SELECT chat_id, message_id, has_photo FROM repair_order_messages WHERE order_id = ?", (order_id,)
    ).fetchall()
    return [(row["chat_id"], row["message_id"], bool(row["has_photo"])) for row in rows]


def find_order_by_message(conn: sqlite3.Connection, chat_id: str, message_id: int) -> int | None:
    """The repair a posted card belongs to, given the chat/message it was
    sent as — how bot/repair_attachments.py resolves a photo replying to
    a card to the repair it should attach to."""
    row = conn.execute(
        "SELECT order_id FROM repair_order_messages WHERE chat_id = ? AND message_id = ?",
        (chat_id, message_id),
    ).fetchone()
    return row["order_id"] if row else None


def add_attachment(
    conn: sqlite3.Connection, order_id: int, photo_path: str, caption: str | None, staff_id: int
) -> int:
    return conn.execute(
        "INSERT INTO repair_attachments (order_id, photo_path, caption, staff_id) VALUES (?, ?, ?, ?)",
        (order_id, photo_path, caption, staff_id),
    ).lastrowid


def get_attachments(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT repair_attachments.*, staff.name AS staff_name
           FROM repair_attachments
           LEFT JOIN staff ON staff.id = repair_attachments.staff_id
           WHERE order_id = ?
           ORDER BY created_at DESC""",
        (order_id,),
    ).fetchall()


def set_price(conn: sqlite3.Connection, order_id: int, price_estimate: int | None, price_final: int | None) -> None:
    conn.execute(
        "UPDATE repair_orders SET price_estimate = ?, price_final = ? WHERE id = ?",
        (price_estimate, price_final, order_id),
    )
    amount = price_final if price_final is not None else price_estimate
    if amount is not None:
        _documents.update_for(conn, "repair", order_id, amount=amount)


def current_price(repair: sqlite3.Row):
    """What the client is to pay as things stand: the final price, or the
    estimate until there is one."""
    if repair["price_final"] is not None:
        return repair["price_final"]
    return repair["price_estimate"]


def change_price(conn: sqlite3.Connection, order_id: int, new_price, staff_id: int | None, author_name: str | None = None):
    """The price changed while the repair is under way («нашли ещё
    поломку»): the estimate becomes the new figure — and the final price
    too, if one had already been set. Zero is a price too («стоимость
    0» — done for free). The change goes into the repair's history
    (who, from what to what), not into its notes — the price itself is
    on the card. Returns the old price. Refused once the repair is
    closed: the money has been taken."""
    repair = get_repair(conn, order_id)
    if not repair:
        raise RepairPartError("Ремонт не найден.")
    if repair["status"] in ("issued", "cancelled"):
        raise RepairPartError(f"Ремонт уже в статусе «{STATUS_LABELS[repair['status']]}» — цену так не поменять.")
    old = current_price(repair)
    set_price(conn, order_id, new_price, new_price if repair["price_final"] is not None else None)
    who = "" if staff_id or not author_name else f" ({author_name})"
    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, ?, ?, ?)",
        (order_id, repair["status"], staff_id, f"Цена изменена: {'—' if old is None else old} → {new_price} грн{who}"),
    )
    return old


# ---- статус из рабочего чата (10.10) ----
#
# «Выдан», «готово», «взял» written in reply to a repair's card move the
# repair right there (bot/repair_attachments.py) — no trip to the CRM.
# Only forward: a message never drags a repair back; that is what the
# «Вернуть» button under the bot's answer (undo_chat_status) is for.

CHAT_MOVES = {
    "new": ("in_progress", "ready", "issued", "cancelled"),
    "in_progress": ("ready", "issued", "cancelled"),
    "ready": ("issued", "cancelled"),
}


def chat_move_allowed(current: str, new_status: str | None) -> bool:
    return new_status in CHAT_MOVES.get(current, ())


def move_from_chat(
    conn: sqlite3.Connection, order_id: int, new_status: str, staff_id: int | None, *,
    account_id: int | None = None, method: str | None = None,
) -> dict:
    """Move a repair to `new_status` on the strength of a chat message.

    «Выдан» takes the money as well, the same way the CRM's form does: the
    whole price onto one account of the repair's точка — `account_id`, or
    the default one for `method` ('cash' / 'card'). A repair with no price
    is handed over with no payment. If only an estimate was on record it
    becomes the final price — that is what the client paid.

    Returns {"previous", "status", "paid", "account"}. Raises
    RepairPartError if the move isn't a forward one, core.cash.PaymentError
    if the account doesn't fit."""
    repair = get_repair(conn, order_id)
    if not repair:
        raise RepairPartError("Ремонт не найден.")
    previous = repair["status"]
    if not chat_move_allowed(previous, new_status):
        raise RepairPartError(
            f"Ремонт в статусе «{STATUS_LABELS[previous]}» — перевести его в «{STATUS_LABELS.get(new_status, new_status)}» по сообщению нельзя."
        )
    paid, account_name = 0, None
    resolved = None
    if new_status == "issued":
        paid = current_price(repair) or 0
        if paid:
            resolved = _cash.resolve_payments(
                conn, repair["location_id"], paid, [(account_id, paid, None)] if account_id else None,
                method if method in _cash.METHODS else "cash",
            )
            account_name = resolved[0]["account"]["name"]
            if not repair["price_final"]:
                set_price(conn, order_id, repair["price_estimate"], paid)
    if new_status == "in_progress" and staff_id and not repair["master_id"]:
        # Whoever says «взял» has it — if he is a master in the CRM.
        is_master = conn.execute("SELECT 1 FROM staff WHERE id = ? AND role = 'master'", (staff_id,)).fetchone()
        if is_master:
            assign_master(conn, order_id, staff_id)
    update_status(conn, order_id, new_status, staff_id, "по сообщению в рабочем чате")
    if resolved:
        _cash.record_payments(conn, "income", resolved, "repair_order", order_id, staff_id)
    return {"previous": previous, "status": new_status, "paid": paid, "account": account_name}


def undo_chat_status(conn: sqlite3.Connection, order_id: int, back_to: str, staff_id: int | None) -> str:
    """«Вернуть»: the bot misread a message (or a person misspoke) — put
    the repair back where it was. Undoing «Выдан» takes its payment back
    out of the касса too (the rows are cancelled, not deleted) and, through
    update_status, the master's accrual and the document's profit.
    Returns the status it was in before the undo."""
    repair = get_repair(conn, order_id)
    if not repair or back_to not in STATUS_LABELS or back_to == repair["status"]:
        raise RepairPartError("Возвращать уже нечего — статус с тех пор изменился.")
    was = repair["status"]
    if was == "issued":
        for row in conn.execute(
            "SELECT id FROM cash_transactions WHERE ref_type = 'repair_order' AND ref_id = ? AND cancelled_at IS NULL",
            (order_id,),
        ).fetchall():
            _cash.cancel_transaction(conn, row["id"])
    update_status(conn, order_id, back_to, staff_id, "возврат статуса из рабочего чата")
    return was


def set_warranty(conn: sqlite3.Connection, order_id: int, warranty_until: str | None) -> None:
    conn.execute("UPDATE repair_orders SET warranty_until = ? WHERE id = ?", (warranty_until, order_id))


def get_status_history(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT repair_status_history.*, staff.name AS staff_name
           FROM repair_status_history
           LEFT JOIN staff ON staff.id = repair_status_history.changed_by
           WHERE order_id = ?
           ORDER BY changed_at ASC""",
        (order_id,),
    ).fetchall()


def get_used_parts(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT stock_movements.*, products.name AS product_name, suppliers.name AS supplier_name
           FROM stock_movements
           JOIN products ON products.id = stock_movements.product_id
           LEFT JOIN batches ON batches.id = stock_movements.batch_id
           LEFT JOIN suppliers ON suppliers.id = batches.supplier_id
           WHERE stock_movements.ref_type = 'repair_order' AND stock_movements.ref_id = ?
           ORDER BY stock_movements.created_at DESC""",
        (order_id,),
    ).fetchall()


# How many notes the group card shows, and how long each may be — a card
# with a photo is a caption, and Telegram cuts those at 1024 characters.
CARD_NOTES = 3
CARD_NOTE_LEN = 110
CARD_TEXT_LIMIT = 1000


def render_card_text(
    repair: sqlite3.Row, parts: list | None = None, notes: list | None = None, notes_shown: int = CARD_NOTES,
) -> str:
    """HTML-formatted staff-group/topic card for a repair — shared by the
    web panel (on intake) and the bot (after a button press edits it in
    place), so the two channels never drift apart on wording. Escapes
    every user-supplied field: this goes out with parse_mode=HTML, and a
    device model or defect description is free text a client or master can
    type anything into.

    Laid out as on the макет: «РК-101 • В работе», клиент, устройство,
    работа, ответственный, запчасть. `parts` is get_used_parts() — pass it
    whenever the repair may have any (card() below does); the «Запчасть»
    line reads «не указана» while the repair is in work or done and
    nobody has said what went in."""
    device = " ".join(filter(None, [repair["device_type"], repair["brand"], repair["model"]]))
    master_label = html.escape(repair["master_name"]) if repair["master_name"] else "не назначен"
    price = current_price(repair)
    client = html.escape(repair["client_phone"] or "—")
    if repair["client_name"] and repair["client_name"] != repair["client_phone"]:
        client += f" · {html.escape(repair['client_name'])}"

    lines = [
        f"🔧 <b>{_documents.label('repair', repair['id'])} • {STATUS_LABELS[repair['status']]}</b>",
        "",
        f"Клиент: {client}",
        f"Устройство: {html.escape(device)}",
    ]
    if repair["defect_description"]:
        lines.append(f"Работа: {html.escape(repair['defect_description'])}")
    lines.append(f"Ответственный: {master_label}")
    if repair["stage_note"]:
        lines.append(f"Стадия: <b>{html.escape(repair['stage_note'])}</b>")
    if parts:
        lines.append("Запчасть: " + "; ".join(
            f"{html.escape(p['product_name'])} × {p['qty']}" + (f" (партия {p['batch_id']})" if p["batch_id"] else "")
            for p in parts
        ))
    elif repair["no_parts"]:
        lines.append("Запчасть: без запчасти")
    elif repair["status"] in ("in_progress", "ready"):
        lines.append("Запчасть: <b>не указана</b>")
    lines.append("Цена: не указана" if price is None else ("Цена: 0 грн (бесплатно)" if not price else f"Цена: {price} грн"))

    timestamp_label = _STATUS_TIMESTAMP_LABEL.get(repair["status"])
    timestamp_col = _TIMESTAMP_COLUMN.get(repair["status"])
    if timestamp_label and timestamp_col and repair[timestamp_col]:
        lines.append(f"{timestamp_label}: {kyiv_datetime(repair[timestamp_col])}")

    # What was said about this repair in the chat (core.repair_notes) —
    # the latest few, short; all of them are on the repair's page.
    # They go out as a quote (Telegram's <blockquote>) — set apart from
    # the card's own lines, the way quoted words are.
    if notes:
        lines.append("")
        lines.append(f"📝 <b>Заметки ({len(notes)})</b>" + (" — последние:" if 0 < notes_shown < len(notes) else ""))
        quoted = []
        for note in (notes[-notes_shown:] if notes_shown else []):
            text = note["text"] if len(note["text"]) <= CARD_NOTE_LEN else note["text"][: CARD_NOTE_LEN - 1] + "…"
            quoted.append(f"{html.escape(text)} — <i>{html.escape(note['author'])}</i>")
        if quoted:
            lines.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")

    return "\n".join(lines)


def card(conn: sqlite3.Connection, order_id: int) -> tuple[str, dict]:
    """(text, keyboard) of a repair's card as it should look right now."""
    repair = get_repair(conn, order_id)
    parts, notes = get_used_parts(conn, order_id), _notes.card_notes(conn, order_id)
    text = render_card_text(repair, parts, notes)
    # A card with a photo is a caption, cut by Telegram at 1024 characters
    # — and a cut that lands inside the notes' quote leaves broken markup,
    # which Telegram rejects outright (the card would silently stop
    # updating). So when the card runs long, it shows fewer notes rather
    # than a broken one; all of them are on the repair's page regardless.
    shown = CARD_NOTES
    while notes and len(text) > CARD_TEXT_LIMIT and shown > 0:
        shown -= 1
        text = render_card_text(repair, parts, notes, shown)
    return text, render_keyboard(order_id, repair["status"])


def render_keyboard(order_id: int, status: str) -> dict:
    """Inline keyboard matching a repair's current status, as a plain dict
    in the Telegram Bot API's InlineKeyboardMarkup shape. Status-action
    rows disappear once the job reaches a final state (nothing left to
    press there), but the "Открыть в CRM" row stays forever — a card in
    the group is the group's whole history of that repair, so it should
    always be able to jump into the full record, done or not.

    That last row is a callback (bot.repair_actions.open_crm_repair), not
    a plain link: this card is posted once into a shared GROUP chat and
    stays there indefinitely, so a URL baked in at render time would
    forever open the CRM as whoever it was minted for — the callback
    mints a fresh, tapper-scoped link at tap time instead (see
    bot/miniapp_links.py's module docstring)."""
    rows = []
    if status == "new":
        rows.append([{"text": "🔧 Взять в работу", "callback_data": f"repair_take:{order_id}"}])
    elif status == "in_progress":
        rows.append([
            {"text": "⚙️ Указать запчасть", "callback_data": f"repair_part:{order_id}"},
            {"text": "✅ Готово", "callback_data": f"repair_done:{order_id}"},
        ])
        rows.append([{"text": "❌ Не удалось починить", "callback_data": f"repair_release:{order_id}"}])
    elif status == "ready":
        # Done, but what was installed can still be said (or corrected)
        # until the device is handed over.
        rows.append([{"text": "⚙️ Указать запчасть", "callback_data": f"repair_part:{order_id}"}])
    rows.append([{"text": "🔗 Открыть в CRM", "callback_data": f"open_crm:repair:{order_id}"}])
    return {"inline_keyboard": rows}
