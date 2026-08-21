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
