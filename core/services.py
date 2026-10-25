"""Продажа услуг (25.10, Павел): «🧾 Услуга» в боте и страница «Услуги» в
Mini App — что за услуга (руками) и сумма. Деньги — на счёт точки, документ
«УС», вся сумма — прибыль (у услуги нет закупки и доли мастера).

Каждая проданная услуга запоминается в справочнике точки (services): бот
потом предлагает их кнопками быстрого выбора — сначала частые — и подставляет
прошлую цену."""
from __future__ import annotations

import sqlite3

from core import accounts as _accounts
from core import cash as _cash
from core import documents as _documents
from core import masters as _masters
from core.accounts import money

QUICK_LIMIT = 8
MAX_NAME = 80


class ServiceError(Exception):
    """Провести нельзя — текст для сотрудника."""


def clean_name(name: str | None) -> str:
    name = " ".join(str(name or "").split())
    if not name:
        raise ServiceError("Напишите, что за услуга.")
    return name[:MAX_NAME]


def find(conn: sqlite3.Connection, location_id: int, name: str) -> sqlite3.Row | None:
    """By name, whatever the letter case. In Python: SQLite's NOCASE folds
    only Latin letters, and «Наклейка» / «наклейка» must be one service."""
    wanted = " ".join(str(name or "").split()).casefold()
    return next((row for row in conn.execute("SELECT * FROM services WHERE location_id = ?", (location_id,))
                 if row["name"].casefold() == wanted), None)


def get(conn: sqlite3.Connection, service_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM services WHERE id = ?", (service_id,)).fetchone()


def quick_list(conn: sqlite3.Connection, location_id: int, limit: int = QUICK_LIMIT) -> list[sqlite3.Row]:
    """Для кнопок быстрого выбора: сначала те, что продают чаще, потом недавние."""
    return conn.execute(
        """SELECT * FROM services WHERE location_id = ? AND active = 1
           ORDER BY uses DESC, last_used_at DESC, id DESC LIMIT ?""",
        (location_id, limit),
    ).fetchall()


def list_all(conn: sqlite3.Connection, location_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM services WHERE location_id = ? AND active = 1 ORDER BY uses DESC, name COLLATE NOCASE",
        (location_id,),
    ).fetchall()


def _remember(conn: sqlite3.Connection, location_id: int, name: str, amount) -> int:
    known = find(conn, location_id, name)
    if known:
        conn.execute(
            """UPDATE services SET uses = uses + 1, price = ?, last_used_at = datetime('now'), active = 1
               WHERE id = ?""",
            (amount, known["id"]),
        )
        return known["id"]
    return conn.execute(
        "INSERT INTO services (location_id, name, price, uses, last_used_at) VALUES (?, ?, ?, 1, datetime('now'))",
        (location_id, name, amount),
    ).lastrowid


def update(conn: sqlite3.Connection, service_id: int, name: str, price) -> None:
    """Справочник в Mini App: поправить название и цену по умолчанию."""
    service = get(conn, service_id)
    if not service:
        raise ServiceError("Услуга не найдена.")
    name = clean_name(name)
    twin = find(conn, service["location_id"], name)
    if twin and twin["id"] != service_id:
        raise ServiceError(f"Услуга «{name}» уже есть.")
    price = money(price) if price not in (None, "") else None
    if price is not None and price < 0:
        raise ServiceError("Цена не может быть меньше нуля.")
    conn.execute("UPDATE services SET name = ?, price = ? WHERE id = ?", (name, price, service_id))


def hide(conn: sqlite3.Connection, service_id: int) -> None:
    """Убрать из быстрого выбора (проданные раньше остаются в истории)."""
    conn.execute("UPDATE services SET active = 0 WHERE id = ?", (service_id,))


def sell(
    conn: sqlite3.Connection, location_id: int, name: str, amount, account_id: int, staff_id: int | None,
    *, key: str | None = None, comment: str | None = None,
) -> int:
    """Провести продажу услуги: деньги на счёт, документ «УС», услуга — в
    справочник. Возвращает id продажи (= номер документа)."""
    name = clean_name(name)
    try:
        amount = money(amount)
    except (TypeError, ValueError) as exc:
        raise ServiceError("Сумма — числом, например 300.") from exc
    if amount <= 0:
        raise ServiceError("Сумма должна быть больше нуля.")
    account = _accounts.get_account(conn, account_id)
    if not account or not account["active"] or account["location_id"] != location_id:
        raise ServiceError("Этого счёта нет у точки.")
    if account["currency"] != _accounts.BASE_CURRENCY:
        raise ServiceError("Услуга оплачивается в гривне — выберите гривневый счёт.")
    service_id = _remember(conn, location_id, name, amount)
    sale_id = conn.execute(
        """INSERT INTO service_sales (location_id, service_id, name, amount, account_id, staff_id, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (location_id, service_id, name, amount, account_id, staff_id, comment),
    ).lastrowid
    _cash.post(conn, "income", account, amount, staff_id=staff_id, ref_type="service", ref_id=sale_id,
               comment=f"Услуга: {name}")
    _documents.register(conn, "service", staff_id=staff_id, location_id=location_id, ref_table="service_sales",
                        ref_id=sale_id, title=name, amount=amount, key=key)
    _documents.set_profit(conn, "service", sale_id, amount)
    return sale_id


def cancel(conn: sqlite3.Connection, sale_id: int) -> None:
    """Деньги со счёта — обратно, начисление сотруднику (если записали) —
    снято; документ помечает отменённым core.doc_cancel."""
    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = 'service' AND ref_id = ? AND cancelled_at IS NULL", (sale_id,),
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    _masters.cancel_accruals(conn, "service", sale_id)
    conn.execute("UPDATE service_sales SET status = 'cancelled' WHERE id = ?", (sale_id,))


def recent(conn: sqlite3.Connection, location_id: int, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT service_sales.*, money_accounts.name AS account_name, staff.name AS staff_name
           FROM service_sales
           LEFT JOIN money_accounts ON money_accounts.id = service_sales.account_id
           LEFT JOIN staff ON staff.id = service_sales.staff_id
           WHERE service_sales.location_id = ? ORDER BY service_sales.id DESC LIMIT ?""",
        (location_id, limit),
    ).fetchall()


def get_sale(conn: sqlite3.Connection, sale_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM service_sales WHERE id = ?", (sale_id,)).fetchone()
