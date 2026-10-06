"""Денежные счета (Заход 2, 06.10) — каждая точка has its own set of
independent balances instead of one «касса»: наличные in each currency,
ФОП, карта, a crypto wallet. core.cash writes the money rows; this module
is the accounts themselves and their balances.

A balance is never stored anywhere. It is the signed sum of the account's
cash_transactions rows that haven't been cancelled — so it can only change
through a document, and any figure on screen can be re-derived from the
feed.

Amounts are in the ACCOUNT's currency and may have up to two decimals
(12.5 USDT); every row also carries amount_uah, its гривня value at the
rate of that very operation — what documents and period reports add up.

Leaf module like core.documents (imports nothing from core) so
core.storage.init_db can seed and backfill through it.
"""
from __future__ import annotations

import sqlite3

KINDS = {"cash": "Наличные", "fop": "Счёт ФОП", "card": "Карта", "crypto": "Криптокошелёк"}
CURRENCIES = ("UAH", "USD", "EUR", "USDT")
BASE_CURRENCY = "UAH"
CURRENCY_LABELS = {"UAH": "грн", "USD": "USD", "EUR": "EUR", "USDT": "USDT"}

# What a точка gets on day one — the set from the презентация. Nothing here
# is fixed: owner/admin rename, switch off and add accounts per точка.
DEFAULT_ACCOUNTS = (
    ("cash", "UAH", "Наличные"),
    ("cash", "USD", "Наличные USD"),
    ("cash", "EUR", "Наличные EUR"),
    ("fop", "UAH", "Счёт ФОП"),
    ("card", "UAH", "Карта"),
    ("crypto", "USDT", "Криптокошелёк USDT"),
)

# cash_transactions.method predates accounts and still has a CHECK of
# ('cash','card') on it — every row keeps a value there, derived from the
# account's kind: physical cash, or everything that isn't.
_LEGACY_METHOD = {"cash": "cash", "fop": "card", "card": "card", "crypto": "card"}

# Internal moves of money between accounts — never income or an expense of
# the business, so period summaries leave them out («деньги ≠ прибыль»).
INTERNAL_CATEGORIES = ("exchange", "transfer")


class AccountError(Exception):
    """An account operation that can't go ahead — message is shown to staff as-is."""


def money(value) -> int | float:
    """Normalize an amount: two decimals at most, and a whole number comes
    back as an int (so 500.0 never shows up on screen as «500.0»)."""
    rounded = round(float(value or 0), 2)
    return int(rounded) if rounded == int(rounded) else rounded


def parse_amount(text) -> int | float | None:
    """What staff typed -> a positive amount, or None if it isn't one.
    Accepts «1200», «12,5», «12.50», «1 200»."""
    cleaned = str(text or "").strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0 or value != value or value == float("inf"):
        return None
    return money(value)


def legacy_method(kind: str) -> str:
    return _LEGACY_METHOD.get(kind, "card")


def currency_label(currency: str) -> str:
    return CURRENCY_LABELS.get(currency, currency)


def ensure_for_location(conn: sqlite3.Connection, location_id: int) -> None:
    """The default set for a точка that has no accounts at all yet. A точка
    whose owner already arranged their own is left exactly as it is."""
    if conn.execute("SELECT 1 FROM money_accounts WHERE location_id = ? LIMIT 1", (location_id,)).fetchone():
        return
    for sort, (kind, currency, name) in enumerate(DEFAULT_ACCOUNTS):
        conn.execute(
            "INSERT INTO money_accounts (location_id, kind, currency, name, sort) VALUES (?, ?, ?, ?, ?)",
            (location_id, kind, currency, name, sort),
        )


def ensure_default_accounts(conn: sqlite3.Connection) -> None:
    for row in conn.execute("SELECT id FROM locations").fetchall():
        ensure_for_location(conn, row["id"])


def list_accounts(
    conn: sqlite3.Connection, location_id: int | None = None, include_inactive: bool = False,
) -> list[sqlite3.Row]:
    query = """SELECT money_accounts.*, locations.name AS location_name
               FROM money_accounts JOIN locations ON locations.id = money_accounts.location_id
               WHERE 1=1"""
    params: list = []
    if location_id is not None:
        query += " AND money_accounts.location_id = ?"
        params.append(location_id)
    if not include_inactive:
        query += " AND money_accounts.active = 1"
    query += " ORDER BY money_accounts.location_id, money_accounts.sort, money_accounts.id"
    return conn.execute(query, params).fetchall()


def get_account(conn: sqlite3.Connection, account_id: int | None) -> sqlite3.Row | None:
    if not account_id:
        return None
    return conn.execute(
        """SELECT money_accounts.*, locations.name AS location_name
           FROM money_accounts JOIN locations ON locations.id = money_accounts.location_id
           WHERE money_accounts.id = ?""",
        (account_id,),
    ).fetchone()


def default_account(conn: sqlite3.Connection, location_id: int, method: str = "cash") -> sqlite3.Row:
    """The гривня account a caller that only knows «cash» or «card» means:
    the точка's first active наличные-UAH account, or its first active
    non-cash UAH one (карта before ФОП). Falls back to any UAH account of
    the точка, creating the default set if it has none at all — money must
    always have somewhere to land."""
    ensure_for_location(conn, location_id)
    accounts = [a for a in list_accounts(conn, location_id) if a["currency"] == BASE_CURRENCY]
    if method == "cash":
        preferred = [a for a in accounts if a["kind"] == "cash"]
    else:
        preferred = [a for a in accounts if a["kind"] == "card"] or [a for a in accounts if a["kind"] != "cash"]
    if preferred:
        return preferred[0]
    if accounts:
        return accounts[0]
    any_account = list_accounts(conn, location_id, include_inactive=True)
    return any_account[0]


def create_account(conn: sqlite3.Connection, location_id: int, kind: str, currency: str, name: str) -> int:
    name = (name or "").strip()
    if kind not in KINDS:
        raise AccountError("Выберите тип счёта.")
    if currency not in CURRENCIES:
        raise AccountError("Выберите валюту счёта.")
    if not name:
        raise AccountError("Введите название счёта.")
    sort = conn.execute(
        "SELECT COALESCE(MAX(sort), -1) + 1 AS n FROM money_accounts WHERE location_id = ?", (location_id,)
    ).fetchone()["n"]
    return conn.execute(
        "INSERT INTO money_accounts (location_id, kind, currency, name, sort) VALUES (?, ?, ?, ?, ?)",
        (location_id, kind, currency, name, sort),
    ).lastrowid


def rename_account(conn: sqlite3.Connection, account_id: int, name: str) -> None:
    name = (name or "").strip()
    if not name:
        raise AccountError("Введите название счёта.")
    conn.execute("UPDATE money_accounts SET name = ? WHERE id = ?", (name, account_id))


def set_active(conn: sqlite3.Connection, account_id: int, active: bool) -> None:
    """Switching an account off only hides it from pickers — its history
    stays. Refused while it still holds money: a balance must not vanish
    from every screen."""
    if not active and balance(conn, account_id) != 0:
        raise AccountError("На счёте есть остаток — сначала переместите или обменяйте деньги.")
    conn.execute("UPDATE money_accounts SET active = ? WHERE id = ?", (int(active), account_id))


def balance(conn: sqlite3.Connection, account_id: int):
    row = conn.execute(
        """SELECT COALESCE(SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END), 0) AS balance
           FROM cash_transactions WHERE account_id = ? AND cancelled_at IS NULL""",
        (account_id,),
    ).fetchone()
    return money(row["balance"])


def balances(conn: sqlite3.Connection, location_id: int | None = None, include_inactive: bool = False) -> list[dict]:
    """Every account (of one точка, or of the whole business) with its
    current balance — the «сверить остатки» list."""
    sums = {
        row["account_id"]: row["balance"]
        for row in conn.execute(
            """SELECT account_id, SUM(CASE WHEN kind = 'income' THEN amount ELSE -amount END) AS balance
               FROM cash_transactions WHERE cancelled_at IS NULL AND account_id IS NOT NULL
               GROUP BY account_id"""
        ).fetchall()
    }
    result = []
    for account in list_accounts(conn, location_id, include_inactive=True):
        value = money(sums.get(account["id"], 0))
        if not account["active"] and not include_inactive and value == 0:
            continue
        result.append({**dict(account), "balance": value, "currency_label": currency_label(account["currency"])})
    return result


def backfill_transactions(conn: sqlite3.Connection) -> None:
    """Rows written before accounts existed knew only «cash» or «card», in
    гривня: each goes to its точка's default account for that method, worth
    exactly its own amount. Only ever touches rows with no account yet."""
    rows = conn.execute(
        "SELECT id, location_id, method, amount FROM cash_transactions WHERE account_id IS NULL"
    ).fetchall()
    if not rows:
        return
    cache: dict[tuple[int, str], int] = {}
    for row in rows:
        key = (row["location_id"], row["method"])
        if key not in cache:
            cache[key] = default_account(conn, row["location_id"], row["method"])["id"]
        conn.execute(
            "UPDATE cash_transactions SET account_id = ?, amount_uah = ?, rate = 1 WHERE id = ?",
            (cache[key], row["amount"], row["id"]),
        )
