"""Per-master repair stats and payout calculation (21.08).

A repair only counts once it's actually "Выдан" (status='issued') with a
price on it — same moment core.cash records касса income for it (see
webapp.routers.repairs.status_view). Profit = price_final minus the
parts cost snapshotted onto each repair_use stock movement at the moment
it was used (core.inventory.record_movement) — an approximation (parts
aren't tracked per supplier batch, see core.purchases), not exact
accounting, but the best basis available without asking staff to trace
which delivery a part came from at write-off time.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from core.accounts import money
from core.timefmt import KYIV, kyiv_date_range_utc, kyiv_today


def _day_bounds_utc() -> tuple[str, str]:
    today = kyiv_today()
    return kyiv_date_range_utc(today, today)


def _month_bounds_utc() -> tuple[str, str]:
    now = datetime.now(KYIV)
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    return kyiv_date_range_utc(month_start, now.strftime("%Y-%m-%d"))


def period_stats(
    conn: sqlite3.Connection, master_id: int, utc_start: str | None = None, utc_end: str | None = None
) -> dict:
    """Repairs count / revenue / parts cost / profit for one master, issued
    repairs only, optionally bounded to [utc_start, utc_end). Omit both
    bounds for all-time."""
    where = "master_id = ? AND status = 'issued'"
    params: list = [master_id]
    if utc_start is not None:
        where += " AND issued_at >= ? AND issued_at < ?"
        params += [utc_start, utc_end]

    row = conn.execute(
        f"SELECT COUNT(*) AS repairs_count, COALESCE(SUM(price_final), 0) AS revenue "
        f"FROM repair_orders WHERE {where}",
        params,
    ).fetchone()

    parts_where = (
        "repair_orders.master_id = ? AND repair_orders.status = 'issued' "
        "AND stock_movements.reason = 'repair_use' AND stock_movements.unit_cost IS NOT NULL"
    )
    parts_params: list = [master_id]
    if utc_start is not None:
        parts_where += " AND repair_orders.issued_at >= ? AND repair_orders.issued_at < ?"
        parts_params += [utc_start, utc_end]

    parts_row = conn.execute(
        f"""SELECT COALESCE(SUM(stock_movements.qty * stock_movements.unit_cost), 0) AS parts_cost
            FROM stock_movements
            JOIN repair_orders
              ON repair_orders.id = stock_movements.ref_id AND stock_movements.ref_type = 'repair_order'
            WHERE {parts_where}""",
        parts_params,
    ).fetchone()

    revenue = row["revenue"]
    parts_cost = parts_row["parts_cost"]
    return {
        "repairs_count": row["repairs_count"],
        "revenue": revenue,
        "parts_cost": parts_cost,
        "profit": revenue - parts_cost,
    }


def payout(profit: int, repairs_count: int, pay_type: str | None, pay_value: int | None) -> int:
    """A master's own cut, per their pay_type/pay_value — 0 if neither is
    set yet (a newly added master with no rate configured). Never
    negative: a heavily discounted repair whose parts cost more than the
    price would otherwise math out to the shop owing the master money,
    which isn't a real scenario worth modeling."""
    if not pay_type or pay_value is None:
        return 0
    if pay_type == "percent":
        return max(0, round(profit * pay_value / 100))
    if pay_type == "fixed":
        return repairs_count * pay_value
    return 0


def master_summary(conn: sqlite3.Connection, master: sqlite3.Row) -> dict:
    """today/month/all-time stats + payout for one master row — the
    building block for both the Мастера list (quick summary per card)
    and a master's own detail page (full breakdown)."""
    day_start, day_end = _day_bounds_utc()
    month_start, month_end = _month_bounds_utc()

    today_stats = period_stats(conn, master["id"], day_start, day_end)
    month_stats = period_stats(conn, master["id"], month_start, month_end)
    all_time_stats = period_stats(conn, master["id"])

    for stats in (today_stats, month_stats, all_time_stats):
        stats["payout"] = payout(stats["profit"], stats["repairs_count"], master["pay_type"], master["pay_value"])

    return {"today": today_stats, "month": month_stats, "all_time": all_time_stats}


# ---- начисления (Заход 5) ----
#
# period_stats()/payout() above compute a master's share on the fly for the
# «Мастера» statistics. The rows below are the LEDGER of what the business
# actually owes him: written once when a document earns it (a client
# repair is выдан, a производство result is accepted), cancelled when that
# document is undone. «Начислено − выплачено = мы должны мастеру».

def repair_share(profit, pay_type: str | None, pay_value) -> float | int:
    """A master's cut of ONE client repair: his percent of the repair's
    profit base (price − parts), or his fixed rate per repair. Never
    negative; 0 with no rate configured."""
    if not pay_type or pay_value is None:
        return 0
    if pay_type == "percent":
        return max(0, round(float(profit) * float(pay_value) / 100))
    if pay_type == "fixed":
        return pay_value
    return 0


def accrue(
    conn: sqlite3.Connection, staff_id: int, kind: str, ref_type: str, ref_id: int, amount, comment: str | None = None,
) -> int | None:
    """Record what a document earned a master. Nothing is written for a
    zero amount (a master with no rate, a job with no pay)."""
    if not amount or amount <= 0:
        return None
    return conn.execute(
        "INSERT INTO master_accruals (staff_id, kind, ref_type, ref_id, amount, comment) VALUES (?, ?, ?, ?, ?, ?)",
        (staff_id, kind, ref_type, ref_id, amount, comment),
    ).lastrowid


def cancel_accruals(conn: sqlite3.Connection, ref_type: str, ref_id: int) -> None:
    conn.execute(
        "UPDATE master_accruals SET cancelled_at = datetime('now') WHERE ref_type = ? AND ref_id = ? AND cancelled_at IS NULL",
        (ref_type, ref_id),
    )


def accrued_for(conn: sqlite3.Connection, ref_type: str, ref_id: int) -> float | int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM master_accruals WHERE ref_type = ? AND ref_id = ? AND cancelled_at IS NULL",
        (ref_type, ref_id),
    ).fetchone()
    total = round(row["total"], 2)
    return int(total) if total == int(total) else total


def accrued_total(conn: sqlite3.Connection, staff_id: int) -> float | int:
    """Everything earned and not cancelled — the «Начислено» line."""
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM master_accruals WHERE staff_id = ? AND cancelled_at IS NULL",
        (staff_id,),
    ).fetchone()
    total = round(row["total"], 2)
    return int(total) if total == int(total) else total


def list_accruals(conn: sqlite3.Connection, staff_id: int, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM master_accruals WHERE staff_id = ? AND cancelled_at IS NULL ORDER BY id DESC LIMIT ?",
        (staff_id, limit),
    ).fetchall()


# ---- выплаты (Заход 6): «начислено − выплачено = мы должны мастеру» ----

class PayoutError(Exception):
    """A payout that can't go ahead — message is shown to staff as-is."""


def paid_total(conn: sqlite3.Connection, staff_id: int) -> float | int:
    row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS total FROM master_payouts WHERE staff_id = ? AND cancelled_at IS NULL",
        (staff_id,),
    ).fetchone()
    return money(row["total"])


def owed(conn: sqlite3.Connection, staff_id: int) -> float | int:
    """What the business owes the master right now (negative — he was
    paid ahead of what he has earned)."""
    return money(accrued_total(conn, staff_id) - paid_total(conn, staff_id))


def list_payouts(conn: sqlite3.Connection, staff_id: int, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT master_payouts.*, staff.name AS paid_by_name FROM master_payouts
           LEFT JOIN staff ON staff.id = master_payouts.paid_by
           WHERE master_payouts.staff_id = ? ORDER BY master_payouts.id DESC LIMIT ?""",
        (staff_id, limit),
    ).fetchall()


def pay_out(
    conn: sqlite3.Connection, staff_id: int, payments: list[tuple[int, object, object]], *, paid_by: int,
    location_id: int | None = None, comment: str | None = None, key: str | None = None,
) -> int:
    """«Выплата мастеру» (РКО): money leaves the точка's accounts and
    what we owe him goes down by its гривня value. Returns the payout id."""
    from core import cash as _cash
    from core import documents as _documents
    from core import locations as _locations

    master = conn.execute("SELECT * FROM staff WHERE id = ? AND role = 'master'", (staff_id,)).fetchone()
    if not master:
        raise PayoutError("Мастер не найден.")
    location_id = _locations.resolve(conn, location_id)
    resolved = _cash.resolve_parts(conn, location_id, payments or [])
    total = _cash.paid_uah(resolved)
    if not resolved or total <= 0:
        raise PayoutError("Укажите сумму выплаты хотя бы на одном счёте.")
    comment = (comment or "").strip() or None
    payout_id = conn.execute(
        "INSERT INTO master_payouts (staff_id, amount, location_id, paid_by, comment) VALUES (?, ?, ?, ?, ?)",
        (staff_id, total, location_id, paid_by, comment),
    ).lastrowid
    title = f"Выплата мастеру: {master['name']}" + (f" — {comment}" if comment else "")
    _cash.record_payments(
        conn, "expense", resolved, "master_payout", payout_id, paid_by, category="master_payout", comment=title,
    )
    _documents.register(
        conn, "cash_out", staff_id=paid_by, location_id=location_id, ref_table="master_payouts",
        ref_id=payout_id, title=title, amount=total, key=key,
    )
    return payout_id


def cancel_payout(conn: sqlite3.Connection, payout_id: int) -> None:
    from core import cash as _cash

    for row in conn.execute(
        "SELECT id FROM cash_transactions WHERE ref_type = 'master_payout' AND ref_id = ?", (payout_id,)
    ).fetchall():
        _cash.cancel_transaction(conn, row["id"])
    conn.execute(
        "UPDATE master_payouts SET cancelled_at = datetime('now') WHERE id = ? AND cancelled_at IS NULL", (payout_id,)
    )
