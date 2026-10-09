"""What the bot's AI assistant (core.ai_agent) can look at and do — the
whole of it. The model never touches the base: it asks for one of these
functions by name and gets back plain data.

Every tool here READS. The two exceptions are not writes to the books
either: «send_repair_card» and «show_open_repairs» ask the bot to post
something into the chat the question came from (the bot does that, see
bot/assistant_chat.py). The model is given no way to change a status, a
price, money or stock: on a live trial (10.10) it turned «11 про готов»
into «выдан». A status said to the bot is handled before the model is
asked at all — bot/assistant_chat.py::_status_said.

A tool sees one точка — the one the chat belongs to — unless the person
asking may see the business's money (owner/admin) and asks for all of
them. Money tools (касса, прибыль, долги, выплаты мастерам, журнал) exist
only for those people: they are not even offered to the model otherwise.
"""
from __future__ import annotations

import re
import sqlite3

from core import accounts as _accounts
from core import cash as _cash
from core import documents as _documents
from core import inventory as _inventory
from core import masters as _masters
from core import orders as _orders
from core import overview as _overview
from core import repair_digest as _digest
from core import repair_notes as _notes
from core import repairs as _repairs
from core import sales as _sales
from core import settlements as _settlements
from core.timefmt import kyiv_date_range_utc, kyiv_datetime, kyiv_today, ru_date

MAX_ROWS = 25
MAX_CARDS = 5

# How people type models in chat vs. how they are written on the card:
# both sides of a comparison go through this, so «13 айфон про макс»
# finds «iPhone 13 Pro Max» and «13 про мах» alike.
_SYNONYMS = {
    "айфон": "iphone", "айфона": "iphone", "айфону": "iphone", "айфоны": "iphone", "iphon": "iphone",
    "самсунг": "samsung", "самсунга": "samsung", "галакси": "galaxy", "сяоми": "xiaomi", "ксяоми": "xiaomi",
    "ксиоми": "xiaomi", "редми": "redmi", "поко": "poco", "хонор": "honor", "хуавей": "huawei",
    "реалми": "realme", "оппо": "oppo", "пиксель": "pixel", "нокиа": "nokia", "моторола": "motorola",
    "про": "pro", "макс": "max", "мах": "max", "плюс": "plus", "мини": "mini", "ультра": "ultra",
    "ноут": "note", "нот": "note", "лайт": "lite", "эпл": "apple", "айпад": "ipad", "макбук": "macbook",
}


def _tokens(text) -> list[str]:
    cleaned = "".join(ch if ch.isalnum() else " " for ch in str(text or "").casefold())
    return [_SYNONYMS.get(token, token) for token in cleaned.split()]


def matches(query: str, *fields) -> bool:
    """Every word of the query is found somewhere in the fields — as a
    word start («13» finds «13 pro», «poc» finds «poco»), after the
    spelling of both sides has been evened out."""
    haystack = _tokens(" ".join(str(f) for f in fields if f))
    return all(any(word.startswith(token) for word in haystack) for token in _tokens(query))


def _digits(text) -> str:
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def _period(args: dict) -> tuple[str, str, str, str]:
    today = kyiv_today()
    date_from, date_to = args.get("date_from") or today, args.get("date_to") or today
    try:
        start, end = kyiv_date_range_utc(date_from, date_to)
    except ValueError:
        date_from = date_to = today
        start, end = kyiv_date_range_utc(today, today)
    return date_from, date_to, start, end


def _period_text(date_from: str, date_to: str) -> str:
    """A period as people read it — дд.мм.гггг, never the ISO form the tools take as arguments."""
    return ru_date(date_from) if date_from == date_to else f"{ru_date(date_from)} — {ru_date(date_to)}"


def _scope(ctx: dict, args: dict) -> int | None:
    return None if (args.get("all_points") and ctx["can_money"]) else ctx["location_id"]


def _repair_row(conn: sqlite3.Connection, ctx: dict, row) -> dict:
    links = _digest._links(conn, [row["id"]], ctx.get("chat_id"), ctx.get("topics") or {})
    return {
        "id": row["id"], "number": _documents.label("repair", row["id"]),
        "device": " ".join(filter(None, [row["device_type"], row["brand"], row["model"]])),
        "status": _repairs.STATUS_LABELS[row["status"]], "stage": row["stage_note"],
        "price_uah": _repairs.current_price(row), "master": row["master_name"], "client": row["client_name"],
        "accepted": kyiv_datetime(row["created_at"]), "card_link": links.get(row["id"]),
    }


# ---- ремонты ----

def _score(query: str, *fields) -> int:
    """How many words of the query are found in the fields."""
    haystack = _tokens(" ".join(str(f) for f in fields if f))
    return sum(any(word.startswith(token) for word in haystack) for token in _tokens(query))


def find_repairs(conn, ctx, args) -> dict:
    query, status = args.get("query") or "", args.get("status")
    found, partial = [], []
    for row in _repairs.list_repairs(conn, location_id=_scope(ctx, args)):
        if status and row["status"] != status:
            continue
        if not args.get("include_closed") and not status and row["status"] in ("issued", "cancelled"):
            continue
        phone = conn.execute("SELECT phone FROM clients WHERE id = ?", (row["client_id"],)).fetchone()["phone"]
        number_asked = _digits(query)
        by_number = number_asked and query.strip().upper().startswith(("РК", "PK", "№")) and int(number_asked) == row["id"]
        by_phone = len(number_asked) >= 6 and number_asked[-9:] in _digits(phone)
        fields = (row["device_type"], row["brand"], row["model"], row["client_name"])
        if by_number or by_phone or (query and matches(query, *fields)) or not query:
            found.append(row)
        elif query and _score(query, *fields):
            partial.append((_score(query, *fields), row))
    if found or not partial:
        ctx["last_found"] = [r["id"] for r in found]
        return {"total": len(found), "repairs": [_repair_row(conn, ctx, r) for r in found[:MAX_ROWS]]}
    # Cards are filled in by hand — «13 про мах» with no brand is an
    # iPhone to everyone in the shop. Nothing matched every word, so the
    # closest ones go back, marked as such, for the model to weigh.
    best = max(score for score, _row in partial)
    close = [row for score, row in partial if score == best]
    ctx["last_found"] = [r["id"] for r in close]
    return {"total": 0, "exact_match": False,
            "note": "точного совпадения нет; это ближайшие по части слов — карточки заполняют от руки, бренд часто не пишут",
            "closest": [_repair_row(conn, ctx, r) for r in close[:MAX_ROWS]]}


def get_repair(conn, ctx, args) -> dict:
    row = _repairs.get_repair(conn, int(args.get("repair_id") or 0))
    if not row or (ctx["location_id"] and row["location_id"] != ctx["location_id"] and not ctx["can_money"]):
        return {"error": "ремонт не найден"}
    ctx["last_found"] = [row["id"]]
    result = _repair_row(conn, ctx, row)
    result.update({
        "client_phone": row["client_phone"], "defect": row["defect_description"], "serial": row["serial_number"],
        "parts": [{"name": p["product_name"], "qty": p["qty"]} for p in _repairs.get_used_parts(conn, row["id"])],
        "notes": [{"when": kyiv_datetime(n["created_at"]), "who": n["author"], "text": n["text"]}
                  for n in _notes.list_notes(conn, row["id"])][-15:],
    })
    if ctx["can_money"]:
        numbers = _repairs.finance(conn, row["id"])
        result["finance"] = {k: numbers[k] for k in ("price", "parts_cost", "master_share", "firm_profit")}
    return result


def show_open_repairs(conn, ctx, args) -> dict:
    """The ready-made list with clickable names goes to the chat as its
    own message — the bot sends it; the model only says a line with it."""
    statuses = tuple(s for s in (args.get("statuses") or []) if s in _digest.OPEN_STATUSES) or _digest.OPEN_STATUSES
    ctx["actions"].append(("open_repairs", statuses))
    rows = [r for r in _repairs.list_repairs(conn, location_id=ctx["location_id"]) if r["status"] in statuses]
    return {"sent_to_chat": True, "count": len(rows), "total_uah": sum(_repairs.current_price(r) or 0 for r in rows)}


def send_repair_card(conn, ctx, args) -> dict:
    repair_id = int(args.get("repair_id") or 0)
    row = _repairs.get_repair(conn, repair_id)
    if not row:
        return {"error": "ремонт не найден"}
    cards = [a for a in ctx["actions"] if a[0] == "repair_card"]
    if len(cards) >= MAX_CARDS:
        return {"error": f"за один раз — не больше {MAX_CARDS} карточек"}
    if ("repair_card", repair_id) not in ctx["actions"]:
        ctx["actions"].append(("repair_card", repair_id))
    return {"sent_to_chat": True, "number": _documents.label("repair", repair_id)}


_REPAIR_NUMBER = re.compile(r"(?:РК|PK|№)\s*-?\s*0*(\d+)", re.IGNORECASE)


# Words of a status report that say what happened, not which repair it
# happened to — they are set aside before the repair is looked for.
_NOT_A_NAME = frozenset(_tokens(
    "выдан выдана выдано выдал выдала выдали отдал отдала отдали забрал забрала забрали получил получила "
    "готов готова готово готовый сделал сделала сделан сделано починил починили закончил "
    "взял взяла взяли беру начал начала начинаю работу работе работа делаю "
    "не удалось починить чинится отказ отказался отказалась возврат вернул вернули "
    "клиент клиенту клиента оплатил оплатила оплатили оплата оплачено заплатил заплатили "
    "наличными наличные наличка нал налом кэш картой карта карту карте перевод переводом перевёл перевел терминал "
    "заказ заказа ремонт ремонта телефон телефона аппарат трубка трубку уже всё все и в на по за это его её ее грн гривен"
))


def name_in(text: str) -> str:
    """What is left of a status report once the words for the status and
    the payment are set aside — the part that names the repair."""
    return " ".join(token for token in _tokens(text) if token not in _NOT_A_NAME)


def already_there(conn: sqlite3.Connection, location_id: int | None, text: str, status: str) -> list:
    """Repairs the sentence names that are ALREADY in that status — so
    «11 про готов» about a repair marked ready an hour ago is answered
    «уже готов», not «не нашёл»."""
    numbered, name = _REPAIR_NUMBER.search(text), name_in(text)
    return [
        r for r in _repairs.list_repairs(conn, location_id=location_id, status=status)
        if (numbered and r["id"] == int(numbered.group(1)))
        or (not numbered and name and matches(name, r["device_type"], r["brand"], r["model"], r["client_name"]))
    ][:5]


def repairs_for_status(conn: sqlite3.Connection, location_id: int | None, text: str, new_status: str) -> tuple[list, list]:
    """Which repair a sentence like «13 про мах выдан, наличными» is
    about — (exact, close), both among repairs that CAN make that move.

    exact — «РК-48» names it outright; otherwise every word of the
    sentence that could be a name (what is left after the words for the
    status and the payment are set aside) is found on the repair's card.
    close — when nothing is exact: the repairs sharing the most of those
    words. A status is applied only to a single exact one; «close» is for
    showing and asking. No model here — picking the repair a status lands
    on must not be a guess («айфон 11 выдан» must not hand over the only
    open iPhone, a 13)."""
    rows = [r for r in _repairs.list_repairs(conn, location_id=location_id) if _repairs.chat_move_allowed(r["status"], new_status)]
    numbered = _REPAIR_NUMBER.search(text)
    if numbered:
        return [row for row in rows if row["id"] == int(numbered.group(1))], []
    name = name_in(text)
    if not name:
        return [], []
    fields = lambda r: (r["device_type"], r["brand"], r["model"], r["client_name"])  # noqa: E731
    exact = [r for r in rows if matches(name, *fields(r))]
    if exact:
        return exact, []
    scored = [(_score(name, *fields(r)), r) for r in rows]
    best = max((score for score, _row in scored), default=0)
    return [], ([row for score, row in scored if score == best] if best else [])


# ---- клиенты, товар, заказы ----

def find_clients(conn, ctx, args) -> dict:
    query = args.get("query") or ""
    digits = _digits(query)
    found = []
    for row in conn.execute("SELECT * FROM clients ORDER BY id DESC").fetchall():
        if (len(digits) >= 4 and digits[-9:] in _digits(row["phone"])) or (query and not digits and matches(query, row["name"])):
            position = _settlements.client_position(conn, row["id"])
            found.append({
                "id": row["id"], "name": row["name"], "phone": row["phone"],
                "repairs": conn.execute("SELECT COUNT(*) AS n FROM repair_orders WHERE client_id = ?", (row["id"],)).fetchone()["n"],
                "sales": conn.execute("SELECT COUNT(*) AS n FROM sales_orders WHERE client_id = ? AND status != 'cancelled'", (row["id"],)).fetchone()["n"],
                "owes_us_uah": position["they_owe"], "we_owe_uah": position["we_owe"],
            })
    return {"total": len(found), "clients": found[:MAX_ROWS]}


def client_history(conn, ctx, args) -> dict:
    client = conn.execute("SELECT * FROM clients WHERE id = ?", (int(args.get("client_id") or 0),)).fetchone()
    if not client:
        return {"error": "клиент не найден"}
    documents = [
        {"number": _documents.doc_label(d), "type": _documents.type_name(d["doc_type"]), "what": d["title"],
         "amount_uah": d["amount"], "status": "отменён" if d["status"] == "cancelled" else "проведён",
         "when": kyiv_datetime(d["created_at"])}
        for d in _documents.list_journal(conn, client_id=client["id"], limit=MAX_ROWS)
    ]
    position = _settlements.client_position(conn, client["id"])
    return {"name": client["name"], "phone": client["phone"], "owes_us_uah": position["they_owe"],
            "we_owe_uah": position["we_owe"], "documents": documents}


def find_products(conn, ctx, args) -> dict:
    query = args.get("query") or ""
    location_id = _scope(ctx, args)
    units: dict[int, list[str]] = {}
    for line in _inventory.stock_lines(conn, location_id=location_id):
        if line["imei"]:
            units.setdefault(line["product_id"], []).append(line["imei"])
    found = [
        {"name": p["name"], "sku": p["sku"], "in_stock": p["total_qty"], "price_uah": p["price"],
         "imei_in_stock": units.get(p["id"], [])[:10]}
        for p in _inventory.list_products_with_stock(conn, location_id=location_id)
        if not query or matches(query, p["name"], p["sku"]) or (_digits(query) and any(_digits(query) in i for i in units.get(p["id"], [])))
    ]
    if args.get("only_in_stock"):
        found = [p for p in found if p["in_stock"] > 0]
    return {"total": len(found), "products": found[:MAX_ROWS]}


def client_orders(conn, ctx, args) -> dict:
    _orders.expire_due(conn)
    rows = _orders.list_orders(conn, statuses=("reserved", "expired"), location_id=ctx["location_id"])
    return {"orders": [
        {"number": _documents.label("client_order", o["id"]), "client": o["client_name"], "phone": o["client_phone"],
         "total_uah": o["total"], "status": _orders.STATUS_LABELS[o["status"]], "reserved_until": kyiv_datetime(o["reserved_until"]),
         "items": [f"{i['product_name']} × {i['qty']}" for i in _orders.get_items(conn, o["id"])]}
        for o in rows[:MAX_ROWS]
    ]}


def problems(conn, ctx, args) -> dict:
    return {"waiting_on_a_person": [f"{p['count']} — {p['text']}" for p in _overview.problems(conn, _scope(ctx, args))]}


def activity(conn, ctx, args) -> dict:
    date_from, date_to, start, end = _period(args)
    numbers = _overview.activity(conn, start, end, _scope(ctx, args))
    return {"period": _period_text(date_from, date_to), "repairs_accepted": numbers["repairs_accepted"],
            "repairs_issued": numbers["repairs_issued"], "sales": numbers["sales"],
            "new_client_orders": numbers["orders_new"], "phones_bought": numbers["buyback_count"]}


# ---- деньги: только владелец и админ ----

def cash_state(conn, ctx, args) -> dict:
    date_from, date_to, start, end = _period(args)
    location_id = _scope(ctx, args)
    flow = _cash.period_summary(conn, start, end, location_id)
    return {
        "accounts": [{"name": a["name"], "currency": a["currency"], "balance": a["balance"]}
                     for a in _accounts.balances(conn, location_id)],
        "period": _period_text(date_from, date_to), "income_uah": flow["income_total"], "expense_uah": flow["expense_total"],
    }


def profit(conn, ctx, args) -> dict:
    date_from, date_to, start, end = _period(args)
    numbers = _overview.profit(conn, start, end, _scope(ctx, args))
    return {"period": _period_text(date_from, date_to), **{k: numbers[k] for k in (
        "sales_count", "sales_revenue", "sales_profit", "repairs_count", "repairs_revenue", "repairs_profit",
        "expenses_total", "writeoffs", "net")}}


def debts(conn, ctx, args) -> dict:
    standing = _overview.standing(conn, _scope(ctx, args))
    return {"clients_owe_us_uah": standing["they_owe"], "we_owe_clients_uah": standing["we_owe"],
            "we_owe_masters_uah": standing["masters_owed"], "stock_at_cost_uah": standing["stock_value"],
            "who_owes_us": [{"name": d["name"], "phone": d["phone"], "uah": d["balance"]} for d in standing["debtors"]],
            "whom_we_owe": [{"name": d["name"], "phone": d["phone"], "uah": -d["balance"]} for d in standing["creditors"]]}


def masters(conn, ctx, args) -> dict:
    rows = conn.execute("SELECT id, name, pay_type, pay_value FROM staff WHERE role = 'master' AND active = 1 ORDER BY name").fetchall()
    return {"masters": [
        {"name": m["name"], "rate": f"{m['pay_value']}%" if m["pay_type"] == "percent" else (f"{m['pay_value']} грн за ремонт" if m["pay_type"] else "не задана"),
         "accrued_uah": _masters.accrued_total(conn, m["id"]), "paid_uah": _masters.paid_total(conn, m["id"]),
         "we_owe_uah": _masters.owed(conn, m["id"]),
         "repairs_in_work": conn.execute("SELECT COUNT(*) AS n FROM repair_orders WHERE master_id = ? AND status = 'in_progress'", (m["id"],)).fetchone()["n"]}
        for m in rows
    ]}


def journal(conn, ctx, args) -> dict:
    date_from, date_to, start, end = _period(args)
    doc_type = args.get("doc_type") if args.get("doc_type") in _documents.DOC_TYPES else None
    rows = _documents.list_journal(conn, utc_start=start, utc_end=end, location_id=_scope(ctx, args), doc_type=doc_type, limit=MAX_ROWS)
    return {"period": _period_text(date_from, date_to), "documents": [
        {"number": _documents.doc_label(d), "type": _documents.type_name(d["doc_type"]), "what": d["title"] or d["client_name"],
         "amount_uah": d["amount"], "profit_uah": d["profit"], "by": d["staff_name"],
         "status": "отменён" if d["status"] == "cancelled" else "проведён", "when": kyiv_datetime(d["created_at"])}
        for d in rows
    ]}


def sales_list(conn, ctx, args) -> dict:
    date_from, date_to, start, end = _period(args)
    rows = [s for s in _sales.list_sales(conn, limit=1000, location_id=_scope(ctx, args), include_cancelled=False)
            if start <= s["created_at"] < end]
    return {"period": _period_text(date_from, date_to), "count": len(rows), "total_uah": sum(s["total"] for s in rows), "sales": [
        {"number": _documents.label("sale", s["id"]), "client": s["client_name"], "total_uah": s["total"],
         "items": [f"{i['product_name']} × {i['qty']}" for i in _sales.get_sale_items(conn, s["id"])],
         "when": kyiv_datetime(s["created_at"])}
        for s in rows[:MAX_ROWS]
    ]}


# name -> (function, money only?, description for the model, JSON-schema properties, required)
_STATUS_ENUM = {"type": "string", "enum": list(_repairs.STATUS_LABELS)}
_DATES = {
    "date_from": {"type": "string", "description": "начало периода, ГГГГ-ММ-ДД (по умолчанию сегодня)"},
    "date_to": {"type": "string", "description": "конец периода включительно, ГГГГ-ММ-ДД (по умолчанию сегодня)"},
}
_ALL = {"all_points": {"type": "boolean", "description": "по всем точкам сразу (только владельцу/админу)"}}

TOOLS: dict[str, tuple] = {
    "find_repairs": (find_repairs, False,
        "Найти ремонты по модели устройства, имени или телефону клиента, номеру РК. Без query — все. "
        "По умолчанию только невыданные; include_closed=true — и выданные с отменёнными.",
        {"query": {"type": "string", "description": "модель («iPhone 13»), имя, телефон или «РК-48»"},
         "status": _STATUS_ENUM, "include_closed": {"type": "boolean"}, **_ALL}, []),
    "get_repair": (get_repair, False, "Всё об одном ремонте: клиент, неисправность, запчасти, заметки из чата.",
        {"repair_id": {"type": "integer"}}, ["repair_id"]),
    "show_open_repairs": (show_open_repairs, False,
        "Отправить в чат готовый список невыданных ремонтов (устройство-ссылка, цена, статус). Используй, когда "
        "просят список/сколько ремонтов, все или с одним статусом.",
        {"statuses": {"type": "array", "items": {"type": "string", "enum": list(_digest.OPEN_STATUSES)}}}, []),
    "send_repair_card": (send_repair_card, False,
        "Прислать в этот чат карточку ремонта (с кнопками). Используй, когда просят прислать/показать/выставить заказ.",
        {"repair_id": {"type": "integer"}}, ["repair_id"]),
    "find_clients": (find_clients, False, "Найти клиента по имени или телефону: сколько ремонтов и покупок, долг.",
        {"query": {"type": "string"}}, ["query"]),
    "client_history": (client_history, False, "Документы клиента (ремонты, продажи, покупки) и взаиморасчёты.",
        {"client_id": {"type": "integer"}}, ["client_id"]),
    "find_products": (find_products, False, "Товары и запчасти на складе: остаток, цена, IMEI. Поиск по названию, артикулу, IMEI.",
        {"query": {"type": "string"}, "only_in_stock": {"type": "boolean"}, **_ALL}, []),
    "client_orders": (client_orders, False, "Заказы клиентов с резервом товара (отложено на 24 часа).", {}, []),
    "problems": (problems, False, "Что ждёт человека: ремонты без запчасти, непринятые перемещения, истёкшие резервы и т.п.",
        {**_ALL}, []),
    "activity": (activity, False, "Счётчики за период: принято и выдано ремонтов, продаж, покупок телефонов.",
        {**_DATES, **_ALL}, []),
    "cash_state": (cash_state, True, "Остатки по счетам кассы и приход/расход за период.", {**_DATES, **_ALL}, []),
    "profit": (profit, True, "Чистая прибыль за период: продажи, ремонты, расходы, списания.", {**_DATES, **_ALL}, []),
    "debts": (debts, True, "Кто должен нам, кому должны мы (клиенты и мастера), товар по себестоимости.", {**_ALL}, []),
    "masters": (masters, True, "Мастера: ставка, начислено, выплачено, долг, сколько ремонтов в работе.", {}, []),
    "journal": (journal, True, "Журнал документов за период, можно одного типа.",
        {**_DATES, "doc_type": {"type": "string", "enum": list(_documents.DOC_TYPES)}, **_ALL}, []),
    "sales_list": (sales_list, True, "Продажи за период: что продано и на сколько.", {**_DATES, **_ALL}, []),
}


def available(can_money: bool) -> dict[str, tuple]:
    return {name: spec for name, spec in TOOLS.items() if can_money or not spec[1]}


def schemas(can_money: bool) -> list[dict]:
    """The tools as OpenAI function definitions."""
    return [
        {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        }}
        for name, (_fn, _money, description, properties, required) in available(can_money).items()
    ]


def call(conn: sqlite3.Connection, ctx: dict, name: str, args: dict) -> dict:
    """Run one tool. Anything wrong — an unknown or forbidden name, bad
    arguments, an error inside — comes back as {"error": …} for the model
    to deal with; it never raises into the conversation."""
    spec = available(ctx["can_money"]).get(name)
    if not spec:
        return {"error": "такого инструмента нет или он вам недоступен"}
    try:
        return spec[0](conn, ctx, args if isinstance(args, dict) else {})
    except (TypeError, ValueError, KeyError, sqlite3.Error) as exc:
        return {"error": f"не получилось: {exc}"}
