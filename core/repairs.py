"""Repair intake, master assignment, status pipeline, parts usage."""
from __future__ import annotations

import sqlite3

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


def list_repairs(
    conn: sqlite3.Connection, status: str | None = None, master_id: int | None = None
) -> list[sqlite3.Row]:
    query = """SELECT repair_orders.*, clients.name AS client_name, devices.device_type,
                      devices.brand, devices.model, staff.name AS master_name
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


def get_repair(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT repair_orders.*, clients.name AS client_name, clients.phone AS client_phone,
                  devices.device_type, devices.brand, devices.model, devices.serial_number,
                  devices.defect_description, staff.name AS master_name
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
