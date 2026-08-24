"""Скупка техники у клиентов — покупка устройств у частных лиц, а не у
поставщиков (см. core.purchases для того случая). Две ветки по
назначению:

- purpose='parts' — просто запись-факт (для истории/кассы). Разборка на
  компоненты остаётся ручной операцией: мастер физически разбирает
  устройство и заносит получившиеся запчасти через обычный Приход
  (core.purchases) — автоматически угадывать состав разборки нереалистично
  и не нужно.
- purpose='resale' — устройство сразу становится обычным товаром (см.
  create_buyback_intake) с qty=1 в каталоге и продаётся через уже
  существующие Продажи — никакой отдельной логики продажи не изобретаем,
  весь путь (карточка товара, движение склада, чек) переиспользуется как
  есть.

Оба входа — веб-форма (webapp/routers/buyback.py) и бот (bot/quick_actions.py)
— используют create_buyback_intake, так что поведение не может разъехаться
между ними (тот же принцип, что core.repairs.create_repair_intake)."""
from __future__ import annotations

import os
import sqlite3
import uuid

from core import cash as _cash
from core import clients as _clients
from core import inventory as _inventory
from core import photos as _photos

PURPOSES = {"parts": "На запчасти", "resale": "На продажу"}

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "buyback_photos")

_BUYBACK_CELL_CODE = "СКУПКА"


def list_buyback_orders(conn: sqlite3.Connection, purpose: str | None = None) -> list[sqlite3.Row]:
    query = """SELECT buyback_orders.*, clients.name AS client_name, clients.phone AS client_phone,
                      staff.name AS staff_name
               FROM buyback_orders
               JOIN clients ON clients.id = buyback_orders.client_id
               LEFT JOIN staff ON staff.id = buyback_orders.staff_id
               WHERE 1=1"""
    params: list = []
    if purpose:
        query += " AND buyback_orders.purpose = ?"
        params.append(purpose)
    query += " ORDER BY buyback_orders.created_at DESC"
    return conn.execute(query, params).fetchall()


def get_buyback_order(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT buyback_orders.*, clients.name AS client_name, clients.phone AS client_phone,
                  staff.name AS staff_name
           FROM buyback_orders
           JOIN clients ON clients.id = buyback_orders.client_id
           LEFT JOIN staff ON staff.id = buyback_orders.staff_id
           WHERE buyback_orders.id = ?""",
        (order_id,),
    ).fetchone()


def write_buyback_photo(order_id: int, data: bytes, ext: str) -> str:
    compressed = _photos.compress_photo(data)
    if compressed is not None:
        data, ext = compressed, ".jpg"
    os.makedirs(PHOTO_DIR, exist_ok=True)
    filename = f"{order_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(PHOTO_DIR, filename), "wb") as f:
        f.write(data)
    return filename


def _get_or_create_buyback_cell(conn: sqlite3.Connection) -> int:
    """Every purpose='resale' item lands in one fixed cell — staff never
    has to pick one at buyback intake (keeps the form short); if the shop
    wants it shelved somewhere specific, that's an ordinary Перемещение
    (core.inventory.transfer_stock) afterward, same as any other product."""
    row = conn.execute("SELECT id FROM storage_cells WHERE code = ?", (_BUYBACK_CELL_CODE,)).fetchone()
    if row:
        return row["id"]
    return _inventory.create_cell(conn, _BUYBACK_CELL_CODE, None, "Автоячейка для техники, скупленной на продажу")


def create_buyback_intake(
    conn: sqlite3.Connection,
    *,
    client_name: str,
    client_phone: str,
    device_type: str,
    brand: str | None,
    model: str,
    serial_number: str | None,
    condition_note: str | None,
    purchase_price: int,
    payment_method: str,
    purpose: str,
    resale_price: int | None,
    staff_id: int,
    photo: tuple[bytes, str] | None,
) -> int:
    """One client (reused by phone, or created) + one buyback_orders row +
    a cash expense for the price paid to the client + (only if
    purpose='resale') a matching products row with qty=1 in the
    СКУПКА cell, so the item is immediately sellable through the existing
    Продажи flow. Shared by the web form and the bot's quick-intake FSM."""
    if purpose not in PURPOSES:
        raise ValueError(f"неизвестное назначение: {purpose}")
    if purpose == "resale" and not resale_price:
        raise ValueError("для «На продажу» нужна цена продажи")

    client_id = _clients.get_or_create_by_phone(conn, client_name, client_phone, source="offline")

    order_id = conn.execute(
        """INSERT INTO buyback_orders
           (client_id, device_type, brand, model, serial_number, condition_note, purchase_price, purpose, staff_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, device_type, brand, model, serial_number, condition_note, purchase_price, purpose, staff_id),
    ).lastrowid

    photo_filename = None
    if photo:
        data, ext = photo
        photo_filename = write_buyback_photo(order_id, data, ext)
        conn.execute("UPDATE buyback_orders SET photo_path = ? WHERE id = ?", (photo_filename, order_id))

    device_label = " ".join(b for b in (device_type, brand, model) if b)
    _cash.record_expense(
        conn, payment_method, purchase_price, "buyback", f"Скупка №{order_id}: {device_label}", staff_id,
        ref_type="buyback_order", ref_id=order_id,
    )

    if purpose == "resale":
        name = f"{device_label} (Б/У)".strip()
        product_id = _inventory.create_product(
            conn, name=name, sku=None, category="Скупка", unit="шт",
            is_repair_part=False, is_sellable=True, min_qty=0, price=resale_price,
        )
        if photo_filename:
            _inventory.set_product_photo(conn, product_id, photo_filename)
        cell_id = _get_or_create_buyback_cell(conn)
        _inventory.receive_stock(conn, product_id, cell_id, 1, staff_id, comment=f"Скупка №{order_id}")
        conn.execute("UPDATE buyback_orders SET product_id = ? WHERE id = ?", (product_id, order_id))

    return order_id
