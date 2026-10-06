"""Обмен валют and перемещение денег (Заход 2, 06.10) — the two ways money
moves between the business's own accounts. Neither is income nor an
expense («деньги ≠ прибыль»): both post rows in the internal categories
that period summaries skip (core.accounts.INTERNAL_CATEGORIES), and each is
a document in the journal (ОБ, ДП).

Перемещение between two точки follows the same shape goods will in Заход 3:
«Отправил → В пути → Принял». The money leaves the source the moment it is
sent and is nobody's balance until someone at the destination confirms what
they actually counted; a difference is recorded on the transfer, never
quietly absorbed. Within one точка there is nobody to hand over to, so it
is received in the same step.
"""
from __future__ import annotations

import sqlite3

from core import accounts as _accounts
from core import cash as _cash
from core import documents as _documents
from core.accounts import AccountError, money


def _active_account(conn: sqlite3.Connection, account_id: int | None, what: str) -> sqlite3.Row:
    account = _accounts.get_account(conn, account_id)
    if not account or not account["active"]:
        raise AccountError(f"Выберите счёт, {what}.")
    return account


def _require_funds(conn: sqlite3.Connection, account: sqlite3.Row, amount) -> None:
    available = _accounts.balance(conn, account["id"])
    if amount > available:
        label = _accounts.currency_label(account["currency"])
        raise AccountError(f"На счёте «{account['name']}» только {available} {label} — нельзя снять {amount} {label}.")


def exchange(
    conn: sqlite3.Connection, from_account_id: int, to_account_id: int, amount_from, amount_to,
    staff_id: int, comment: str | None = None, key: str | None = None,
) -> int:
    """«−45 000 UAH, +1 000 USD по курсу 45.00». Both sides are what was
    actually handed over and received; the rate is derived from them (and
    always quoted as гривня per unit of the foreign currency when one side
    is гривня). Same точка only — moving money to another точка is a
    перемещение, a separate step with its own confirmation."""
    source = _active_account(conn, from_account_id, "с которого отдаёте")
    target = _active_account(conn, to_account_id, "на который получаете")
    amount_from, amount_to = _accounts.parse_amount(amount_from), _accounts.parse_amount(amount_to)
    if not amount_from or not amount_to:
        raise AccountError("Укажите, сколько отдали и сколько получили.")
    if source["id"] == target["id"]:
        raise AccountError("Счета должны быть разными.")
    if source["location_id"] != target["location_id"]:
        raise AccountError("Обмен — между счетами одной точки. Для другой точки оформите перемещение.")
    if source["currency"] == target["currency"]:
        raise AccountError("У счетов одна валюта — это перемещение, а не обмен.")
    _require_funds(conn, source, amount_from)

    base = _accounts.BASE_CURRENCY
    if source["currency"] == base:
        rate = amount_from / amount_to          # гривен за единицу купленной валюты
        source_rate, target_rate = None, rate
    elif target["currency"] == base:
        rate = amount_to / amount_from          # гривен за единицу проданной валюты
        source_rate, target_rate = rate, None
    else:
        rate = amount_to / amount_from          # валюта на валюту: гривневой оценки нет
        source_rate = target_rate = None

    exchange_id = conn.execute(
        """INSERT INTO money_exchanges
           (location_id, from_account_id, to_account_id, amount_from, amount_to, rate, comment, staff_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (source["location_id"], source["id"], target["id"], amount_from, amount_to, rate, comment, staff_id),
    ).lastrowid
    _cash.post(conn, "expense", source, amount_from, staff_id=staff_id, category="exchange",
               ref_type="exchange", ref_id=exchange_id, comment=comment, rate=source_rate)
    _cash.post(conn, "income", target, amount_to, staff_id=staff_id, category="exchange",
               ref_type="exchange", ref_id=exchange_id, comment=comment, rate=target_rate)
    _documents.register(
        conn, "exchange", staff_id=staff_id, location_id=source["location_id"], ref_table="money_exchanges",
        ref_id=exchange_id, key=key,
        title=(f"{amount_from} {_accounts.currency_label(source['currency'])} → "
               f"{amount_to} {_accounts.currency_label(target['currency'])} · курс {money(rate)}"),
    )
    return exchange_id


def send_transfer(
    conn: sqlite3.Connection, from_account_id: int, to_account_id: int, amount, staff_id: int,
    comment: str | None = None, key: str | None = None,
) -> int:
    source = _active_account(conn, from_account_id, "с которого отправляете")
    target = _active_account(conn, to_account_id, "на который отправляете")
    amount = _accounts.parse_amount(amount)
    if not amount:
        raise AccountError("Укажите сумму перемещения.")
    if source["id"] == target["id"]:
        raise AccountError("Счета должны быть разными.")
    if source["currency"] != target["currency"]:
        raise AccountError("Перемещать можно только между счетами одной валюты. Разные валюты — это обмен.")
    _require_funds(conn, source, amount)

    transfer_id = conn.execute(
        "INSERT INTO money_transfers (from_account_id, to_account_id, amount, comment, sent_by) VALUES (?, ?, ?, ?, ?)",
        (source["id"], target["id"], amount, comment, staff_id),
    ).lastrowid
    _cash.post(conn, "expense", source, amount, staff_id=staff_id, category="transfer",
               ref_type="money_transfer", ref_id=transfer_id, comment=comment)
    label = _accounts.currency_label(source["currency"])
    _documents.register(
        conn, "money_transfer", staff_id=staff_id, location_id=source["location_id"], ref_table="money_transfers",
        ref_id=transfer_id, key=key,
        title=f"{amount} {label}: {source['location_name']} · {source['name']} → {target['location_name']} · {target['name']}",
    )
    if source["location_id"] == target["location_id"]:
        receive_transfer(conn, transfer_id, staff_id, amount)
    return transfer_id


def get_transfer(conn: sqlite3.Connection, transfer_id: int) -> sqlite3.Row | None:
    return conn.execute(_TRANSFER_SELECT + " WHERE money_transfers.id = ?", (transfer_id,)).fetchone()


def receive_transfer(
    conn: sqlite3.Connection, transfer_id: int, staff_id: int, received_amount=None, note: str | None = None,
) -> None:
    """«Принял»: the money lands on the destination account — the amount
    actually counted, which is the sent amount unless the receiver says
    otherwise. A difference needs a note and stays on the transfer (and in
    the document's trail) as a расхождение for the owner to look into."""
    transfer = get_transfer(conn, transfer_id)
    if not transfer:
        raise AccountError("Перемещение не найдено.")
    if transfer["status"] != "sent":
        raise AccountError("Это перемещение уже принято или отменено.")
    received = transfer["amount"] if received_amount in (None, "") else _accounts.parse_amount(received_amount)
    if not received:
        raise AccountError("Укажите сумму, которую получили.")
    received = money(received)
    note = (note or "").strip() or None
    differs = received != money(transfer["amount"])
    if differs and not note:
        raise AccountError("Сумма отличается от отправленной — напишите, в чём расхождение.")

    target = _accounts.get_account(conn, transfer["to_account_id"])
    _cash.post(conn, "income", target, received, staff_id=staff_id, category="transfer",
               ref_type="money_transfer", ref_id=transfer_id, comment=transfer["comment"])
    conn.execute(
        """UPDATE money_transfers SET status = 'received', received_by = ?, received_at = datetime('now'),
                                      received_amount = ?, discrepancy_note = ?
           WHERE id = ?""",
        (staff_id, received, note if differs else None, transfer_id),
    )
    doc = _documents.get_for(conn, "money_transfer", transfer_id)
    if doc:
        label = _accounts.currency_label(target["currency"])
        text = f"получено {received} {label}"
        if differs:
            text += f" вместо {money(transfer['amount'])} {label}: {note}"
        _documents.add_event(conn, doc["id"], "discrepancy" if differs else "received", staff_id, text)


_TRANSFER_SELECT = """SELECT money_transfers.*,
                             fa.name AS from_name, fa.currency AS currency, fa.location_id AS from_location_id,
                             fl.name AS from_location_name,
                             ta.name AS to_name, ta.location_id AS to_location_id, tl.name AS to_location_name,
                             sender.name AS sent_by_name, receiver.name AS received_by_name
                      FROM money_transfers
                      JOIN money_accounts fa ON fa.id = money_transfers.from_account_id
                      JOIN locations fl ON fl.id = fa.location_id
                      JOIN money_accounts ta ON ta.id = money_transfers.to_account_id
                      JOIN locations tl ON tl.id = ta.location_id
                      LEFT JOIN staff sender ON sender.id = money_transfers.sent_by
                      LEFT JOIN staff receiver ON receiver.id = money_transfers.received_by"""


def list_in_transit(conn: sqlite3.Connection, location_id: int | None = None, direction: str = "incoming") -> list[sqlite3.Row]:
    """Money that has left one account and not yet reached the other —
    «в пути». direction='incoming' is what this точка must confirm,
    'outgoing' what it sent and is waiting on."""
    query = _TRANSFER_SELECT + " WHERE money_transfers.status = 'sent'"
    params: list = []
    if location_id is not None:
        query += " AND " + ("ta.location_id = ?" if direction == "incoming" else "fa.location_id = ?")
        params.append(location_id)
    return conn.execute(query + " ORDER BY money_transfers.sent_at", params).fetchall()


def list_discrepancies(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        _TRANSFER_SELECT + " WHERE money_transfers.discrepancy_note IS NOT NULL ORDER BY money_transfers.id DESC LIMIT ?",
        (limit,),
    ).fetchall()
