"""Касса — a single append-only ledger of money in and out (21.08).

Deliberately no shift open/close ritual: cash-on-hand is just the running
signed sum of every method='cash' row (cash_balance()), so nobody has to
remember to "open the register" before a sale goes through. Card/transfer
payments count toward revenue reporting but never touch that balance —
Павел confirmed only cash needs to be physically reconciled.

Income rows are written by core.sales.create_sale() and
webapp.routers.repairs.status_view() (when a repair is marked "Выдан"
with a price on it) — this module itself never decides when money changed
hands, only records it. Expense/adjustment rows are the manual entry
points a storekeeper/admin/owner uses directly from the Касса page.
"""
from __future__ import annotations

import sqlite3

from core import documents as _documents
from core import locations as _locations

METHODS = {"cash": "Наличные", "card": "Карта/перевод"}

EXPENSE_CATEGORIES = {
    "rent": "Аренда",
    "salary": "Зарплата",
    "supplies": "Закупка",
    "buyback": "Скупка техники",
    "other": "Прочее",
}


def record_income(
    conn: sqlite3.Connection,
    method: str,
    amount: int,
    ref_type: str,
    ref_id: int,
    staff_id: int,
    comment: str | None = None,
    *,
    location_id: int | None = None,
) -> int:
    if amount <= 0:
        raise ValueError("сумма должна быть положительной")
    return conn.execute(
        """INSERT INTO cash_transactions (kind, method, amount, ref_type, ref_id, comment, staff_id, location_id)
           VALUES ('income', ?, ?, ?, ?, ?, ?, ?)""",
        (method, amount, ref_type, ref_id, comment, staff_id, _locations.resolve(conn, location_id)),
    ).lastrowid


def record_expense(
    conn: sqlite3.Connection, method: str, amount: int, category: str, comment: str | None, staff_id: int,
    *, ref_type: str | None = None, ref_id: int | None = None, location_id: int | None = None,
    key: str | None = None,
) -> int:
    """An expense typed in by hand (no ref_type) is its own document — РКО
    in the journal. One that pays for something else (a покупка's payout,
    ref_type set) belongs to that document and gets none of its own."""
    if amount <= 0:
        raise ValueError("сумма должна быть положительной")
    location_id = _locations.resolve(conn, location_id)
    transaction_id = conn.execute(
        """INSERT INTO cash_transactions
           (kind, method, amount, category, ref_type, ref_id, comment, staff_id, location_id)
           VALUES ('expense', ?, ?, ?, ?, ?, ?, ?, ?)""",
        (method, amount, category, ref_type, ref_id, comment, staff_id, location_id),
    ).lastrowid
    if ref_type is None:
        _documents.register(
            conn, "cash_out", staff_id=staff_id, location_id=location_id, ref_table="cash_transactions",
            ref_id=transaction_id, title=comment or EXPENSE_CATEGORIES.get(category, category),
            amount=-amount, key=key,
        )
    return transaction_id


def record_adjustment(
    conn: sqlite3.Connection, amount: int, comment: str | None, staff_id: int,
    *, location_id: int | None = None, key: str | None = None,
) -> int:
    """«Внести/изъять наличку» — a manual cash-only correction (float
    top-up, end-of-day withdrawal to the safe, a recount fix). Positive
    amount = внесли, negative = изъяли; always method='cash', since this
    exists purely to true up the physical drawer against reality. Always a
    document of its own (КР) — «баланс нельзя исправлять вручную»."""
    if amount == 0:
        raise ValueError("сумма не может быть нулевой")
    kind = "income" if amount > 0 else "expense"
    location_id = _locations.resolve(conn, location_id)
    transaction_id = conn.execute(
        """INSERT INTO cash_transactions (kind, method, amount, category, comment, staff_id, location_id)
           VALUES (?, 'cash', ?, 'adjustment', ?, ?, ?)""",
        (kind, abs(amount), comment, staff_id, location_id),
    ).lastrowid
    _documents.register(
        conn, "cash_adjust", staff_id=staff_id, location_id=location_id, ref_table="cash_transactions",
        ref_id=transaction_id, title=comment, amount=amount, key=key,
    )
    return transaction_id


def cancel_transaction(conn: sqlite3.Connection, transaction_id: int) -> None:
    """Take a row out of every balance and total without deleting it —
    it stays in the feed, marked отменено (see core.doc_cancel)."""
    conn.execute(
        "UPDATE cash_transactions SET cancelled_at = datetime('now') WHERE id = ? AND cancelled_at IS NULL",
        (transaction_id,),
    )


def _scope(location_id: int | None) -> tuple[str, list]:
    """Live rows of one точка (every точка when None) — cancelled rows
    never count toward anything."""
    if location_id is None:
        return " AND cancelled_at IS NULL", []
    return " AND cancelled_at IS NULL AND location_id = ?", [location_id]


def cash_balance(conn: sqlite3.Connection, location_id: int | None = None) -> int:
    """How much cash is physically in the drawer right now — every
    method='cash' income minus every method='cash' expense, all-time."""
    clause, params = _scope(location_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END), 0) AS balance
            FROM cash_transactions WHERE method = 'cash'{clause}""",
        params,
    ).fetchone()
    return row["balance"]


def period_summary(conn: sqlite3.Connection, utc_start: str, utc_end: str, location_id: int | None = None) -> dict:
    """Totals for one period (see core.timefmt.kyiv_date_range_utc for the
    utc_start/utc_end bounds) — the касса page's «за сегодня» / «за
    период» card."""
    clause, params = _scope(location_id)
    row = conn.execute(
        f"""SELECT
             COALESCE(SUM(CASE WHEN kind='income' AND method='cash' THEN amount ELSE 0 END), 0) AS income_cash,
             COALESCE(SUM(CASE WHEN kind='income' AND method='card' THEN amount ELSE 0 END), 0) AS income_card,
             COALESCE(SUM(CASE WHEN kind='expense' AND method='cash' THEN amount ELSE 0 END), 0) AS expense_cash,
             COALESCE(SUM(CASE WHEN kind='expense' AND method='card' THEN amount ELSE 0 END), 0) AS expense_card
           FROM cash_transactions
           WHERE created_at >= ? AND created_at < ?{clause}""",
        [utc_start, utc_end, *params],
    ).fetchone()
    income_total = row["income_cash"] + row["income_card"]
    expense_total = row["expense_cash"] + row["expense_card"]
    return {
        "income_cash": row["income_cash"],
        "income_card": row["income_card"],
        "income_total": income_total,
        "expense_cash": row["expense_cash"],
        "expense_card": row["expense_card"],
        "expense_total": expense_total,
        "net": income_total - expense_total,
    }


def list_transactions(conn: sqlite3.Connection, limit: int = 100, location_id: int | None = None) -> list[sqlite3.Row]:
    """Newest first, cancelled ones included (the page marks them) —
    nothing проведённое ever disappears from the feed."""
    where, params = ("WHERE cash_transactions.location_id = ?", [location_id]) if location_id is not None else ("", [])
    return conn.execute(
        f"""SELECT cash_transactions.*, staff.name AS staff_name
            FROM cash_transactions
            LEFT JOIN staff ON staff.id = cash_transactions.staff_id
            {where}
            ORDER BY cash_transactions.created_at DESC, cash_transactions.id DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
