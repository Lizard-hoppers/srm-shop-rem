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

METHODS = {"cash": "Наличные", "card": "Карта/перевод"}

EXPENSE_CATEGORIES = {
    "rent": "Аренда",
    "salary": "Зарплата",
    "supplies": "Закупка",
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
) -> int:
    if amount <= 0:
        raise ValueError("сумма должна быть положительной")
    return conn.execute(
        """INSERT INTO cash_transactions (kind, method, amount, ref_type, ref_id, comment, staff_id)
           VALUES ('income', ?, ?, ?, ?, ?, ?)""",
        (method, amount, ref_type, ref_id, comment, staff_id),
    ).lastrowid


def record_expense(
    conn: sqlite3.Connection, method: str, amount: int, category: str, comment: str | None, staff_id: int
) -> int:
    if amount <= 0:
        raise ValueError("сумма должна быть положительной")
    return conn.execute(
        """INSERT INTO cash_transactions (kind, method, amount, category, comment, staff_id)
           VALUES ('expense', ?, ?, ?, ?, ?)""",
        (method, amount, category, comment, staff_id),
    ).lastrowid


def record_adjustment(conn: sqlite3.Connection, amount: int, comment: str | None, staff_id: int) -> int:
    """«Внести/изъять наличку» — a manual cash-only correction (float
    top-up, end-of-day withdrawal to the safe, a recount fix). Positive
    amount = внесли, negative = изъяли; always method='cash', since this
    exists purely to true up the physical drawer against reality."""
    if amount == 0:
        raise ValueError("сумма не может быть нулевой")
    kind = "income" if amount > 0 else "expense"
    return conn.execute(
        """INSERT INTO cash_transactions (kind, method, amount, category, comment, staff_id)
           VALUES (?, 'cash', ?, 'adjustment', ?, ?)""",
        (kind, abs(amount), comment, staff_id),
    ).lastrowid


def cash_balance(conn: sqlite3.Connection) -> int:
    """How much cash is physically in the drawer right now — every
    method='cash' income minus every method='cash' expense, all-time."""
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END), 0) AS balance
           FROM cash_transactions WHERE method = 'cash'"""
    ).fetchone()
    return row["balance"]


def period_summary(conn: sqlite3.Connection, utc_start: str, utc_end: str) -> dict:
    """Totals for one period (see core.timefmt.kyiv_date_range_utc for the
    utc_start/utc_end bounds) — the касса page's «за сегодня» / «за
    период» card."""
    row = conn.execute(
        """SELECT
             COALESCE(SUM(CASE WHEN kind='income' AND method='cash' THEN amount ELSE 0 END), 0) AS income_cash,
             COALESCE(SUM(CASE WHEN kind='income' AND method='card' THEN amount ELSE 0 END), 0) AS income_card,
             COALESCE(SUM(CASE WHEN kind='expense' AND method='cash' THEN amount ELSE 0 END), 0) AS expense_cash,
             COALESCE(SUM(CASE WHEN kind='expense' AND method='card' THEN amount ELSE 0 END), 0) AS expense_card
           FROM cash_transactions
           WHERE created_at >= ? AND created_at < ?""",
        (utc_start, utc_end),
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


def list_transactions(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT cash_transactions.*, staff.name AS staff_name
           FROM cash_transactions
           LEFT JOIN staff ON staff.id = cash_transactions.staff_id
           ORDER BY cash_transactions.created_at DESC, cash_transactions.id DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
