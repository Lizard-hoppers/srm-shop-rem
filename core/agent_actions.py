"""What the bot's AI assistant can DO in the CRM (10.10) — «бот, запиши
расход 500 на воду», «бот, прими ремонт: айфон 13, экран, 3000, клиент
0671234567», «бот, продай кабель клиенту 067… наличными».

Павел's rule for the bot: it does what it is told, at once, and does not
send people to the Mini App or ask them to confirm; the safety net is a
«Вернуть» after, not a question before. This module is that rule made
safe:

  * the model only FILLS IN a form — the name of an action and its
    fields. Everything that decides what actually happens is code: who
    may do it (the same roles as in the Mini App), which client / repair
    / product / account is meant (found here, and only when exactly one
    fits — never a guess), whether the amounts make sense;
  * an action either goes through whole, through the same core function
    the Mini App's form calls, or is refused with a line saying what is
    missing — the model passes that on as a question;
  * what was done is reported by code, not by the model: a receipt line
    per action (ctx["receipts"]), each with what «Вернуть» has to undo.
    The model's own words never stand in for the record.

A repair's STATUS is not here on purpose: the model mixes «готов» and
«выдан» up (seen live, 10.10) — a status said to the bot is handled by
the narrow reading in bot/assistant_chat.py before the model is asked.
"""
from __future__ import annotations

import html
import re
import sqlite3

from core import accounts as _accounts
from core import cash as _cash
from core import clients as _clients
from core import doc_cancel as _doc_cancel
from core import documents as _documents
from core import inventory as _inventory
from core import masters as _masters
from core import orders as _orders
from core import repair_notes as _notes
from core import repairs as _repairs
from core import sales as _sales
from core import settlements as _settlements
from core import shifts as _shifts
from core.accounts import money
from core.agent_tools import _REPAIR_NUMBER, _digits, matches
from core.timefmt import kyiv_datetime

_EVERYONE = ("owner", "admin", "storekeeper", "master")
_CASH = ("owner", "admin", "storekeeper")
_BOSS = ("owner", "admin")
_INTAKE = ("owner", "admin", "master")

MAX_AMOUNT = 1_000_000


class Refused(Exception):
    """The action can't go ahead as asked — the message says why, for the
    model to pass on (usually as a question)."""


# ---- finding what is meant: exactly one, or a refusal that lists the candidates ----

def _amount(value, what: str = "сумму"):
    amount = _accounts.parse_amount(value)
    if not amount or amount <= 0 or amount > MAX_AMOUNT:
        raise Refused(f"Назовите {what} числом.")
    return money(amount)


def _qty(value) -> int:
    try:
        qty = int(value or 1)
    except (TypeError, ValueError) as exc:
        raise Refused("Количество должно быть числом.") from exc
    if not 0 < qty <= 1000:
        raise Refused("Количество должно быть больше нуля.")
    return qty


def _account(conn, ctx, word) -> sqlite3.Row:
    """The account money goes onto / comes off: by a word («наличные»,
    «карта», «фоп», «доллары») among the точка's accounts; the cash
    гривня one when nothing is said."""
    accounts = [a for a in _accounts.list_accounts(conn, ctx["location_id"]) if a["active"]]
    if not word:
        return _accounts.default_account(conn, ctx["location_id"], "cash")
    lowered = str(word).casefold()
    hints = {"нал": "cash", "кэш": "cash", "cash": "cash", "карт": "card", "card": "card", "терминал": "card",
             "перевод": "card", "фоп": "fop", "fop": "fop", "крипт": "crypto", "usdt": "crypto"}
    currency = next((code for key, code in (("дол", "USD"), ("usd", "USD"), ("$", "USD"), ("евр", "EUR"), ("eur", "EUR"), ("usdt", "USDT"))
                     if key in lowered), None)
    kind = next((value for key, value in hints.items() if key in lowered), None)
    found = [a for a in accounts if (kind is None or a["kind"] == kind)
             and (a["currency"] == (currency or (_accounts.BASE_CURRENCY if kind != "crypto" else a["currency"])))]
    if not found:
        found = [a for a in accounts if matches(str(word), a["name"])]
    if len(found) != 1:
        raise Refused("На какой счёт? У точки есть: " + ", ".join(a["name"] for a in accounts) + ".")
    if found[0]["currency"] != _accounts.BASE_CURRENCY:
        raise Refused(f"Счёт «{found[0]['name']}» валютный — такие операции с курсом делаются в приложении, раздел «Касса».")
    return found[0]


def _client(conn, phone=None, name=None, *, create: bool = False) -> sqlite3.Row:
    normalized = _clients.normalize_phone(str(phone or ""))
    if normalized:
        row = _clients.get_by_phone(conn, normalized)
        if row:
            return row
        if create:
            return _clients.get_client(conn, _clients.get_or_create_by_phone(conn, (name or "").strip() or normalized, normalized))
        raise Refused(f"Клиента с номером {normalized} в базе нет.")
    if phone and not normalized:
        raise Refused("Не похоже на номер телефона — назовите его полностью, например 0671234567.")
    if name:
        found = [c for c in conn.execute("SELECT * FROM clients ORDER BY id DESC").fetchall() if matches(str(name), c["name"])]
        if len(found) == 1:
            return found[0]
        if found:
            raise Refused("Таких клиентов несколько: " + "; ".join(f"{c['name']} {c['phone'] or ''}".strip() for c in found[:6]) + ". Назовите номер телефона.")
        raise Refused(f"Клиента «{name}» в базе нет — назовите номер телефона.")
    raise Refused("Какой клиент? Назовите номер телефона.")


def _repair(conn, ctx, ref) -> sqlite3.Row:
    rows = [r for r in _repairs.list_repairs(conn, location_id=ctx["location_id"]) if r["status"] not in ("issued", "cancelled")]
    numbered = _REPAIR_NUMBER.search(str(ref or "")) or (re.fullmatch(r"\s*(\d{1,6})\s*", str(ref or "")))
    if numbered:
        found = [r for r in rows if r["id"] == int(numbered.group(1))]
    else:
        found = [r for r in rows if ref and matches(str(ref), r["device_type"], r["brand"], r["model"], r["client_name"])]
    if len(found) == 1:
        return _repairs.get_repair(conn, found[0]["id"])
    listing = "; ".join(f"{_documents.label('repair', r['id'])} {' '.join(filter(None, [r['brand'], r['model']]))}" for r in (found or rows)[:8])
    if found:
        raise Refused(f"Под «{ref}» подходят несколько ремонтов: {listing}. Назовите номер РК.")
    raise Refused(f"Открытого ремонта «{ref}» нет. Сейчас открыты: {listing or 'ни одного'}.")


def _master(conn, name) -> sqlite3.Row:
    rows = conn.execute("SELECT * FROM staff WHERE role = 'master' AND active = 1 ORDER BY name").fetchall()
    found = [m for m in rows if name and matches(str(name), m["name"])]
    if len(found) != 1:
        raise Refused("Какой мастер? Есть: " + ", ".join(m["name"] for m in rows) + ".")
    return found[0]


def _product(conn, ctx, query) -> tuple[sqlite3.Row, int | None]:
    """(product, batch_id) — batch_id names the unit when an IMEI was given
    or the serial product has exactly one unit on hand."""
    units = [l for l in _inventory.stock_lines(conn, location_id=ctx["location_id"]) if l["imei"] and l["warehouse_kind"] == "point"]
    digits = _digits(query)
    if len(digits) >= 8:
        unit = next((l for l in units if l["imei"] == digits), None)
        if unit:
            return _inventory.get_product(conn, unit["product_id"]), unit["batch_id"]
    products = [p for p in _inventory.list_products_with_stock(conn, location_id=ctx["location_id"])
                if query and matches(str(query), p["name"], p["sku"])]
    in_stock = [p for p in products if p["total_qty"] > 0]
    found = in_stock or products
    if len(found) != 1:
        if found:
            raise Refused("Таких товаров несколько: " + "; ".join(f"{p['name']} ({p['total_qty']} шт)" for p in found[:8]) + ". Уточните название.")
        raise Refused(f"Товара «{query}» в каталоге нет.")
    product = found[0]
    if product["is_serial"]:
        own = [l for l in units if l["product_id"] == product["id"] and l["qty"] - l["reserved"] > 0]
        if len(own) != 1:
            raise Refused(f"«{product['name']}» — по IMEI. " + ("На складе: " + ", ".join(l["imei"] for l in own[:8]) + ". Назовите IMEI." if own else "На складе точки его нет."))
        return product, own[0]["batch_id"]
    return product, None


def _need_shift(conn, ctx) -> None:
    if not _shifts.current_shift(conn, ctx["staff"]["id"], ctx["location_id"]):
        raise Refused("Смена сегодня не открыта. Сначала откройте её — действие open_shift (человеку достаточно сказать «открой смену»), потом повторите.")


def open_shift(conn, ctx, a) -> dict:
    """«Открой смену»: the day's shift, with the balances it opens on in
    the receipt — the same «сверить остатки» the button shows. A
    difference the person names goes on record as «расхождение»."""
    if _shifts.current_shift(conn, ctx["staff"]["id"], ctx["location_id"]):
        return {"done": True, "note": "смена сегодня уже открыта"}
    note = " ".join(str(a.get("discrepancy") or "").split()) or None
    _shifts.open_shift(conn, ctx["staff"]["id"], ctx["location_id"], note)
    balances = [f"{html.escape(b['name'])}: {b['balance']} {b['currency']}" for b in _accounts.balances(conn, ctx["location_id"]) if b["balance"]]
    _receipt(ctx, "✅ Смена открыта" + (f" · расхождение: {html.escape(note)}" if note else " · остатки приняты как есть")
             + ("\n" + " · ".join(balances) if balances else "\nНа счетах точки пусто."))
    return {"done": True}


def _doc(conn, doc_type: str, ref_table: str, ref_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM documents WHERE doc_type = ? AND ref_table = ? AND ref_id = ?", (doc_type, ref_table, ref_id)
    ).fetchone()


def _receipt(ctx, text: str, undo: tuple | None = None, sync_repair: int | None = None, after: tuple | None = None) -> None:
    ctx["receipts"].append({"text": text, "undo": undo, "sync_repair": sync_repair, "after": after})


def _low(conn, account: sqlite3.Row) -> str:
    """A line for the receipt when money just left an account that didn't
    have it — the books allow it (as the Mini App does), the person
    should know."""
    left = _accounts.balance(conn, account["id"])
    return f"\n⚠️ На счёте «{html.escape(account['name'])}» теперь {left} грн — меньше нуля." if left < 0 else ""


def _doc_receipt(ctx, doc: sqlite3.Row | None, text: str, **kw) -> dict:
    label = _documents.doc_label(doc) if doc else ""
    _receipt(ctx, f"✅ <b>{label}</b> {text}".replace("  ", " "), undo=("doc", doc["id"]) if doc else None, **kw)
    return {"done": True, "document": label}


# ---- the actions ----

def new_repair(conn, ctx, a) -> dict:
    client = _client(conn, a.get("client_phone"), a.get("client_name"), create=True)
    device = " ".join(str(a.get("device") or "").split())
    if not device:
        raise Refused("Какое устройство принимаем? Назовите модель.")
    price = _amount(a["price"], "цену") if a.get("price") not in (None, "") else None
    master = _master(conn, a["master"]) if a.get("master") else None
    order_id, card_text, keyboard, photo = _repairs.create_repair_intake(
        conn, client_name=client["name"], client_phone=client["phone"], device_type=str(a.get("device_type") or "Телефон"),
        brand=None, model=device, serial_number=None, defect_description=" ".join(str(a.get("defect") or "").split()) or None,
        channel="offline", master_id=master["id"] if master else None, price_estimate=int(price) if price else None,
        staff_id=ctx["staff"]["id"], photo=None, location_id=ctx["location_id"],
    )
    label = _documents.label("repair", order_id)
    _receipt(
        ctx, f"✅ <b>{label}</b> принят: {html.escape(device)}" + (f" · {price} грн" if price else "")
        + f" · клиент {html.escape(client['phone'] or client['name'])}" + (f" · мастер {html.escape(master['name'])}" if master else ""),
        undo=("repair_new", order_id), after=("notify_repair", order_id, card_text, keyboard),
    )
    return {"done": True, "document": label}


def set_repair_price(conn, ctx, a) -> dict:
    repair = _repair(conn, ctx, a.get("repair"))
    price = 0 if str(a.get("price")).strip() in ("0", "0.0") else _amount(a.get("price"), "цену")
    old = _repairs.change_price(conn, repair["id"], price, ctx["staff"]["id"])
    label = _documents.label("repair", repair["id"])
    _receipt(ctx, f"✅ <b>{label}</b>: цена {'—' if old is None else old} → <b>{price} грн</b>", undo=("price", repair["id"], old or 0), sync_repair=repair["id"])
    return {"done": True, "document": label}


def assign_repair_master(conn, ctx, a) -> dict:
    repair, master = _repair(conn, ctx, a.get("repair")), _master(conn, a.get("master"))
    _repairs.assign_master(conn, repair["id"], master["id"])
    label = _documents.label("repair", repair["id"])
    _receipt(ctx, f"✅ <b>{label}</b>: мастер — {html.escape(master['name'])}", undo=("master", repair["id"], repair["master_id"] or 0), sync_repair=repair["id"])
    return {"done": True, "document": label}


def repair_without_parts(conn, ctx, a) -> dict:
    repair = _repair(conn, ctx, a.get("repair"))
    _repairs.declare_no_parts(conn, repair["id"])
    label = _documents.label("repair", repair["id"])
    _receipt(ctx, f"✅ <b>{label}</b>: отмечено «без запчасти»", sync_repair=repair["id"])
    return {"done": True, "document": label}


def add_repair_note(conn, ctx, a) -> dict:
    repair = _repair(conn, ctx, a.get("repair"))
    text = " ".join(str(a.get("text") or "").split())
    if not text:
        raise Refused("Что записать в заметку?")
    _notes.add_note(conn, repair["id"], "text", text, text if len(text) <= 200 else None, staff_id=ctx["staff"]["id"])
    label = _documents.label("repair", repair["id"])
    _receipt(ctx, f"✅ <b>{label}</b>: заметка записана", sync_repair=repair["id"])
    return {"done": True, "document": label}


def clear_repair_notes(conn, ctx, a) -> dict:
    repair = _repair(conn, ctx, a.get("repair"))
    hidden = _notes.hide_from_card(conn, repair["id"])
    label = _documents.label("repair", repair["id"])
    _receipt(ctx, f"🧹 <b>{label}</b>: заметки убраны с карточки ({hidden})" if hidden else f"<b>{label}</b>: заметок на карточке и так нет",
             sync_repair=repair["id"])
    return {"done": True, "document": label}


def add_expense(conn, ctx, a) -> dict:
    _need_shift(conn, ctx)
    amount, account = _amount(a.get("amount")), _account(conn, ctx, a.get("account"))
    category = a.get("category") if a.get("category") in _cash.EXPENSE_CATEGORIES else "other"
    comment = " ".join(str(a.get("comment") or "").split()) or None
    transaction_id = _cash.record_expense(
        conn, "cash", amount, category, comment, ctx["staff"]["id"], location_id=ctx["location_id"], account_id=account["id"],
    )
    return _doc_receipt(
        ctx, _doc(conn, "cash_out", "cash_transactions", transaction_id),
        f"расход {amount} грн · {_cash.EXPENSE_CATEGORIES[category]} · {html.escape(account['name'])}" + (f" · {html.escape(comment)}" if comment else "")
        + _low(conn, account),
    )


def cash_correction(conn, ctx, a) -> dict:
    _need_shift(conn, ctx)
    amount, account = _amount(a.get("amount")), _account(conn, ctx, a.get("account"))
    direction = "out" if a.get("direction") == "out" else "in"
    comment = " ".join(str(a.get("comment") or "").split())
    if not comment:
        raise Refused("Укажите причину корректировки кассы — без неё она не проводится.")
    transaction_id = _cash.record_adjustment(
        conn, amount if direction == "in" else -amount, comment, ctx["staff"]["id"], location_id=ctx["location_id"], account_id=account["id"],
    )
    return _doc_receipt(
        ctx, _doc(conn, "cash_adjust", "cash_transactions", transaction_id),
        f"{'внесено' if direction == 'in' else 'изъято'} {amount} грн · {html.escape(account['name'])} · {html.escape(comment)}" + _low(conn, account),
    )


def sell(conn, ctx, a) -> dict:
    _need_shift(conn, ctx)
    product, batch_id = _product(conn, ctx, a.get("product"))
    qty = 1 if batch_id else _qty(a.get("qty"))
    price = _amount(a["price"], "цену") if a.get("price") not in (None, "") else product["price"]
    if not price:
        raise Refused(f"У «{product['name']}» нет цены в карточке — назовите цену продажи.")
    client = _client(conn, a.get("client_phone"), a.get("client_name"), create=True) if (a.get("client_phone") or a.get("client_name")) else None
    in_debt = str(a.get("account") or "").casefold() in ("debt", "долг", "в долг")
    if in_debt and not client:
        raise Refused("В долг — только клиенту: назовите его номер телефона.")
    total = money(qty * price)
    account = None if in_debt else _account(conn, ctx, a.get("account"))
    sale_id = _sales.create_sale(
        conn, client["id"] if client else None, "offline", ctx["staff"]["id"], [(product["id"], qty, price, batch_id)],
        location_id=ctx["location_id"], payments=[] if in_debt else [(account["id"], total, None)], allow_debt=in_debt,
    )
    return _doc_receipt(
        ctx, _documents.get_for(conn, "sale", sale_id),
        f"продажа: {html.escape(product['name'])}" + (f" × {qty}" if qty > 1 else "") + f" — {total} грн · "
        + ("в долг" if in_debt else html.escape(account["name"])) + (f" · {html.escape(client['name'])}" if client else ""),
    ) | {"sold_product_id": product["id"]}


def client_money(conn, ctx, a) -> dict:
    _need_shift(conn, ctx)
    direction = "out" if a.get("direction") == "out" else "in"
    if direction == "out" and ctx["staff"]["role"] not in _BOSS:
        raise Refused("Выдать деньги контрагенту может владелец или админ.")
    client = _client(conn, a.get("client_phone"), a.get("client_name"))
    amount, account = _amount(a.get("amount")), _account(conn, ctx, a.get("account"))
    move = _settlements.receive_money if direction == "in" else _settlements.pay_out_money
    entry_id = move(conn, client["id"], [(account["id"], amount, None)], staff_id=ctx["staff"]["id"],
                    location_id=ctx["location_id"], comment=" ".join(str(a.get("comment") or "").split()) or None)
    position = _settlements.client_position(conn, client["id"])
    balance = f"нам должен {position['they_owe']} грн" if position["they_owe"] else (f"мы должны {position['we_owe']} грн" if position["we_owe"] else "взаиморасчёты 0")
    return _doc_receipt(
        ctx, _doc(conn, "cash_in" if direction == "in" else "cash_out", "client_ledger", entry_id),
        f"{'принято от' if direction == 'in' else 'выдано'} {html.escape(client['name'])}: {amount} грн · {html.escape(account['name'])} · теперь {balance}" + _low(conn, account),
    )


def master_payout(conn, ctx, a) -> dict:
    _need_shift(conn, ctx)
    master = _master(conn, a.get("master"))
    amount, account = _amount(a.get("amount")), _account(conn, ctx, a.get("account"))
    payout_id = _masters.pay_out(conn, master["id"], [(account["id"], amount, None)], paid_by=ctx["staff"]["id"],
                                 location_id=ctx["location_id"], comment=" ".join(str(a.get("comment") or "").split()) or None)
    return _doc_receipt(
        ctx, _doc(conn, "cash_out", "master_payouts", payout_id),
        f"выплата мастеру {html.escape(master['name'])}: {amount} грн · {html.escape(account['name'])} · осталось должны {_masters.owed(conn, master['id'])} грн" + _low(conn, account),
    )


def stock_write_off(conn, ctx, a) -> dict:
    product, batch_id = _product(conn, ctx, a.get("product"))
    if batch_id or product["is_serial"]:
        raise Refused("Устройства по IMEI списываются в приложении — там выбирается конкретная единица.")
    qty, comment = _qty(a.get("qty")), " ".join(str(a.get("comment") or "").split())
    if not comment:
        raise Refused("Укажите причину списания.")
    cell_id = _inventory.pick_cell_with_stock(conn, product["id"], qty, ctx["location_id"])
    if cell_id is None:
        raise Refused(f"«{product['name']}»: на складе точки нет {qty} шт свободного остатка.")
    movement_id = _inventory.write_off_stock(conn, product["id"], cell_id, qty, ctx["staff"]["id"], comment=comment)
    return _doc_receipt(ctx, _doc(conn, "writeoff", "stock_movements", movement_id), f"списано: {html.escape(product['name'])} × {qty} · {html.escape(comment)}")


def stock_add(conn, ctx, a) -> dict:
    product, _batch = _product(conn, ctx, a.get("product"))
    if product["is_serial"]:
        raise Refused("Устройства по IMEI приходуются в приложении или через «🛒 Покупка» — нужен IMEI.")
    qty = _qty(a.get("qty"))
    lines = [l for l in _inventory.stock_lines(conn, location_id=ctx["location_id"], product_id=product["id"]) if l["warehouse_kind"] == "point"]
    cells = _inventory.list_cells(conn, ctx["location_id"])
    cell_id = lines[0]["cell_id"] if lines else (cells[0]["id"] if cells else None)
    if cell_id is None:
        raise Refused("У точки нет ни одной ячейки склада — создайте её в приложении.")
    movement_id = _inventory.receive_stock(conn, product["id"], cell_id, qty, ctx["staff"]["id"],
                                           comment=" ".join(str(a.get("comment") or "").split()) or "оприходование из чата")
    return _doc_receipt(ctx, _doc(conn, "stock_in", "stock_movements", movement_id), f"оприходовано: {html.escape(product['name'])} × {qty}")


def reserve_for_client(conn, ctx, a) -> dict:
    product, batch_id = _product(conn, ctx, a.get("product"))
    client = _client(conn, a.get("client_phone"), a.get("client_name"), create=True)
    qty = 1 if batch_id else _qty(a.get("qty"))
    price = _amount(a["price"], "цену") if a.get("price") not in (None, "") else product["price"]
    if not price:
        raise Refused(f"У «{product['name']}» нет цены в карточке — назовите цену.")
    order_id = _orders.create_order(conn, client["id"], [(product["id"], qty, price, batch_id)], ctx["staff"]["id"], location_id=ctx["location_id"])
    return _doc_receipt(
        ctx, _documents.get_for(conn, "client_order", order_id),
        f"отложено на 24 часа: {html.escape(product['name'])}" + (f" × {qty}" if qty > 1 else "") + f" — {money(qty * price)} грн · {html.escape(client['name'])}",
    )


def add_client(conn, ctx, a) -> dict:
    normalized = _clients.normalize_phone(str(a.get("phone") or ""))
    name = " ".join(str(a.get("name") or "").split())
    if not normalized or not name:
        raise Refused("Нужны имя и номер телефона клиента.")
    existing = _clients.get_by_phone(conn, normalized)
    if existing:
        raise Refused(f"Клиент с номером {normalized} уже есть: {existing['name']}.")
    _clients.create_client(conn, name=name, phone=normalized, source="offline")
    _receipt(ctx, f"✅ Клиент записан: {html.escape(name)} · {normalized}")
    return {"done": True}


_PREFIXES = {prefix.upper(): doc_type for doc_type, (prefix, _name) in _documents.DOC_TYPES.items()}


def cancel_document(conn, ctx, a) -> dict:
    reason = " ".join(str(a.get("reason") or "").split())
    match = re.fullmatch(r"\s*([А-ЯA-Zа-яa-z]{2,3})\s*-?\s*0*(\d+)\s*", str(a.get("number") or ""))
    names_one = (match and match.group(1).upper() in _PREFIXES) or a.get("last_of_type") in _documents.DOC_TYPES
    if names_one and not reason:
        raise Refused("Укажите причину отмены — без неё документ не отменяется.")
    if match and match.group(1).upper() in _PREFIXES:
        prefix, number = match.group(1).upper(), int(match.group(2))
        docs = conn.execute("SELECT * FROM documents WHERE number = ? AND doc_type IN ({})".format(
            ",".join("?" for _ in _documents.DOC_TYPES)), [number, *_documents.DOC_TYPES]).fetchall()
        docs = [d for d in docs if _documents.DOC_TYPES[d["doc_type"]][0].upper() == prefix]
        if len(docs) != 1:
            raise Refused(f"Документ {prefix}-{number:03d} не найден." if not docs else f"{prefix}-{number:03d} — таких документов несколько, отмените в журнале приложения.")
    elif a.get("last_of_type") in _documents.DOC_TYPES:
        # «удали ту продажу, это был тест» — the latest live document of
        # that kind at this точка; several to choose from are listed, not guessed.
        live = conn.execute(
            "SELECT * FROM documents WHERE doc_type = ? AND status = 'posted' AND location_id = ? ORDER BY created_at DESC, id DESC LIMIT 5",
            (a["last_of_type"], ctx["location_id"]),
        ).fetchall()
        if not live:
            raise Refused(f"Проведённых документов «{_documents.type_name(a['last_of_type'])}» нет.")
        if len(live) > 1:
            listing = "; ".join(f"{_documents.doc_label(d)} от {kyiv_datetime(d['created_at'])}" + (f" на {d['amount']} грн" if d["amount"] else "") for d in live)
            raise Refused(f"Таких документов несколько: {listing}. Назовите номер того, который отменить.")
        docs = live[:1]
    else:
        raise Refused("Какой документ отменить? Назовите номер, например ПД-12 или РКО-3.")
    try:
        _doc_cancel.cancel_document(conn, docs[0]["id"], ctx["staff"]["id"], reason)
    except _documents.DocumentError as exc:
        raise Refused(str(exc)) from exc
    _receipt(ctx, f"✅ <b>{_documents.doc_label(docs[0])}</b> отменён · причина: {html.escape(reason)}")
    return {"done": True, "document": _documents.doc_label(docs[0])}


# name -> (function, roles, description, properties, required)
_S, _N = {"type": "string"}, {"type": "number"}
_ACCOUNT = {"type": "string", "description": "счёт: «наличные», «карта», «фоп»… (не сказано — наличные)"}
_CLIENT = {"client_phone": {"type": "string", "description": "телефон клиента, если назван"},
           "client_name": {"type": "string", "description": "имя клиента — достаточно его одного, если телефон не назвали"}}
_REPAIR = {"type": "string", "description": "какой ремонт: номер («РК-48», «48») или модель/клиент («13 про мах», «поко»)"}
_PRODUCT = {"type": "string", "description": "товар: название или его часть, артикул либо IMEI"}

ACTIONS: dict[str, tuple] = {
    "new_repair": (new_repair, _INTAKE, "Принять новый ремонт у клиента. Нужны телефон клиента и модель устройства; неисправность, цена и мастер — если названы.",
        {**_CLIENT, "device": {"type": "string", "description": "модель, как сказали: «iPhone 13 Pro»"}, "device_type": {"type": "string", "description": "Телефон, Ноутбук, Планшет…"},
         "defect": _S, "price": _N, "master": _S}, ["client_phone", "device"]),
    "set_repair_price": (set_repair_price, _INTAKE, "Поставить или изменить цену ремонта (полная цена для клиента).", {"repair": _REPAIR, "price": _N}, ["repair", "price"]),
    "assign_repair_master": (assign_repair_master, _INTAKE, "Назначить мастера на ремонт.", {"repair": _REPAIR, "master": _S}, ["repair", "master"]),
    "repair_without_parts": (repair_without_parts, _INTAKE, "Отметить, что ремонт сделан без запчасти.", {"repair": _REPAIR}, ["repair"]),
    "add_repair_note": (add_repair_note, _EVERYONE, "Записать заметку в карточку ремонта.", {"repair": _REPAIR, "text": _S}, ["repair", "text"]),
    "clear_repair_notes": (clear_repair_notes, _EVERYONE, "Убрать блок заметок с карточки ремонта в чате (заметки остаются на странице ремонта в приложении).",
        {"repair": _REPAIR}, ["repair"]),
    "add_expense": (add_expense, _CASH, "Записать расход из кассы (аренда, зарплата, закупка, прочее).",
        {"amount": _N, "category": {"type": "string", "enum": list(_cash.EXPENSE_CATEGORIES)}, "comment": {"type": "string", "description": "на что"}, "account": _ACCOUNT}, ["amount"]),
    "cash_correction": (cash_correction, _BOSS, "Внести деньги в кассу или изъять из неё (корректировка, не расход). Нужна причина.",
        {"direction": {"type": "string", "enum": ["in", "out"]}, "amount": _N, "comment": {"type": "string", "description": "причина"}, "account": _ACCOUNT}, ["direction", "amount", "comment"]),
    "sell": (sell, _EVERYONE, "Продать товар со склада точки. account — куда оплата, или «в долг» (тогда нужен клиент).",
        {"product": _PRODUCT, "qty": {"type": "integer"}, "price": {"type": "number", "description": "цена за штуку; не названа — из карточки товара"},
         **_CLIENT, "account": {"type": "string", "description": "«наличные», «карта»… или «в долг»"}}, ["product"]),
    "client_money": (client_money, _CASH, "Принять деньги от клиента (in — он гасит долг / вносит аванс) или выдать ему (out — возврат аванса; только владелец/админ).",
        {"direction": {"type": "string", "enum": ["in", "out"]}, **_CLIENT, "amount": _N, "account": _ACCOUNT, "comment": _S}, ["direction", "amount"]),
    "master_payout": (master_payout, _BOSS, "Выплатить мастеру деньги из кассы.", {"master": _S, "amount": _N, "account": _ACCOUNT, "comment": _S}, ["master", "amount"]),
    "stock_write_off": (stock_write_off, _CASH, "Списать товар со склада точки (брак, потеря). Нужна причина.", {"product": _PRODUCT, "qty": {"type": "integer"}, "comment": _S}, ["product", "qty", "comment"]),
    "stock_add": (stock_add, _CASH, "Оприходовать товар на склад точки без накладной (нашли, пересчёт).", {"product": _PRODUCT, "qty": {"type": "integer"}, "comment": _S}, ["product", "qty"]),
    "reserve_for_client": (reserve_for_client, _EVERYONE, "Отложить товар за клиентом на 24 часа (заказ с резервом).",
        {"product": _PRODUCT, "qty": {"type": "integer"}, "price": _N, **_CLIENT}, ["product", "client_phone"]),
    "add_client": (add_client, _EVERYONE, "Записать нового клиента.", {"name": _S, "phone": _S}, ["name", "phone"]),
    "cancel_document": (cancel_document, _BOSS,
        "Отменить (удалить) проведённый документ: продажу, расход, корректировку… Нужна причина («это был тест» — причина). "
        "Номер назван — number («ПД-12»). Не назван («удали ту продажу») — last_of_type с типом документа.",
        {"number": _S, "last_of_type": {"type": "string", "enum": list(_doc_cancel.CANCELLABLE_TYPES)}, "reason": _S}, ["reason"]),
    "open_shift": (open_shift, _EVERYONE, "Открыть смену на сегодня (нужна для операций с деньгами). discrepancy — если человек назвал расхождение в остатках.",
        {"discrepancy": _S}, []),
}


def available(staff) -> dict[str, tuple]:
    """Actions this person may ask for: none for someone who isn't staff in the CRM."""
    if not staff:
        return {}
    return {name: spec for name, spec in ACTIONS.items() if staff["role"] in spec[1]}


def schemas(staff) -> list[dict]:
    return [
        {"type": "function", "function": {"name": name, "description": description,
                                          "parameters": {"type": "object", "properties": properties, "required": required}}}
        for name, (_fn, _roles, description, properties, required) in available(staff).items()
    ]


def run(conn: sqlite3.Connection, ctx: dict, name: str, args: dict) -> dict:
    """Carry one action out — whole, or not at all. A refusal comes back
    as {"refused": …} for the model to pass on; the same request repeated
    within one conversation turn is not carried out twice."""
    spec = available(ctx.get("staff")).get(name)
    if not spec:
        return {"refused": "Это действие вам недоступно." if name in ACTIONS else "Такого действия нет."}
    args = args if isinstance(args, dict) else {}
    fingerprint = (name, repr(sorted(args.items(), key=lambda kv: kv[0])))
    if fingerprint in ctx["done"]:
        return {"refused": "Это уже сделано только что — второй раз не выполняю."}
    # A savepoint opened outside a transaction IS the transaction, and
    # releasing it commits — the action would then outlive a request that
    # fails later (seen in tests). So the request's transaction is opened
    # first; the savepoint only marks where this one action began.
    if not conn.in_transaction:
        conn.execute("BEGIN")
    conn.execute("SAVEPOINT agent_action")
    try:
        result = spec[0](conn, ctx, args)
    except Refused as exc:
        conn.execute("ROLLBACK TO agent_action")
        conn.execute("RELEASE agent_action")
        return {"refused": str(exc)}
    except (_cash.PaymentError, _sales.SaleError, _inventory.InsufficientStockError, _orders.OrderError,
            _settlements.SettlementError, _masters.PayoutError, _repairs.RepairPartError, _accounts.AccountError,
            ValueError, TypeError, KeyError, sqlite3.IntegrityError) as exc:
        conn.execute("ROLLBACK TO agent_action")
        conn.execute("RELEASE agent_action")
        return {"refused": str(exc) or "не получилось"}
    conn.execute("RELEASE agent_action")
    ctx["done"].add(fingerprint)
    return result


def undo(conn: sqlite3.Connection, what: tuple, staff) -> str:
    """«Вернуть» under a receipt. Returns the line to show; raises Refused."""
    kind = what[0]
    if kind == "doc":
        doc = _documents.get(conn, int(what[1]))
        if not doc:
            raise Refused("Документ не найден.")
        if not staff or (staff["role"] not in _BOSS and doc["staff_id"] != staff["id"]):
            raise Refused("Вернуть может тот, кто это сделал, либо владелец или админ.")
        try:
            _doc_cancel.cancel_document(conn, doc["id"], staff["id"], "отмена из чата")
        except _documents.DocumentError as exc:
            raise Refused(str(exc)) from exc
        return f"↩️ <b>{_documents.doc_label(doc)}</b> отменён."
    if not staff:
        raise Refused("Вернуть может сотрудник, подключённый к CRM.")
    order_id = int(what[1])
    label = _documents.label("repair", order_id)
    if kind == "repair_new":
        repair = _repairs.get_repair(conn, order_id)
        if not repair or repair["status"] != "new":
            raise Refused("Этот ремонт уже в работе — отменять его так поздно.")
        _repairs.update_status(conn, order_id, "cancelled", staff["id"], "принят по ошибке (отмена из чата)")
        return f"↩️ <b>{label}</b> отменён."
    if kind == "price":
        try:
            _repairs.change_price(conn, order_id, money(float(what[2])), staff["id"])
        except _repairs.RepairPartError as exc:
            raise Refused(str(exc)) from exc
        return f"↩️ <b>{label}</b>: цена возвращена — {what[2]} грн."
    if kind == "master":
        _repairs.assign_master(conn, order_id, int(what[2]) or None)
        return f"↩️ <b>{label}</b>: мастер возвращён прежний."
    raise Refused("Это уже не вернуть.")
