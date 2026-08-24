"""Repair intake, master assignment, status pipeline, parts usage."""
from __future__ import annotations

import html
import os
import sqlite3
import uuid

from core import clients as _clients
from core import device_catalog as _device_catalog
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
    conn: sqlite3.Connection, status: str | None = None, master_id: int | None = None
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
) -> int:
    device_id = conn.execute(
        """INSERT INTO devices (client_id, device_type, brand, model, serial_number, defect_description)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (client_id, device_type, brand, model, serial_number, defect_description),
    ).lastrowid

    order_id = conn.execute(
        """INSERT INTO repair_orders (device_id, client_id, master_id, status, channel, price_estimate)
           VALUES (?, ?, ?, 'new', ?, ?)""",
        (device_id, client_id, master_id, channel, price_estimate),
    ).lastrowid

    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) VALUES (?, 'new', ?, 'Принят')",
        (order_id, staff_id),
    )
    return order_id


def update_status(conn: sqlite3.Connection, order_id: int, new_status: str, staff_id: int, comment: str | None = None) -> None:
    if new_status not in STATUS_LABELS:
        raise ValueError(f"неизвестный статус: {new_status}")

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
    )
    _device_catalog.remember(conn, device_type, brand, model)

    photo_for_notify = None
    if photo:
        data, ext = photo
        repair = get_repair(conn, order_id)
        filename = write_device_photo(repair["device_id"], data, ext)
        set_device_photo(conn, repair["device_id"], filename)
        photo_for_notify = (data, filename)

    repair = get_repair(conn, order_id)
    keyboard = render_keyboard(order_id, repair["status"])
    card_text = render_card_text(repair)
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
        """UPDATE repair_orders SET status = 'in_progress', master_id = ?, started_at = datetime('now')
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
            "UPDATE repair_orders SET status = 'ready', completed_at = datetime('now') WHERE id = ? AND status = 'in_progress'",
            (order_id,),
        )
    else:
        cur = conn.execute(
            """UPDATE repair_orders SET status = 'ready', completed_at = datetime('now')
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
            "UPDATE repair_orders SET status = 'cancelled' WHERE id = ? AND status = 'in_progress'",
            (order_id,),
        )
    else:
        cur = conn.execute(
            "UPDATE repair_orders SET status = 'cancelled' WHERE id = ? AND status = 'in_progress' AND master_id = ?",
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
        """SELECT stock_movements.*, products.name AS product_name
           FROM stock_movements
           JOIN products ON products.id = stock_movements.product_id
           WHERE stock_movements.ref_type = 'repair_order' AND stock_movements.ref_id = ?
           ORDER BY stock_movements.created_at DESC""",
        (order_id,),
    ).fetchall()


def render_card_text(repair: sqlite3.Row) -> str:
    """HTML-formatted staff-group/topic card for a repair — shared by the
    web panel (on intake) and the bot (after a button press edits it in
    place), so the two channels never drift apart on wording. Escapes
    every user-supplied field: this goes out with parse_mode=HTML, and a
    device model or defect description is free text a client or master can
    type anything into (same class of input as the repairs_list.html
    device-catalog XSS fix, different sink — a Telegram message, not a
    page)."""
    device = " ".join(filter(None, [repair["device_type"], repair["brand"], repair["model"]]))
    channel_label = "Онлайн" if repair["channel"] == "online" else "Офлайн"
    master_label = html.escape(repair["master_name"]) if repair["master_name"] else "не назначен"
    price_label = f"{repair['price_estimate']} грн" if repair["price_estimate"] else "не указана"

    lines = [
        f"🔧 <b>Ремонт №{repair['id']} — {STATUS_LABELS[repair['status']]}</b>",
        "",
        f"Клиент: {html.escape(repair['client_name'])}",
        f"Телефон: {html.escape(repair['client_phone'] or '—')}",
        f"Устройство: {html.escape(device)}",
    ]
    if repair["defect_description"]:
        lines.append(f"Неисправность: {html.escape(repair['defect_description'])}")
    lines.append(f"Мастер: {master_label}")
    lines.append(f"Оценка: {price_label}")
    lines.append(f"Канал: {channel_label}")

    timestamp_label = _STATUS_TIMESTAMP_LABEL.get(repair["status"])
    timestamp_col = _TIMESTAMP_COLUMN.get(repair["status"])
    if timestamp_label and timestamp_col and repair[timestamp_col]:
        lines.append(f"{timestamp_label}: {kyiv_datetime(repair[timestamp_col])}")

    return "\n".join(lines)


def render_keyboard(order_id: int, status: str) -> dict | None:
    """Inline keyboard matching a repair's current status, as a plain dict
    in the Telegram Bot API's InlineKeyboardMarkup shape — None once the
    job reaches a final state (nothing left to press)."""
    if status == "new":
        return {"inline_keyboard": [[{"text": "🔧 Взять в работу", "callback_data": f"repair_take:{order_id}"}]]}
    if status == "in_progress":
        return {
            "inline_keyboard": [[
                {"text": "✅ Готов к выдаче", "callback_data": f"repair_done:{order_id}"},
                {"text": "❌ Не удалось починить", "callback_data": f"repair_release:{order_id}"},
            ]]
        }
    return None
