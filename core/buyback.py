"""Покупка техники у клиентов — устройств у частных лиц, а не у
поставщиков (см. core.purchases для того случая).

С Захода 4 (06.10) основной путь — create_purchase() внизу: «один телефон
— одна покупка». Продавец по номеру телефона, до шести фото со всех
сторон, модель, IMEI, поломки, цена в любой валюте, оплата с одного или
нескольких счетов точки. Телефон сразу становится серийной единицей на
складе купившей точки: партия = IMEI + цена покупки как себестоимость.
Что с ним дальше (в ремонт, в производство, на витрину) — отдельные
документы.

create_buyback_intake() ниже — прежняя форма (24.08) с двумя ветками по
назначению, оставлена для записей, сделанных до Захода 4:

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

from core import accounts as _accounts
from core import cash as _cash
from core import clients as _clients
from core import documents as _documents
from core import settlements as _settlements
from core import locations as _locations
from core import notify as _notify
from core.storage import get_conn as _get_conn
from core import inventory as _inventory
from core import photos as _photos

PURPOSES = {"parts": "На запчасти", "resale": "На продажу"}

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "buyback_photos")

_BUYBACK_CELL_CODE = "СКУПКА"


def list_buyback_orders(
    conn: sqlite3.Connection, purpose: str | None = None, location_id: int | None = None
) -> list[sqlite3.Row]:
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
    if location_id is not None:
        query += " AND buyback_orders.location_id = ?"
        params.append(location_id)
    query += " ORDER BY buyback_orders.created_at DESC, buyback_orders.id DESC"
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


def _get_or_create_buyback_cell(conn: sqlite3.Connection, location_id: int) -> int:
    """Every purpose='resale' item lands in one fixed cell of the точка
    that bought it — staff never has to pick one at buyback intake (keeps
    the form short); if the shop wants it shelved somewhere specific,
    that's an ordinary Перемещение (core.inventory.transfer_stock)
    afterward, same as any other product. Cell codes are unique across the
    whole business, so every точка past the first gets its number in the
    code («СКУПКА-2»)."""
    code = _BUYBACK_CELL_CODE
    if location_id != _locations.default_location_id(conn):
        code = f"{_BUYBACK_CELL_CODE}-{location_id}"
    row = conn.execute("SELECT id FROM storage_cells WHERE code = ?", (code,)).fetchone()
    if row:
        return row["id"]
    return _inventory.create_cell(
        conn, code, None, "Автоячейка для техники, скупленной на продажу", location_id=location_id
    )


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
    location_id: int | None = None,
    key: str | None = None,
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

    location_id = _locations.resolve(conn, location_id)
    client_id = _clients.get_or_create_by_phone(conn, client_name, client_phone, source="offline")

    order_id = conn.execute(
        """INSERT INTO buyback_orders
           (client_id, device_type, brand, model, serial_number, condition_note, purchase_price, purpose,
            staff_id, location_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, device_type, brand, model, serial_number, condition_note, purchase_price, purpose,
         staff_id, location_id),
    ).lastrowid

    photo_filename = None
    if photo:
        data, ext = photo
        photo_filename = write_buyback_photo(order_id, data, ext)
        conn.execute("UPDATE buyback_orders SET photo_path = ? WHERE id = ?", (photo_filename, order_id))

    device_label = " ".join(b for b in (device_type, brand, model) if b)
    _cash.record_expense(
        conn, payment_method, purchase_price, "buyback", f"Скупка №{order_id}: {device_label}", staff_id,
        ref_type="buyback_order", ref_id=order_id, location_id=location_id,
    )

    if purpose == "resale":
        name = f"{device_label} (Б/У)".strip()
        product_id = _inventory.create_product(
            conn, name=name, sku=None, category="Скупка", unit="шт",
            is_repair_part=False, is_sellable=True, min_qty=0, price=resale_price, is_serial=True,
        )
        if photo_filename:
            _inventory.set_product_photo(conn, product_id, photo_filename)
        cell_id = _get_or_create_buyback_cell(conn, location_id)
        # record_movement directly, tagged with this покупка — not
        # receive_stock, which is the MANUAL «оприходование» and would
        # put a second, separate document into the journal for it.
        # One device = one партия of one unit: what we paid for it is its
        # cost, its serial/IMEI (when given) is its identity.
        batch_id = _inventory.create_batch(
            conn, product_id, source="buyback", unit_cost=purchase_price, imei=serial_number,
        )
        _inventory.record_movement(
            conn, product_id, 1, "receipt", staff_id, to_cell_id=cell_id,
            ref_type="buyback_order", ref_id=order_id, comment=f"Скупка №{order_id}", batch_id=batch_id,
        )
        conn.execute("UPDATE buyback_orders SET product_id = ? WHERE id = ?", (product_id, order_id))

    _documents.register(
        conn, "buyback", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="buyback_orders", ref_id=order_id, title=device_label, amount=purchase_price, key=key,
    )
    return order_id


# ---- Покупка телефона (Заход 4) ----

# The six sides, in the order the bot asks for them.
PHOTO_SLOTS = ("Передняя", "Задняя", "Верх", "Низ", "Правая", "Левая")
_PHONE_CATEGORY = "Телефоны Б/У"
_MIN_IMEI_LENGTH = 8


class PurchaseError(Exception):
    """A покупка that can't be проведена as entered — message is shown to staff as-is."""


def check_imei(conn: sqlite3.Connection, imei: str | None) -> str:
    """Normalized IMEI/serial, or PurchaseError: too short to identify
    anything, or already on our books (the same phone can't be bought
    while it is still in stock)."""
    normalized = _inventory.normalize_imei(imei)
    if not normalized or len(normalized) < _MIN_IMEI_LENGTH or not normalized.isalnum():
        raise PurchaseError("Проверьте IMEI — обычно это 15 цифр (наберите *#06# на телефоне).")
    if _inventory.find_unit_by_imei(conn, normalized):
        raise PurchaseError(f"Устройство с IMEI {normalized} уже числится на складе.")
    return normalized


def recent_models(conn: sqlite3.Connection, limit: int = 6) -> list[str]:
    """Model names bought most recently — the «выберите из списка» of the
    model step."""
    rows = conn.execute(
        """SELECT products.name, MAX(buyback_orders.id) AS last
           FROM buyback_orders JOIN products ON products.id = buyback_orders.product_id
           WHERE products.is_serial = 1 AND products.active = 1
           GROUP BY products.id ORDER BY last DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [row["name"] for row in rows]


def _model_product(conn: sqlite3.Connection, model: str) -> int:
    """The serial SKU for this model — an existing one with the same name
    (case-insensitively), or a new one. Every phone of that model, bought
    or received from a supplier, is a unit (партия + IMEI) of this card."""
    row = conn.execute(
        "SELECT id FROM products WHERE active = 1 AND is_serial = 1 AND LOWER(name) = LOWER(?) ORDER BY id LIMIT 1",
        (model,),
    ).fetchone()
    if row:
        return row["id"]
    return _inventory.create_product(
        conn, name=model, sku=None, category=_PHONE_CATEGORY, unit="шт",
        is_repair_part=False, is_sellable=True, min_qty=0, price=None, is_serial=True,
    )


def create_purchase(
    conn: sqlite3.Connection,
    *,
    seller_phone: str,
    model: str,
    imei: str,
    comment: str | None,
    price,
    staff_id: int,
    location_id: int | None = None,
    currency: str = "UAH",
    rate: float | None = None,
    payments: list[tuple[int, object, object]] | None = None,
    photos: list[tuple[bytes, str, str | None]] | None = None,
    seller_name: str | None = None,
    key: str | None = None,
) -> int:
    """One phone, one покупка. Everything that can refuse — the seller's
    number, the IMEI, the price, the payment split — is checked before the
    first write (PurchaseError / core.cash.PaymentError).

    `price` is in `currency`; `rate` (гривен за единицу) is required for a
    foreign one. `payments` is [(account_id, amount, rate), …] over the
    точка's accounts and must add up to the price's гривня value — None
    means all of it from the точка's наличные. `photos` is
    [(bytes, ext, label), …], at most len(PHOTO_SLOTS)."""
    location_id = _locations.resolve(conn, location_id)
    phone = _clients.normalize_phone(seller_phone or "")
    if not phone:
        raise PurchaseError("Укажите номер телефона продавца — например 0501234567.")
    model = " ".join((model or "").split())
    if not model:
        raise PurchaseError("Укажите модель телефона.")
    imei = check_imei(conn, imei)
    amount = _accounts.parse_amount(price)
    if not amount:
        raise PurchaseError("Укажите цену покупки.")
    currency = (currency or "UAH").upper()
    if currency not in _accounts.CURRENCIES:
        raise PurchaseError("Выберите валюту цены.")
    if currency == _accounts.BASE_CURRENCY:
        rate_value = 1.0
    else:
        rate_value = _accounts.parse_amount(rate)
        if not rate_value:
            raise PurchaseError(f"Укажите курс: сколько гривен за 1 {currency}.")
    total_uah = _accounts.money(float(amount) * float(rate_value))
    resolved = _cash.resolve_payments(conn, location_id, total_uah, payments, "cash")
    photos = list(photos or [])[: len(PHOTO_SLOTS)]

    existing = _clients.get_by_phone(conn, phone)
    client_id = existing["id"] if existing else _clients.create_client(
        conn, name=(seller_name or "").strip() or phone, phone=phone, source="offline",
    )
    product_id = _model_product(conn, model)

    order_id = conn.execute(
        """INSERT INTO buyback_orders
           (client_id, device_type, model, serial_number, imei, condition_note, purchase_price, currency, rate,
            purchase_price_uah, purpose, product_id, staff_id, location_id)
           VALUES (?, 'Телефон', ?, ?, ?, ?, ?, ?, ?, ?, 'resale', ?, ?, ?)""",
        (client_id, model, imei, imei, (comment or "").strip() or None, amount, currency, rate_value,
         total_uah, product_id, staff_id, location_id),
    ).lastrowid

    first_photo = None
    for position, (data, ext, label) in enumerate(photos):
        filename = write_buyback_photo(order_id, data, ext)
        first_photo = first_photo or filename
        conn.execute(
            "INSERT INTO buyback_photos (order_id, position, label, path) VALUES (?, ?, ?, ?)",
            (order_id, position, label or (PHOTO_SLOTS[position] if position < len(PHOTO_SLOTS) else None), filename),
        )
    if first_photo:
        conn.execute("UPDATE buyback_orders SET photo_path = ? WHERE id = ?", (first_photo, order_id))
        if not _inventory.get_product(conn, product_id)["photo_path"]:
            _inventory.set_product_photo(conn, product_id, first_photo)

    # The phone is ours now: a партия of one unit — its IMEI, what we paid
    # for it as its cost — in the buying точка's «СКУПКА» cell.
    batch_id = _inventory.create_batch(
        conn, product_id, source="buyback", unit_cost=amount, currency=currency, rate=rate_value, imei=imei,
    )
    _inventory.record_movement(
        conn, product_id, 1, "receipt", staff_id, to_cell_id=_get_or_create_buyback_cell(conn, location_id),
        ref_type="buyback_order", ref_id=order_id, comment=f"Покупка №{order_id}", batch_id=batch_id,
    )
    conn.execute("UPDATE buyback_orders SET batch_id = ? WHERE id = ?", (batch_id, order_id))

    _cash.record_payments(
        conn, "expense", resolved, "buyback_order", order_id, staff_id,
        category="buyback", comment=f"Покупка №{order_id}: {model}",
    )
    # Взаиморасчёты: we owed the seller the price and paid it on the spot.
    _settlements.post(conn, client_id, -total_uah, "buyback", ref_type="buyback_order", ref_id=order_id,
                      location_id=location_id, staff_id=staff_id, comment=f"{_documents.label('buyback', order_id)}: {model}")
    _settlements.post(conn, client_id, total_uah, "payment", ref_type="buyback_order", ref_id=order_id,
                      location_id=location_id, staff_id=staff_id, comment=f"Оплата {_documents.label('buyback', order_id)}")
    _documents.register(
        conn, "buyback", staff_id=staff_id, location_id=location_id, client_id=client_id,
        ref_table="buyback_orders", ref_id=order_id, title=model, amount=total_uah, key=key,
    )
    return order_id


def post_card_to_group(store, order_id: int) -> None:
    """The покупка's card into the точка's staff group, in its «Скупка»
    topic — only if that topic is configured (locations.buyback_topic_id).
    Photos as one album with the card as caption; plain text if there are
    none. Best effort; blocking httpx, so callers run it off the request /
    event loop (a FastAPI BackgroundTask, asyncio.to_thread in the bot)."""
    if not store.staff_group_chat_id or not store.buyback_topic_id:
        return
    with _get_conn(store.db_path) as conn:
        text = card_text(conn, order_id)
        photos = read_photos(conn, order_id)
    if photos:
        _notify.send_album(store.staff_group_chat_id, photos, text, message_thread_id=store.buyback_topic_id)
    else:
        _notify.notify_staff_group(
            text, message_thread_id=store.buyback_topic_id, staff_group_chat_id=store.staff_group_chat_id,
        )


def get_photos(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM buyback_photos WHERE order_id = ? ORDER BY position", (order_id,)
    ).fetchall()


def read_photos(conn: sqlite3.Connection, order_id: int) -> list[tuple[bytes, str]]:
    """The покупка's photos off disk, in order — for posting its card."""
    result = []
    for photo in get_photos(conn, order_id):
        path = os.path.join(PHOTO_DIR, photo["path"])
        if os.path.exists(path):
            with open(path, "rb") as f:
                result.append((f.read(), photo["path"]))
    return result


def card_text(conn: sqlite3.Connection, order_id: int) -> str:
    """The card of a покупка as it appears in Telegram (HTML): «ПК-001 ·
    iPhone 13 · 128 GB · Black», контрагент, IMEI, поломки, «Куплен: 5 000
    грн · Мастерская», and how it was paid."""
    import html

    order = get_buyback_order(conn, order_id)
    location = _locations.get_location(conn, order["location_id"]) if order["location_id"] else None
    label = _documents.label("buyback", order_id)
    device = " ".join(x for x in (order["brand"], order["model"]) if x) or order["device_type"]
    price = _accounts.money(order["purchase_price"])
    currency = order["currency"] or "UAH"
    bought = f"{price} {_accounts.currency_label(currency)}"
    if currency != "UAH" and order["purchase_price_uah"] is not None:
        bought += f" = {_accounts.money(order['purchase_price_uah'])} грн"
    lines = [
        f"🛒 <b>{label} · {html.escape(device)}</b>",
        f"Контрагент: {html.escape(order['client_phone'] or order['client_name'])}",
    ]
    if order["imei"] or order["serial_number"]:
        lines.append(f"IMEI: <code>{html.escape(order['imei'] or order['serial_number'])}</code>")
    if order["condition_note"]:
        lines.append(f"Поломки: {html.escape(order['condition_note'])}")
    lines.append(f"Куплен: {bought}" + (f" · {html.escape(location['name'])}" if location else ""))
    paid = _cash.payments_for(conn, "buyback_order", order_id)
    if paid:
        parts = [
            f"{html.escape(p['account_name'] or '—')} — {_accounts.money(p['amount'])} {_accounts.currency_label(p['currency'] or 'UAH')}"
            for p in paid if not p["cancelled_at"]
        ]
        if parts:
            lines.append("Оплата: " + "; ".join(parts))
    return "\n".join(lines)
