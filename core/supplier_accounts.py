"""Поставщики деталей: сметы из их чатов и взаиморасчёты (10.10).

Every supplier has a Telegram group of his own. A смета (a file with
what he is selling us and for how much) posted there is read by the bot
and becomes a draft here; a staff member then answers the same questions
in the same order, right in that group —

    магазин → сумма → оплата (сейчас / в долг / из депозита)
           → счёт (если сейчас) → курс (если счёт валютный) → провести

— and only «провести» writes anything to the books. This module is that
sequence and those books; the chat side is bot/supplier_chat.py.

The books are one signed ledger per supplier and currency
(supplier_ledger): a смета adds what we owe, a payment takes it off.
Balance > 0 — «мы должны поставщику», < 0 — «наш депозит у него». The
three ways to settle a смета:

    now      money leaves an account of the chosen точка today (a cash
             expense, category «Закупка») — the balance doesn't move;
    debt     nothing leaves — the balance grows by the amount;
    deposit  nothing leaves — it comes off money he already holds for us;
             refused if he holds less than the amount.

A payment or a deposit reported in the chat («бот, оплата 5000») is the
same record with kind 'payment': the same steps without «оплата», and it
only takes the amount off the balance.
"""
from __future__ import annotations

import json
import sqlite3

from core import accounts as _accounts
from core import cash as _cash
from core import documents as _documents
from core.accounts import money

CURRENCIES = ("UAH", "USD", "EUR", "USDT")
MODES = {"now": "оплачено сразу", "debt": "в долг", "deposit": "из депозита"}
STEPS = ("store", "amount", "mode", "account", "rate", "confirm")
MAX_AMOUNT = 10_000_000
_REF = "supplier_invoice"
_DOC_TYPE = {"invoice": "supplier_invoice", "payment": "supplier_payment"}


class SupplierError(Exception):
    """A step that can't go ahead — message is shown in the chat as-is."""


# ---- чаты ----

def get_chat(conn: sqlite3.Connection, chat_id) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT supplier_chats.*, suppliers.name AS supplier_name FROM supplier_chats
           JOIN suppliers ON suppliers.id = supplier_chats.supplier_id WHERE supplier_chats.chat_id = ?""",
        (str(chat_id),),
    ).fetchone()


def link_chat(conn: sqlite3.Connection, chat_id, title: str | None, staff_id: int | None, supplier_name: str | None = None) -> sqlite3.Row:
    """This group is this supplier's. The supplier is found by name (the
    name given, else the group's title) or created; linking again just
    moves the group to that supplier."""
    name = " ".join((supplier_name or title or "").split())
    if not name:
        raise SupplierError("Назовите поставщика: /supplier Название")
    # Compared here, not in SQL: SQLite's lower() leaves Cyrillic as it is.
    known = next((row["id"] for row in conn.execute("SELECT id, name FROM suppliers").fetchall() if row["name"].casefold() == name.casefold()), None)
    supplier_id = known or conn.execute("INSERT INTO suppliers (name) VALUES (?)", (name,)).lastrowid
    conn.execute(
        """INSERT INTO supplier_chats (chat_id, supplier_id, title, linked_by) VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_id) DO UPDATE SET supplier_id = excluded.supplier_id, title = excluded.title, linked_by = excluded.linked_by""",
        (str(chat_id), supplier_id, title, staff_id),
    )
    return get_chat(conn, chat_id)


def unlink_chat(conn: sqlite3.Connection, chat_id) -> bool:
    return conn.execute("DELETE FROM supplier_chats WHERE chat_id = ?", (str(chat_id),)).rowcount > 0


def set_desk(conn: sqlite3.Connection, supplier_name: str | None, chat_id, thread_id: int | None) -> sqlite3.Row:
    """Where this supplier's сметы are confirmed: a staff group (and a тема
    of it) instead of his own group, where the bot then says nothing —
    he reads that chat and the bot is not for him. `chat_id` None — back
    to his group. The supplier is named in full or by a part of the name
    («Глушко»), and must already have a group."""
    wanted = " ".join((supplier_name or "").split()).casefold()
    rows = conn.execute(
        "SELECT supplier_chats.chat_id, suppliers.name FROM supplier_chats JOIN suppliers ON suppliers.id = supplier_chats.supplier_id ORDER BY suppliers.name"
    ).fetchall()
    found = [row for row in rows if row["name"].casefold() == wanted] or [row for row in rows if wanted and wanted in row["name"].casefold()]
    if len(found) != 1:
        known = ", ".join(f"«{row['name']}»" for row in rows) or "пока ни у кого"
        raise SupplierError(
            "Это рабочая группа магазина. Сюда можно направить сметы поставщика, у которого уже есть своя группа: "
            f"/supplier Название. Группы есть у: {known}."
        )
    conn.execute("UPDATE supplier_chats SET desk_chat_id = ?, desk_thread_id = ? WHERE chat_id = ?",
                 (str(chat_id) if chat_id is not None else None, thread_id if chat_id is not None else None, found[0]["chat_id"]))
    return get_chat(conn, found[0]["chat_id"])


def log_message(conn: sqlite3.Connection, chat_id, message_id: int | None, author_id: int | None, author_name: str | None,
                is_staff: bool, text: str | None, file_name: str | None = None) -> None:
    conn.execute(
        """INSERT INTO supplier_chat_messages (chat_id, message_id, author_id, author_name, is_staff, text, file_name)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (str(chat_id), message_id, author_id, author_name, 1 if is_staff else 0, (text or "")[:4000] or None, file_name),
    )


# ---- баланс ----

def balances(conn: sqlite3.Connection, supplier_id: int) -> dict[str, float]:
    """{currency: balance} — only currencies with something on them."""
    rows = conn.execute(
        """SELECT currency, ROUND(SUM(amount), 2) AS total FROM supplier_ledger
           WHERE supplier_id = ? AND cancelled_at IS NULL GROUP BY currency""",
        (supplier_id,),
    ).fetchall()
    return {row["currency"]: money(row["total"]) for row in rows if abs(row["total"]) >= 0.01}


def balance(conn: sqlite3.Connection, supplier_id: int, currency: str):
    return balances(conn, supplier_id).get(currency, 0)


def balance_text(conn: sqlite3.Connection, supplier_id: int) -> str:
    """«мы должны 5400 UAH · наш депозит 300 USD», or «взаиморасчёты 0»."""
    parts = [(f"мы должны {value} {cur}" if value > 0 else f"наш депозит {money(-value)} {cur}")
             for cur, value in sorted(balances(conn, supplier_id).items())]
    return " · ".join(parts) or "взаиморасчёты 0"


def all_balances(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": row["id"], "name": row["name"], "balances": balances(conn, row["id"])}
        for row in conn.execute("SELECT id, name FROM suppliers ORDER BY name").fetchall()
    ]


def _post(conn, supplier_id, currency, amount, kind, *, ref_id, location_id, staff_id, comment) -> None:
    conn.execute(
        """INSERT INTO supplier_ledger (supplier_id, currency, amount, kind, ref_type, ref_id, location_id, staff_id, comment)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (supplier_id, currency, money(amount), kind, _REF, ref_id, location_id, staff_id, comment),
    )


def set_opening(conn: sqlite3.Connection, supplier_id: int, currency: str, target, staff_id: int | None) -> tuple:
    """«Сейчас между нами столько» (27.10): the balance in this currency is
    made exactly `target` (positive — we owe, negative — our deposit) by one
    «остаток» line for the difference. Said again — set again, not added.
    Returns (before, after)."""
    before = balance(conn, supplier_id, currency)
    difference = money(money(target) - before)
    if difference:
        _post(conn, supplier_id, currency, difference, "opening", ref_id=None, location_id=None, staff_id=staff_id,
              comment=f"остаток: было {before}, стало {money(target)}")
    return before, balance(conn, supplier_id, currency)


# ---- смета / платёж: черновик и его шаги ----

_SELECT = """SELECT supplier_invoices.*, suppliers.name AS supplier_name, locations.name AS location_name,
                    money_accounts.name AS account_name, money_accounts.currency AS account_currency
             FROM supplier_invoices
             JOIN suppliers ON suppliers.id = supplier_invoices.supplier_id
             LEFT JOIN locations ON locations.id = supplier_invoices.location_id
             LEFT JOIN money_accounts ON money_accounts.id = supplier_invoices.account_id"""


def get(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE supplier_invoices.id = ?", (invoice_id,)).fetchone()


def by_card(conn: sqlite3.Connection, chat_id, message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        _SELECT + " WHERE COALESCE(supplier_invoices.card_chat_id, supplier_invoices.chat_id) = ? AND supplier_invoices.card_message_id = ?",
        (str(chat_id), message_id),
    ).fetchone()


def reading_of(invoice: sqlite3.Row) -> dict:
    """What was read from the file (core.supplier_files.read's result) —
    kept as it came, for the card; {} for a payment."""
    try:
        reading = json.loads(invoice["items_json"] or "{}")
    except ValueError:
        return {}
    return reading if isinstance(reading, dict) else {}


def totals_of(invoice: sqlite3.Row) -> dict:
    """{currency: total} when the file gives every price in two currencies
    (dollars and гривня side by side); {} for an ordinary смета."""
    totals = reading_of(invoice).get("totals")
    return totals if isinstance(totals, dict) else {}


def file_rate(invoice: sqlite3.Row) -> float | None:
    """The rate the supplier himself counted by — known only when the file
    has both columns and the смета is being settled in the foreign one."""
    rate = reading_of(invoice).get("rate")
    if invoice["currency"] == _accounts.BASE_CURRENCY or invoice["currency"] not in totals_of(invoice):
        return None
    return float(rate) if isinstance(rate, (int, float)) and rate > 0 else None


def doc_type(invoice: sqlite3.Row) -> str:
    return _DOC_TYPE[invoice["kind"]]


def label(invoice: sqlite3.Row) -> str:
    """«СЧ-007» for a смета, «ОП-008» for a payment — one counter for both."""
    return _documents.label(doc_type(invoice), invoice["id"])


def document(conn: sqlite3.Connection, invoice: sqlite3.Row) -> sqlite3.Row | None:
    return _documents.get_for(conn, doc_type(invoice), invoice["id"])


def create(conn: sqlite3.Connection, *, supplier_id: int, chat_id=None, kind: str = "invoice", reading: dict | None = None,
           currency: str = "UAH", total=None, file_name: str | None = None, source_message_id: int | None = None) -> int:
    """A draft: nothing in the books yet. A смета starts with the total
    read from the file as the amount to pay (it can be changed at the
    «сумма» step); a payment starts with the amount that was said."""
    currency = currency if currency in CURRENCIES else "UAH"
    total = money(total) if total else None
    return conn.execute(
        """INSERT INTO supplier_invoices (kind, supplier_id, chat_id, source_message_id, file_name, items_json, currency, total, amount, pay_mode)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, supplier_id, str(chat_id) if chat_id is not None else None, source_message_id, file_name,
         json.dumps(reading or {}, ensure_ascii=False), currency, total, total, "now" if kind == "payment" else None),
    ).lastrowid


def set_card(conn: sqlite3.Connection, invoice_id: int, message_id: int, chat_id=None) -> None:
    """Where the card is now: the message, and the chat when it isn't the
    supplier's own group (see set_desk)."""
    conn.execute("UPDATE supplier_invoices SET card_message_id = ?, card_chat_id = ? WHERE id = ?",
                 (message_id, str(chat_id) if chat_id is not None else None, invoice_id))


def card_chat(invoice: sqlite3.Row) -> str | None:
    return invoice["card_chat_id"] or invoice["chat_id"]


def _draft(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    invoice = get(conn, invoice_id)
    if not invoice:
        raise SupplierError("Запись не найдена.")
    if invoice["status"] != "draft":
        raise SupplierError("Уже проведено." if invoice["status"] == "posted" else "Уже отменено.")
    return invoice


def _next_step(invoice: sqlite3.Row) -> str:
    """The first question still unanswered — always in the same order."""
    if not invoice["location_id"]:
        return "store"
    if not invoice["amount"]:
        return "amount"
    if not invoice["pay_mode"]:
        return "mode"
    if invoice["pay_mode"] == "now":
        if not invoice["account_id"]:
            return "account"
        if invoice["account_currency"] != _accounts.BASE_CURRENCY and not invoice["rate"]:
            return "rate"
    return "confirm"


def _advance(conn: sqlite3.Connection, invoice_id: int, staff_id: int | None, *, amount_confirmed: bool = False) -> sqlite3.Row:
    invoice = get(conn, invoice_id)
    step = _next_step(invoice)
    # «Сумма» is asked once even though a смета arrives with one: the
    # person confirms what the file says or types what was agreed. (A
    # payment's amount was said by the person — not asked again.)
    if (invoice["kind"] == "invoice" and step in ("mode", "account", "rate", "confirm")
            and invoice["step"] in ("store", "amount") and not amount_confirmed):
        step = "amount"
    conn.execute("UPDATE supplier_invoices SET step = ?, staff_id = COALESCE(?, staff_id) WHERE id = ?", (step, staff_id, invoice_id))
    return get(conn, invoice_id)


def choose_store(conn: sqlite3.Connection, invoice_id: int, location_id: int, staff_id: int | None) -> sqlite3.Row:
    _draft(conn, invoice_id)
    if not conn.execute("SELECT 1 FROM locations WHERE id = ? AND active = 1", (location_id,)).fetchone():
        raise SupplierError("Такой точки нет.")
    # Another точка has other accounts — one chosen before no longer applies.
    conn.execute("UPDATE supplier_invoices SET location_id = ?, account_id = NULL, rate = NULL, step = 'store' WHERE id = ?", (location_id, invoice_id))
    return _advance(conn, invoice_id, staff_id)


def set_amount(conn: sqlite3.Connection, invoice_id: int, amount, staff_id: int | None) -> sqlite3.Row:
    """«Сумма»: None confirms the amount as it stands; a number replaces it."""
    invoice = _draft(conn, invoice_id)
    if amount is not None:
        value = _accounts.parse_amount(amount)
        if not value or value <= 0 or value > MAX_AMOUNT:
            raise SupplierError("Сумма должна быть числом больше нуля, например 5400.")
        conn.execute("UPDATE supplier_invoices SET amount = ? WHERE id = ?", (money(value), invoice_id))
    elif not invoice["amount"]:
        raise SupplierError("Сумму не удалось прочитать из файла — ответьте на это сообщение числом.")
    return _advance(conn, invoice_id, staff_id, amount_confirmed=True)


def set_currency(conn: sqlite3.Connection, invoice_id: int, currency: str, staff_id: int | None) -> sqlite3.Row:
    """The file was read in the wrong currency — fix it at the «сумма» step.
    A file with two price columns has a total of its own for each: going
    over to the other column takes that column's sum, not the same number
    under another name."""
    invoice = _draft(conn, invoice_id)
    if currency not in CURRENCIES:
        raise SupplierError("Такой валюты нет.")
    column_total = totals_of(invoice).get(currency)
    if column_total:
        conn.execute("UPDATE supplier_invoices SET total = ?, amount = ? WHERE id = ?", (money(column_total), money(column_total), invoice_id))
    conn.execute(
        "UPDATE supplier_invoices SET currency = ?, account_id = NULL, rate = NULL, staff_id = COALESCE(?, staff_id) WHERE id = ?",
        (currency, staff_id, invoice_id),
    )
    return get(conn, invoice_id)


def choose_mode(conn: sqlite3.Connection, invoice_id: int, mode: str, staff_id: int | None) -> sqlite3.Row:
    invoice = _draft(conn, invoice_id)
    if invoice["kind"] != "invoice" or mode not in MODES:
        raise SupplierError("Так оплатить нельзя.")
    if mode == "deposit":
        held = -balance(conn, invoice["supplier_id"], invoice["currency"])
        if held < invoice["amount"]:
            raise SupplierError(
                f"Депозита не хватает: у поставщика {max(money(held), 0)} {invoice['currency']}, а смета на {money(invoice['amount'])}. "
                "Выберите «сейчас» или «в долг»."
            )
    conn.execute("UPDATE supplier_invoices SET pay_mode = ?, account_id = NULL, rate = NULL WHERE id = ?", (mode, invoice_id))
    return _advance(conn, invoice_id, staff_id, amount_confirmed=True)


def payable_accounts(conn: sqlite3.Connection, invoice: sqlite3.Row) -> list[sqlite3.Row]:
    """Accounts of the chosen точка the amount can be paid from: the ones
    in the смета's currency (a гривня смета — гривня accounts)."""
    if not invoice["location_id"]:
        return []
    return [a for a in _accounts.list_accounts(conn, invoice["location_id"]) if a["active"] and a["currency"] == invoice["currency"]]


def choose_account(conn: sqlite3.Connection, invoice_id: int, account_id: int, staff_id: int | None) -> sqlite3.Row:
    invoice = _draft(conn, invoice_id)
    if account_id not in [a["id"] for a in payable_accounts(conn, invoice)]:
        raise SupplierError("Этот счёт не подходит: нужен счёт выбранной точки в валюте сметы.")
    conn.execute("UPDATE supplier_invoices SET account_id = ?, rate = NULL WHERE id = ?", (account_id, invoice_id))
    return _advance(conn, invoice_id, staff_id, amount_confirmed=True)


def set_rate(conn: sqlite3.Connection, invoice_id: int, rate, staff_id: int | None) -> sqlite3.Row:
    _draft(conn, invoice_id)
    value = _accounts.parse_amount(rate)
    if not value or not 0 < value < 10_000:
        raise SupplierError("Курс — число: сколько гривен за единицу валюты, например 41.5.")
    conn.execute("UPDATE supplier_invoices SET rate = ? WHERE id = ?", (float(value), invoice_id))
    return _advance(conn, invoice_id, staff_id, amount_confirmed=True)


def take_file_rate(conn: sqlite3.Connection, invoice_id: int, staff_id: int | None) -> sqlite3.Row:
    """«Курс» answered with the supplier's own — the гривня value then
    comes out as the гривня column of his file, to the копійка."""
    rate = file_rate(_draft(conn, invoice_id))
    if not rate:
        raise SupplierError("В смете курса нет — ответьте на сообщение числом.")
    conn.execute("UPDATE supplier_invoices SET rate = ? WHERE id = ?", (rate, invoice_id))
    return _advance(conn, invoice_id, staff_id, amount_confirmed=True)


def restart(conn: sqlite3.Connection, invoice_id: int, staff_id: int | None) -> sqlite3.Row:
    """«Изменить»: the same questions again from the first one."""
    invoice = _draft(conn, invoice_id)
    conn.execute(
        "UPDATE supplier_invoices SET location_id = NULL, pay_mode = ?, account_id = NULL, rate = NULL, step = 'store' WHERE id = ?",
        ("now" if invoice["kind"] == "payment" else None, invoice_id),
    )
    return get(conn, invoice_id)


def cancel_draft(conn: sqlite3.Connection, invoice_id: int) -> sqlite3.Row:
    _draft(conn, invoice_id)
    conn.execute("UPDATE supplier_invoices SET status = 'cancelled' WHERE id = ?", (invoice_id,))
    return get(conn, invoice_id)


def post(conn: sqlite3.Connection, invoice_id: int, staff_id: int) -> sqlite3.Row:
    """«Провести» — the only step that writes to the books, and only when
    every question has its answer. Everything is checked again here."""
    invoice = _draft(conn, invoice_id)
    if _next_step(invoice) != "confirm":
        raise SupplierError("Сначала ответьте на все вопросы выше.")
    amount, currency, supplier_id = money(invoice["amount"]), invoice["currency"], invoice["supplier_id"]
    title = f"{invoice['supplier_name']}" + (f" · {invoice['file_name']}" if invoice["file_name"] else "")
    if invoice["kind"] == "invoice":
        if invoice["pay_mode"] == "deposit" and -balance(conn, supplier_id, currency) < amount:
            raise SupplierError("Депозита уже не хватает — выберите другой способ («Изменить»).")
        _post(conn, supplier_id, currency, amount, "invoice", ref_id=invoice_id, location_id=invoice["location_id"],
              staff_id=staff_id, comment=label(invoice))
    paid_now = invoice["kind"] == "payment" or invoice["pay_mode"] == "now"
    amount_uah = amount if currency == _accounts.BASE_CURRENCY else (money(amount * invoice["rate"]) if invoice["rate"] else None)
    if paid_now:
        account = _accounts.get_account(conn, invoice["account_id"])
        if not account or not account["active"] or account["location_id"] != invoice["location_id"] or account["currency"] != currency:
            raise SupplierError("Счёт больше не подходит — выберите заново («Изменить»).")
        _cash.post(
            conn, "expense", account, amount, staff_id=staff_id, category="supplies", ref_type=_REF, ref_id=invoice_id,
            comment=f"Поставщику {invoice['supplier_name']}: {label(invoice)}",
            rate=invoice["rate"] if currency != _accounts.BASE_CURRENCY else None,
        )
        _post(conn, supplier_id, currency, -amount, "payment", ref_id=invoice_id, location_id=invoice["location_id"],
              staff_id=staff_id, comment=f"Оплата {label(invoice)}")
    conn.execute(
        "UPDATE supplier_invoices SET status = 'posted', posted_at = datetime('now'), staff_id = ?, step = 'confirm' WHERE id = ?",
        (staff_id, invoice_id),
    )
    if invoice["kind"] == "invoice":
        _documents.register(
            conn, _REF, staff_id=staff_id, location_id=invoice["location_id"], ref_table="supplier_invoices", ref_id=invoice_id,
            title=f"{title} · {MODES[invoice['pay_mode']]}" + ("" if currency == _accounts.BASE_CURRENCY else f" · {amount} {currency}"),
            amount=amount_uah,
        )
    else:
        _documents.register(
            conn, "supplier_payment", staff_id=staff_id, location_id=invoice["location_id"], ref_table="supplier_invoices",
            ref_id=invoice_id, title=f"{title} · {invoice['account_name']}" + ("" if currency == _accounts.BASE_CURRENCY else f" · {amount} {currency}"),
            amount=-amount_uah if amount_uah is not None else None,
        )
    return get(conn, invoice_id)


def cancel_posted(conn: sqlite3.Connection, invoice_id: int) -> None:
    """Undo a проведённая смета or payment: its ledger rows and cash rows
    stop counting (called by core.doc_cancel, which marks the document)."""
    for row in conn.execute("SELECT id FROM cash_transactions WHERE ref_type = ? AND ref_id = ?", (_REF, invoice_id)).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    conn.execute(
        "UPDATE supplier_ledger SET cancelled_at = datetime('now') WHERE ref_type = ? AND ref_id = ? AND cancelled_at IS NULL",
        (_REF, invoice_id),
    )
    conn.execute("UPDATE supplier_invoices SET status = 'cancelled' WHERE id = ?", (invoice_id,))


def recent(conn: sqlite3.Connection, supplier_id: int | None = None, limit: int = 20) -> list[sqlite3.Row]:
    query, params = _SELECT + " WHERE supplier_invoices.status = 'posted'", []
    if supplier_id:
        query += " AND supplier_invoices.supplier_id = ?"
        params.append(supplier_id)
    return conn.execute(query + " ORDER BY supplier_invoices.id DESC LIMIT ?", [*params, limit]).fetchall()
