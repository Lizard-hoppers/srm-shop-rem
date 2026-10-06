"""Касса — the append-only ledger of money in and out, one row per movement
on one денежный счёт (core.accounts).

Since Заход 2 (06.10) a точка has several independent balances rather than
one drawer, so every row names its account; `amount` is in that account's
currency and `amount_uah` is its гривня value at the rate of the operation
itself. Rows are never edited or deleted: a mistake is cancelled
(cancelled_at, see core.doc_cancel) and stops counting everywhere while
staying in the feed.

Who writes here: a sale and a repair's выдача (income, split across any
number of accounts — record_payments), a покупка's payout, the manual
expense / «внести-изъять» from the Касса page and the bot, and
core.money_ops (обмен валют, перемещение денег). This module never decides
when money changed hands, only records it.

`method` ('cash'/'card') is what callers passed before accounts existed.
It still works — it picks the точка's default гривня account of that sort
(core.accounts.default_account) — so the one-tap paths («наличные» /
«карта») and every older caller behave exactly as before.
"""
from __future__ import annotations

import sqlite3

from core import accounts as _accounts
from core import documents as _documents
from core import locations as _locations
from core.accounts import money

METHODS = {"cash": "Наличные", "card": "Карта/перевод"}

EXPENSE_CATEGORIES = {
    "rent": "Аренда",
    "salary": "Зарплата",
    "supplies": "Закупка",
    "buyback": "Скупка техники",
    "other": "Прочее",
}

# Categories only the system writes (never offered in the «Расход» menu):
# money that left the касса through a settlement, not a business expense.
SETTLEMENT_CATEGORIES = {
    "master_payout": "Выплата мастеру",
    "client_payout": "Выдача денег контрагенту",
}

# How far the гривня value of a split payment may be from the document's
# total and still count as «оплачено полностью» — a foreign-currency part
# rarely multiplies out to a whole гривня.
PAYMENT_TOLERANCE_UAH = 1


class PaymentError(Exception):
    """A payment that can't be taken as entered — message is shown to staff as-is."""


def _account_for(conn: sqlite3.Connection, location_id: int, method: str, account_id: int | None) -> sqlite3.Row:
    if account_id:
        account = _accounts.get_account(conn, account_id)
        if not account:
            raise PaymentError("Счёт не найден.")
        return account
    return _accounts.default_account(conn, location_id, method if method in METHODS else "cash")


def _uah_value(account: sqlite3.Row, amount, rate) -> tuple[float | int | None, float | None]:
    """(amount_uah, rate) for a row on this account. A гривня account is
    worth itself; a foreign one needs the rate of the operation — without
    one its гривня value is simply unknown (None), which is allowed only
    for corrections, never for a payment."""
    if account["currency"] == _accounts.BASE_CURRENCY:
        return money(amount), 1
    if rate:
        return money(float(amount) * float(rate)), float(rate)
    return None, None


def post(
    conn: sqlite3.Connection,
    kind: str,
    account: sqlite3.Row,
    amount,
    *,
    staff_id: int | None,
    category: str | None = None,
    ref_type: str | None = None,
    ref_id: int | None = None,
    comment: str | None = None,
    rate: float | None = None,
) -> int:
    """The one INSERT every money movement goes through."""
    amount = money(amount)
    if amount <= 0:
        raise ValueError("сумма должна быть положительной")
    amount_uah, rate = _uah_value(account, amount, rate)
    return conn.execute(
        """INSERT INTO cash_transactions
           (kind, method, amount, category, ref_type, ref_id, comment, staff_id, location_id,
            account_id, amount_uah, rate)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, _accounts.legacy_method(account["kind"]), amount, category, ref_type, ref_id, comment, staff_id,
         account["location_id"], account["id"], amount_uah, rate),
    ).lastrowid


def record_income(
    conn: sqlite3.Connection,
    method: str,
    amount,
    ref_type: str,
    ref_id: int,
    staff_id: int,
    comment: str | None = None,
    *,
    location_id: int | None = None,
    account_id: int | None = None,
    rate: float | None = None,
) -> int:
    account = _account_for(conn, _locations.resolve(conn, location_id), method, account_id)
    return post(conn, "income", account, amount, staff_id=staff_id, ref_type=ref_type, ref_id=ref_id,
                comment=comment, rate=rate)


def record_expense(
    conn: sqlite3.Connection, method: str, amount, category: str, comment: str | None, staff_id: int,
    *, ref_type: str | None = None, ref_id: int | None = None, location_id: int | None = None,
    key: str | None = None, account_id: int | None = None, rate: float | None = None,
) -> int:
    """An expense typed in by hand (no ref_type) is its own document — РКО
    in the journal. One that pays for something else (a покупка's payout,
    ref_type set) belongs to that document and gets none of its own."""
    account = _account_for(conn, _locations.resolve(conn, location_id), method, account_id)
    transaction_id = post(
        conn, "expense", account, amount, staff_id=staff_id, category=category, ref_type=ref_type, ref_id=ref_id,
        comment=comment, rate=rate,
    )
    if ref_type is None:
        row = conn.execute("SELECT amount_uah FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
        _documents.register(
            conn, "cash_out", staff_id=staff_id, location_id=account["location_id"], ref_table="cash_transactions",
            ref_id=transaction_id, title=_document_title(account, amount, comment or EXPENSE_CATEGORIES.get(category, category)),
            amount=-row["amount_uah"] if row["amount_uah"] is not None else None, key=key,
        )
    return transaction_id


def record_adjustment(
    conn: sqlite3.Connection, amount, comment: str | None, staff_id: int,
    *, location_id: int | None = None, key: str | None = None, account_id: int | None = None,
) -> int:
    """«Внести/изъять» — a correction of one account's balance against
    reality (float top-up, a recount fix). Positive amount = внесли,
    negative = изъяли. Always a document of its own (КР): «баланс нельзя
    исправлять вручную, корректировка — отдельный документ с причиной и
    пользователем». Defaults to the точка's наличные гривня."""
    amount = money(amount)
    if amount == 0:
        raise ValueError("сумма не может быть нулевой")
    account = _account_for(conn, _locations.resolve(conn, location_id), "cash", account_id)
    transaction_id = post(
        conn, "income" if amount > 0 else "expense", account, abs(amount), staff_id=staff_id,
        category="adjustment", comment=comment,
    )
    row = conn.execute("SELECT amount_uah FROM cash_transactions WHERE id = ?", (transaction_id,)).fetchone()
    signed_uah = None if row["amount_uah"] is None else (row["amount_uah"] if amount > 0 else -row["amount_uah"])
    _documents.register(
        conn, "cash_adjust", staff_id=staff_id, location_id=account["location_id"], ref_table="cash_transactions",
        ref_id=transaction_id, title=_document_title(account, amount, comment), amount=signed_uah, key=key,
    )
    return transaction_id


def _document_title(account: sqlite3.Row, amount, text: str | None) -> str:
    """«Аренда» for the plain гривня-cash case (as before accounts); the
    account and native amount spelled out for anything else, since the
    document's own amount column is always гривня."""
    is_plain = account["kind"] == "cash" and account["currency"] == _accounts.BASE_CURRENCY
    if is_plain:
        return text or ""
    native = f"{account['name']}: {money(abs(amount))} {_accounts.currency_label(account['currency'])}"
    return f"{text} · {native}" if text else native


def cancel_transaction(conn: sqlite3.Connection, transaction_id: int) -> None:
    """Take a row out of every balance and total without deleting it —
    it stays in the feed, marked отменено (see core.doc_cancel)."""
    conn.execute(
        "UPDATE cash_transactions SET cancelled_at = datetime('now') WHERE id = ? AND cancelled_at IS NULL",
        (transaction_id,),
    )


# ---- payments of a document, split across accounts ----

def resolve_payments(
    conn: sqlite3.Connection,
    location_id: int,
    total_uah,
    payments: list[tuple[int, object, object]] | None,
    legacy_method: str = "cash",
    allow_partial: bool = False,
) -> list[dict]:
    """Turn «how the client paid» into rows ready to post, or raise
    PaymentError. `payments` is a list of (account_id, amount, rate) — rate
    only matters for a foreign-currency account. Nothing given means the
    whole total onto the точка's default account for legacy_method (the
    one-tap «наличные»/«карта» path). Otherwise every account must be an
    active one of THIS точка, and the parts' гривня value must add up to
    the total. allow_partial (a sale «в долг», a заказ handed over with
    part of it unpaid): less than the total is fine — an empty list then
    means «nothing paid now», not the default account; more is still an
    error."""
    total_uah = money(total_uah)
    if payments is None or (not payments and not allow_partial):
        account = _accounts.default_account(conn, location_id, legacy_method if legacy_method in METHODS else "cash")
        return [{"account": account, "amount": total_uah, "rate": 1, "amount_uah": total_uah}]

    resolved = resolve_parts(conn, location_id, payments)
    paid = money(sum(p["amount_uah"] for p in resolved))
    if allow_partial and paid < total_uah:
        return resolved
    if abs(paid - total_uah) > PAYMENT_TOLERANCE_UAH:
        diff = money(total_uah - paid)
        hint = f"не хватает {diff} грн" if diff > 0 else f"лишние {abs(diff)} грн"
        raise PaymentError(f"Оплата не сходится с суммой: к оплате {total_uah} грн, внесено {paid} грн ({hint}).")
    return resolved


def resolve_parts(conn: sqlite3.Connection, location_id: int, payments: list[tuple[int, object, object]]) -> list[dict]:
    """The parts of a payment on their own, each checked (an active
    account of THIS точка, a readable amount, a rate for foreign
    currency) — for money that isn't measured against a document's total:
    «Принять деньги», «Выдать деньги», выплата мастеру."""
    resolved = []
    for account_id, amount, rate in payments:
        account = _accounts.get_account(conn, account_id)
        if not account or not account["active"] or account["location_id"] != location_id:
            raise PaymentError("Выбран счёт, которого нет у этой точки.")
        amount = _accounts.parse_amount(amount)
        if not amount:
            raise PaymentError(f"Проверьте сумму на счёте «{account['name']}».")
        if account["currency"] == _accounts.BASE_CURRENCY:
            rate_value, amount_uah = 1, amount
        else:
            rate_value = _accounts.parse_amount(rate)
            if not rate_value:
                raise PaymentError(
                    f"Укажите курс для счёта «{account['name']}» — сколько гривен за 1 {account['currency']}."
                )
            amount_uah = money(float(amount) * float(rate_value))
        resolved.append({"account": account, "amount": amount, "rate": rate_value, "amount_uah": amount_uah})
    return resolved


def paid_uah(resolved: list[dict]):
    return money(sum(p["amount_uah"] for p in resolved))


def record_payments(
    conn: sqlite3.Connection, kind: str, resolved: list[dict], ref_type: str, ref_id: int, staff_id: int,
    *, category: str | None = None, comment: str | None = None,
) -> None:
    """Post a document's resolved payments — income for a sale/repair,
    expense for a покупка."""
    for payment in resolved:
        post(
            conn, kind, payment["account"], payment["amount"], staff_id=staff_id, category=category,
            ref_type=ref_type, ref_id=ref_id, comment=comment,
            rate=payment["rate"] if payment["account"]["currency"] != _accounts.BASE_CURRENCY else None,
        )


def payment_method_label(resolved: list[dict]) -> str:
    """What goes into a document's own payment_method column: the one
    account's kind ('cash', 'card', 'fop', 'crypto') or 'mixed'."""
    kinds = {p["account"]["kind"] for p in resolved}
    return kinds.pop() if len(kinds) == 1 else "mixed"


def payments_for(conn: sqlite3.Connection, ref_type: str, ref_id: int) -> list[sqlite3.Row]:
    """How a document was actually paid, account by account (cancelled
    rows included, flagged) — the «Оплата» block on its page."""
    return conn.execute(
        """SELECT cash_transactions.*, money_accounts.name AS account_name, money_accounts.currency AS currency
           FROM cash_transactions
           LEFT JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
           WHERE cash_transactions.ref_type = ? AND cash_transactions.ref_id = ?
           ORDER BY cash_transactions.id""",
        (ref_type, ref_id),
    ).fetchall()


# ---- balances and summaries ----

def _scope(location_id: int | None) -> tuple[str, list]:
    """Live rows of one точка (every точка when None) — cancelled rows
    never count toward anything."""
    if location_id is None:
        return " AND cash_transactions.cancelled_at IS NULL", []
    return " AND cash_transactions.cancelled_at IS NULL AND cash_transactions.location_id = ?", [location_id]


def cash_balance(conn: sqlite3.Connection, location_id: int | None = None):
    """Наличные гривни physically on hand right now — the sum over the
    наличные-UAH accounts. Other currencies and non-cash accounts have
    their own balances (core.accounts.balances) and are never mixed in."""
    clause, params = _scope(location_id)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN cash_transactions.kind = 'income' THEN cash_transactions.amount
                                     ELSE -cash_transactions.amount END), 0) AS balance
            FROM cash_transactions
            JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
            WHERE money_accounts.kind = 'cash' AND money_accounts.currency = 'UAH'{clause}""",
        params,
    ).fetchone()
    return money(row["balance"])


def period_summary(conn: sqlite3.Connection, utc_start: str, utc_end: str, location_id: int | None = None) -> dict:
    """Money in and out over one period, in гривня (amount_uah), split by
    «наличными» (cash accounts of any currency) and «безнал» (ФОП, карта,
    крипто). Обмен and перемещение are left out — moving money between
    one's own accounts is neither income nor an expense."""
    clause, params = _scope(location_id)
    internal = ",".join("?" for _ in _accounts.INTERNAL_CATEGORIES)
    row = conn.execute(
        f"""SELECT
             COALESCE(SUM(CASE WHEN cash_transactions.kind='income' AND money_accounts.kind='cash' THEN amount_uah ELSE 0 END), 0) AS income_cash,
             COALESCE(SUM(CASE WHEN cash_transactions.kind='income' AND money_accounts.kind!='cash' THEN amount_uah ELSE 0 END), 0) AS income_card,
             COALESCE(SUM(CASE WHEN cash_transactions.kind='expense' AND money_accounts.kind='cash' THEN amount_uah ELSE 0 END), 0) AS expense_cash,
             COALESCE(SUM(CASE WHEN cash_transactions.kind='expense' AND money_accounts.kind!='cash' THEN amount_uah ELSE 0 END), 0) AS expense_card
            FROM cash_transactions
            JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
            WHERE cash_transactions.created_at >= ? AND cash_transactions.created_at < ?
              AND (cash_transactions.category IS NULL OR cash_transactions.category NOT IN ({internal})){clause}""",
        [utc_start, utc_end, *_accounts.INTERNAL_CATEGORIES, *params],
    ).fetchone()
    values = {key: money(row[key]) for key in ("income_cash", "income_card", "expense_cash", "expense_card")}
    income_total = money(values["income_cash"] + values["income_card"])
    expense_total = money(values["expense_cash"] + values["expense_card"])
    return {**values, "income_total": income_total, "expense_total": expense_total,
            "net": money(income_total - expense_total)}


def list_transactions(conn: sqlite3.Connection, limit: int = 100, location_id: int | None = None) -> list[sqlite3.Row]:
    """Newest first, cancelled ones included (the page marks them) —
    nothing проведённое ever disappears from the feed."""
    where, params = ("WHERE cash_transactions.location_id = ?", [location_id]) if location_id is not None else ("", [])
    return conn.execute(
        f"""SELECT cash_transactions.*, staff.name AS staff_name,
                   money_accounts.name AS account_name, money_accounts.currency AS currency
            FROM cash_transactions
            LEFT JOIN staff ON staff.id = cash_transactions.staff_id
            LEFT JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
            {where}
            ORDER BY cash_transactions.created_at DESC, cash_transactions.id DESC
            LIMIT ?""",
        [*params, limit],
    ).fetchall()
