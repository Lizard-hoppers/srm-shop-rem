"""Главная, «Чистая прибыль» и «Проблемы» (Заход 7) — read-only numbers
over what the other modules record. Nothing here writes.

Чистая прибыль периода, as the презентация defines it:

    прибыль продаж   (сумма − себестоимость проданного, по документам ПД)
  + прибыль ремонтов (цена − запчасти − доля мастера, по выданным РК)
  − расходы          (аренда, зарплата, прочее — кассовые расходы)
  − списания товара  (себестоимость списанного)

What is deliberately NOT an expense: закупка товара and покупка телефонов
(that money became stock — it turns into cost when the thing is sold),
выплата мастеру (his share was already taken out of the repair's profit;
in производство it went into the phone's cost), обмен валют, перемещения,
деньги контрагентов.
"""
from __future__ import annotations

import sqlite3

from core import cash as _cash
from core import inventory as _inventory
from core import masters as _masters
from core import repairs as _repairs
from core import settlements as _settlements
from core.accounts import money

# Кассовые расходы that reduce profit (see the module docstring for what doesn't).
OPERATING_CATEGORIES = ("rent", "salary", "other")

PERIODS = ("day", "week", "month")


def _loc(column: str, location_id: int | None) -> tuple[str, list]:
    return (f" AND {column} = ?", [location_id]) if location_id else ("", [])


def profit(conn: sqlite3.Connection, utc_start: str, utc_end: str, location_id: int | None = None) -> dict:
    """The «Чистая прибыль» breakdown for a period (one точка, or the
    whole business when location_id is None)."""
    clause, params = _loc("documents.location_id", location_id)
    sales = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS revenue, COALESCE(SUM(profit), 0) AS profit,
                   COALESCE(SUM(CASE WHEN profit IS NULL THEN 1 ELSE 0 END), 0) AS unknown
            FROM documents
            WHERE doc_type = 'sale' AND status = 'posted' AND created_at >= ? AND created_at < ?{clause}""",
        [utc_start, utc_end, *params],
    ).fetchone()
    # A repair earns when it is выдан, not when it was taken in.
    clause, params = _loc("repair_orders.location_id", location_id)
    repairs = conn.execute(
        f"""SELECT COUNT(*) AS n,
                   COALESCE(SUM(COALESCE(repair_orders.price_final, repair_orders.price_estimate, 0)), 0) AS revenue,
                   COALESCE(SUM(documents.profit), 0) AS profit
            FROM repair_orders
            LEFT JOIN documents ON documents.doc_type = 'repair' AND documents.ref_id = repair_orders.id
            WHERE repair_orders.status = 'issued' AND repair_orders.issued_at >= ? AND repair_orders.issued_at < ?{clause}""",
        [utc_start, utc_end, *params],
    ).fetchone()
    # Repairs выданные before profit was written onto the document
    # (Заход 5) have none there — theirs is worked out here, the same way.
    legacy_profit = sum(
        _repairs.finance(conn, row["id"])["firm_profit"]
        for row in conn.execute(
            f"""SELECT repair_orders.id FROM repair_orders
                LEFT JOIN documents ON documents.doc_type = 'repair' AND documents.ref_id = repair_orders.id
                WHERE repair_orders.status = 'issued' AND repair_orders.issued_at >= ? AND repair_orders.issued_at < ?
                  AND documents.profit IS NULL{clause}""",
            [utc_start, utc_end, *params],
        ).fetchall()
    )
    repairs_profit = repairs["profit"] + legacy_profit
    clause, params = _loc("location_id", location_id)
    expenses = {
        row["category"]: money(row["total"])
        for row in conn.execute(
            f"""SELECT category, SUM(COALESCE(amount_uah, amount)) AS total FROM cash_transactions
                WHERE kind = 'expense' AND cancelled_at IS NULL AND created_at >= ? AND created_at < ?
                  AND category IN ({','.join('?' for _ in OPERATING_CATEGORIES)}){clause}
                GROUP BY category""",
            [utc_start, utc_end, *OPERATING_CATEGORIES, *params],
        )
    }
    clause, params = _loc("documents.location_id", location_id)
    writeoffs = conn.execute(
        f"""SELECT COALESCE(SUM(stock_movements.qty * COALESCE(stock_movements.unit_cost, 0)), 0) AS total
            FROM documents
            JOIN stock_movements ON documents.ref_table = 'stock_movements' AND stock_movements.id = documents.ref_id
            WHERE documents.doc_type = 'writeoff' AND documents.status = 'posted'
              AND documents.created_at >= ? AND documents.created_at < ?{clause}""",
        [utc_start, utc_end, *params],
    ).fetchone()["total"]
    expenses_total = money(sum(expenses.values()))
    return {
        "sales_count": sales["n"], "sales_revenue": money(sales["revenue"]), "sales_profit": money(sales["profit"]),
        "sales_unknown_cost": sales["unknown"],
        "repairs_count": repairs["n"], "repairs_revenue": money(repairs["revenue"]),
        "repairs_profit": money(repairs_profit),
        "gross": money(sales["profit"] + repairs_profit),
        "expenses": {key: expenses.get(key, 0) for key in OPERATING_CATEGORIES}, "expenses_total": expenses_total,
        "writeoffs": money(writeoffs),
        "net": money(sales["profit"] + repairs_profit - expenses_total - writeoffs),
    }


def activity(conn: sqlite3.Connection, utc_start: str, utc_end: str, location_id: int | None = None) -> dict:
    """What happened in the period, in pieces — the counters of «Главная»."""
    def count(table: str, column: str = "created_at", extra: str = "") -> int:
        clause, params = _loc(f"{table}.location_id", location_id)
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {table}.{column} >= ? AND {table}.{column} < ?{extra}{clause}",
            [utc_start, utc_end, *params],
        ).fetchone()["n"]

    clause, params = _loc("location_id", location_id)
    buyback = conn.execute(
        f"""SELECT COUNT(*) AS n, COALESCE(SUM(amount), 0) AS total FROM documents
            WHERE doc_type = 'buyback' AND status = 'posted' AND created_at >= ? AND created_at < ?{clause}""",
        [utc_start, utc_end, *params],
    ).fetchone()
    return {
        "repairs_accepted": count("repair_orders"),
        "repairs_issued": count("repair_orders", "issued_at", " AND repair_orders.status = 'issued'"),
        "sales": count("sales_orders", extra=" AND sales_orders.status != 'cancelled'"),
        "orders_new": count("client_orders"),
        "buyback_count": buyback["n"], "buyback_total": money(buyback["total"]),
        "cash": _cash.period_summary(conn, utc_start, utc_end, location_id),
    }


def standing(conn: sqlite3.Connection, location_id: int | None = None) -> dict:
    """Where things stand right now: деньги, товар, долги."""
    debtors = _settlements.debtors(conn)
    masters_owed = [
        (row["id"], row["name"], _masters.owed(conn, row["id"]))
        for row in conn.execute("SELECT id, name FROM staff WHERE role = 'master'").fetchall()
    ]
    clause, params = _loc("location_id", location_id)
    return {
        "cash_balance": _cash.cash_balance(conn, location_id),
        "stock_value": _inventory.stock_value(conn, location_id),
        "they_owe": money(sum(d["balance"] for d in debtors if d["balance"] > 0)),
        "we_owe": money(-sum(d["balance"] for d in debtors if d["balance"] < 0)),
        "debtors": [dict(d) for d in debtors if d["balance"] > 0][:10],
        "creditors": [dict(d) for d in debtors if d["balance"] < 0][:10],
        "masters_owed": money(sum(amount for _id, _name, amount in masters_owed if amount > 0)),
        "masters": [{"id": i, "name": n, "owed": a} for i, n, a in masters_owed if a],
        "open_repairs": conn.execute(
            f"SELECT COUNT(*) AS n FROM repair_orders WHERE status NOT IN ('issued', 'cancelled'){clause}", params,
        ).fetchone()["n"],
        "open_orders": conn.execute(
            f"SELECT COUNT(*) AS n FROM client_orders WHERE status = 'reserved'{clause}", params,
        ).fetchone()["n"],
    }


# How long something may sit before it is a problem.
READY_NOT_ISSUED_DAYS = 7
TRANSIT_HOURS = 24
AT_MASTER_DAYS = 3
NO_PART_HOURS = 2


def problems(conn: sqlite3.Connection, location_id: int | None = None) -> list[dict]:
    """«Проблемы»: things that are waiting on a person. Each is
    {"text", "path", "count"} — path is where to go and fix it."""
    found: list[dict] = []

    def add(count: int, text: str, path: str) -> None:
        if count:
            found.append({"count": count, "text": text, "path": path})

    clause, params = _loc("repair_orders.location_id", location_id)
    no_part = conn.execute(
        f"""SELECT COUNT(*) AS n FROM repair_orders
            WHERE status = 'in_progress' AND no_parts = 0
              AND started_at <= datetime('now', '-{NO_PART_HOURS} hours')
              AND NOT EXISTS (SELECT 1 FROM stock_movements WHERE ref_type = 'repair_order'
                              AND ref_id = repair_orders.id AND reason = 'repair_use'){clause}""",
        params,
    ).fetchone()["n"]
    add(no_part, "ремонтов в работе без указанной запчасти", "/repairs")
    stale_ready = conn.execute(
        f"""SELECT COUNT(*) AS n FROM repair_orders
            WHERE status = 'ready' AND completed_at <= datetime('now', '-{READY_NOT_ISSUED_DAYS} days'){clause}""",
        params,
    ).fetchone()["n"]
    add(stale_ready, f"готовых ремонтов не забирают больше {READY_NOT_ISSUED_DAYS} дней", "/repairs")

    clause, params = _loc("destination.location_id", location_id)
    transit = conn.execute(
        f"""SELECT COUNT(*) AS n FROM stock_transfers
            JOIN warehouses AS destination ON destination.id = stock_transfers.to_warehouse_id
            WHERE stock_transfers.status = 'sent'
              AND stock_transfers.sent_at <= datetime('now', '-{TRANSIT_HOURS} hours'){clause}""",
        params,
    ).fetchone()["n"]
    add(transit, f"перемещений товара в пути дольше {TRANSIT_HOURS} ч — не приняты", "/transfers")
    clause, params = _loc("money_accounts.location_id", location_id)
    money_transit = conn.execute(
        f"""SELECT COUNT(*) AS n FROM money_transfers
            JOIN money_accounts ON money_accounts.id = money_transfers.to_account_id
            WHERE money_transfers.status = 'sent'
              AND money_transfers.sent_at <= datetime('now', '-{TRANSIT_HOURS} hours'){clause}""",
        params,
    ).fetchone()["n"]
    add(money_transit, f"перемещений денег в пути дольше {TRANSIT_HOURS} ч — не приняты", "/cash")
    negative = conn.execute(
        f"""SELECT COUNT(*) AS n FROM (
                SELECT money_accounts.id
                FROM money_accounts
                JOIN cash_transactions ON cash_transactions.account_id = money_accounts.id
                     AND cash_transactions.cancelled_at IS NULL
                WHERE money_accounts.active = 1{clause}
                GROUP BY money_accounts.id
                HAVING SUM(CASE WHEN cash_transactions.kind = 'income' THEN cash_transactions.amount
                                ELSE -cash_transactions.amount END) < -0.005)""",
        params,
    ).fetchone()["n"]
    add(negative, "счетов с отрицательным остатком", "/cash")

    clause, params = _loc("production_orders.location_id", location_id)
    at_master = conn.execute(
        f"""SELECT COUNT(*) AS n FROM production_orders
            WHERE status = 'in_work' AND handed_at <= datetime('now', '-{AT_MASTER_DAYS} days'){clause}""",
        params,
    ).fetchone()["n"]
    add(at_master, f"наших телефонов у мастера дольше {AT_MASTER_DAYS} дней без отчёта", "/production")
    reported = conn.execute(
        f"SELECT COUNT(*) AS n FROM production_orders WHERE status = 'reported'{clause}", params,
    ).fetchone()["n"]
    add(reported, "отчётов мастера ждут «Принять результат»", "/production")

    clause, params = _loc("client_orders.location_id", location_id)
    lapsed = conn.execute(
        f"""SELECT COUNT(*) AS n FROM client_orders
            WHERE (status = 'expired' OR (status = 'reserved' AND reserved_until <= datetime('now'))){clause}""",
        params,
    ).fetchone()["n"]
    add(lapsed, "заказов с истёкшим резервом — продлить или отменить", "/orders")

    # Only products somebody set a minimum for — «0 при минимуме 0» is not
    # a problem, it is simply a product this точка doesn't keep.
    low = [row for row in _inventory.low_stock_report(conn, location_id) if (row["min_qty"] or 0) > 0]
    add(len(low), "товаров с остатком ниже минимума", "/inventory/products?low_stock=1")
    return found


def problems_text(conn: sqlite3.Connection, location_id: int | None = None) -> list[str]:
    """The same list as plain lines — for the bot's сводка and the
    помощник's digest."""
    return [f"• {p['count']} — {p['text']}" for p in problems(conn, location_id)]
