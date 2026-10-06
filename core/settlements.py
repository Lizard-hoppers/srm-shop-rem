"""Взаиморасчёты с контрагентом (Заход 6). One ledger (client_ledger)
answers «кто кому должен»: every sale, покупка and movement of money
with a client writes a signed row, and the sum of his live rows is his
balance —

    > 0   «нам должны»  (sold «в долг», a заказ handed over unpaid)
    < 0   «мы должны»   (an аванс, a предоплата of a заказ not yet выдан)

A document that is paid on the spot writes both of its rows (what was
owed, what was paid) so «Сверка за период» reads as a complete statement,
not only as a list of exceptions.

Money that isn't tied to one document's total — «Принять деньги» (ПКО)
and «Выдать деньги» (РКО) — is posted here too: the ledger row is the
thing the cash rows and the journal document point at.
"""
from __future__ import annotations

import sqlite3

from core import cash as _cash
from core import documents as _documents
from core import locations as _locations
from core.accounts import money
from core.timefmt import kyiv_date_range_utc

KIND_LABELS = {
    "sale": "Продажа",
    "buyback": "Покупка у контрагента",
    "payment": "Оплата",
    "money_in": "Приём денег",
    "money_out": "Выдача денег",
}


class SettlementError(Exception):
    """A settlement that can't go ahead — message is shown to staff as-is."""


def post(
    conn: sqlite3.Connection, client_id: int, amount, kind: str, *, ref_type: str | None = None,
    ref_id: int | None = None, location_id: int | None = None, staff_id: int | None = None,
    comment: str | None = None,
) -> int | None:
    """Write one ledger row. amount > 0 — the client owes us more;
    amount < 0 — less (or we owe him). A zero amount writes nothing."""
    amount = money(amount)
    if not amount:
        return None
    return conn.execute(
        """INSERT INTO client_ledger (client_id, amount, kind, ref_type, ref_id, location_id, staff_id, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (client_id, amount, kind, ref_type, ref_id, location_id, staff_id, comment),
    ).lastrowid


def cancel_ref(conn: sqlite3.Connection, ref_type: str, ref_id: int) -> None:
    """The document was cancelled — its rows stop counting."""
    conn.execute(
        "UPDATE client_ledger SET cancelled_at = datetime('now') WHERE ref_type = ? AND ref_id = ? AND cancelled_at IS NULL",
        (ref_type, ref_id),
    )


def balance(conn: sqlite3.Connection, client_id: int):
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM client_ledger WHERE client_id = ? AND cancelled_at IS NULL",
        (client_id,),
    ).fetchone()
    return money(row["total"])


def position(value) -> dict:
    """A balance as the two numbers of the макет: «нам должны» / «мы должны»."""
    value = money(value)
    return {"balance": value, "they_owe": value if value > 0 else 0, "we_owe": -value if value < 0 else 0}


def client_position(conn: sqlite3.Connection, client_id: int) -> dict:
    return position(balance(conn, client_id))


def paid_for(conn: sqlite3.Connection, ref_type: str, ref_id: int):
    """How much the client has paid towards this document (as a positive number)."""
    row = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM client_ledger
           WHERE ref_type = ? AND ref_id = ? AND amount < 0 AND cancelled_at IS NULL""",
        (ref_type, ref_id),
    ).fetchone()
    return money(-row["total"])


def debtors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every контрагент with a non-zero balance, biggest debts to us first."""
    return conn.execute(
        """SELECT clients.id, clients.name, clients.phone, ROUND(SUM(client_ledger.amount), 2) AS balance
           FROM client_ledger JOIN clients ON clients.id = client_ledger.client_id
           WHERE client_ledger.cancelled_at IS NULL
           GROUP BY clients.id HAVING ABS(SUM(client_ledger.amount)) >= 0.01
           ORDER BY balance DESC""",
    ).fetchall()


def _source_label(row: sqlite3.Row) -> str:
    if row["ref_type"] == "sales_order":
        return _documents.label("sale", row["ref_id"])
    if row["ref_type"] == "buyback_order":
        return _documents.label("buyback", row["ref_id"])
    if row["ref_type"] == "client_order":
        return _documents.label("client_order", row["ref_id"])
    return ""


def statement(conn: sqlite3.Connection, client_id: int, date_from: str, date_to: str) -> dict:
    """«Сверка за период» (inclusive Kyiv dates): the balance going in,
    every row of the period with a running balance, the balance coming
    out. `charged` — what made the client owe us more, `paid` — less."""
    start, end = kyiv_date_range_utc(date_from, date_to)
    opening = conn.execute(
        """SELECT COALESCE(SUM(amount), 0) AS total FROM client_ledger
           WHERE client_id = ? AND cancelled_at IS NULL AND created_at < ?""",
        (client_id, start),
    ).fetchone()["total"]
    rows = conn.execute(
        """SELECT * FROM client_ledger
           WHERE client_id = ? AND cancelled_at IS NULL AND created_at >= ? AND created_at < ?
           ORDER BY created_at, id""",
        (client_id, start, end),
    ).fetchall()
    running = opening
    lines, charged, paid = [], 0, 0
    for row in rows:
        running += row["amount"]
        if row["amount"] > 0:
            charged += row["amount"]
        else:
            paid += -row["amount"]
        lines.append({
            "created_at": row["created_at"], "kind_label": KIND_LABELS.get(row["kind"], row["kind"]),
            "source": _source_label(row), "comment": row["comment"],
            "charged": money(row["amount"]) if row["amount"] > 0 else None,
            "paid": money(-row["amount"]) if row["amount"] < 0 else None,
            "balance": money(running),
        })
    return {
        "opening": position(opening), "closing": position(running), "rows": lines,
        "charged": money(charged), "paid": money(paid),
    }


def _move_money(
    conn: sqlite3.Connection, direction: str, client_id: int, payments: list[tuple[int, object, object]], *,
    staff_id: int, location_id: int | None, comment: str | None, key: str | None,
    ref: tuple[str, int] | None,
) -> int:
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()
    if not client:
        raise SettlementError("Контрагент не найден.")
    location_id = _locations.resolve(conn, location_id)
    resolved = _cash.resolve_parts(conn, location_id, payments or [])
    total = _cash.paid_uah(resolved)
    if not resolved or total <= 0:
        raise SettlementError("Укажите сумму хотя бы на одном счёте.")
    comment = (comment or "").strip() or None
    incoming = direction == "in"
    entry_id = post(
        conn, client_id, -total if incoming else total, "money_in" if incoming else "money_out",
        ref_type=ref[0] if ref else None, ref_id=ref[1] if ref else None, location_id=location_id,
        staff_id=staff_id, comment=comment,
    )
    who = client["name"] + (f" · {client['phone']}" if client["phone"] else "")
    _cash.record_payments(
        conn, "income" if incoming else "expense", resolved, "client_payment", entry_id, staff_id,
        category=None if incoming else "client_payout",
        comment=("Принято от " if incoming else "Выдано: ") + who + (f" — {comment}" if comment else ""),
    )
    _documents.register(
        conn, "cash_in" if incoming else "cash_out", staff_id=staff_id, location_id=location_id,
        client_id=client_id, ref_table="client_ledger", ref_id=entry_id,
        title=("Принято от " if incoming else "Выдано: ") + who + (f" — {comment}" if comment else ""),
        amount=total, key=key,
    )
    return entry_id


def receive_money(
    conn: sqlite3.Connection, client_id: int, payments: list[tuple[int, object, object]], *, staff_id: int,
    location_id: int | None = None, comment: str | None = None, key: str | None = None,
    ref: tuple[str, int] | None = None,
) -> int:
    """«Принять деньги» (ПКО): the client pays — onto any of the точка's
    accounts — and owes us that much less. `ref` ties it to a document
    it is a payment towards (a заказ). Returns the ledger row id."""
    return _move_money(conn, "in", client_id, payments, staff_id=staff_id, location_id=location_id,
                       comment=comment, key=key, ref=ref)


def pay_out_money(
    conn: sqlite3.Connection, client_id: int, payments: list[tuple[int, object, object]], *, staff_id: int,
    location_id: int | None = None, comment: str | None = None, key: str | None = None,
) -> int:
    """«Выдать деньги» (РКО): we hand the client money — returning an
    аванс, or lending."""
    return _move_money(conn, "out", client_id, payments, staff_id=staff_id, location_id=location_id,
                       comment=comment, key=key, ref=None)


def cancel_money(conn: sqlite3.Connection, entry_id: int) -> None:
    """Undo a «Принять/Выдать деньги»: its cash rows and its ledger row."""
    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = 'client_payment' AND ref_id = ?", (entry_id,)
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    conn.execute(
        "UPDATE client_ledger SET cancelled_at = datetime('now') WHERE id = ? AND cancelled_at IS NULL", (entry_id,)
    )
