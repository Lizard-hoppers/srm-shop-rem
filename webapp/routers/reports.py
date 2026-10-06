from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core import cash as core_cash
from core import inventory as core_inventory
from core import reports as core_reports
from core import repairs as core_repairs
from core import store_access
from core import store_settings as core_store_settings
from core.storage import get_conn
from webapp.deps import loc, require_role
from webapp.templating import render

router = APIRouter(prefix="/reports")


@router.get("")
def reports_view(request: Request, staff=Depends(require_role("owner", "admin"))):
    location_id = loc(request)
    with get_conn() as conn:
        by_status = core_reports.repairs_by_status(conn, location_id)
        by_master = core_reports.repairs_by_master(conn, location_id)
        avg_days = core_reports.avg_repair_turnaround_days(conn, location_id)
        by_channel = core_reports.sales_by_channel(conn, location_id)
        revenue = core_reports.repairs_revenue(conn, location_id)
        low_stock = core_inventory.low_stock_report(conn, location_id)
    return render(
        request, "reports.html", staff=staff,
        by_status=by_status, by_master=by_master, avg_days=avg_days,
        by_channel=by_channel, revenue=revenue, low_stock=low_stock,
        status_labels=core_repairs.STATUS_LABELS,
    )


_ALL_STORES_KEYS = ("open_repairs", "repairs_revenue", "sales_orders", "sales_revenue", "cash_balance", "low_stock_count")


@router.get("/all-stores")
def all_stores_view(request: Request, staff=Depends(require_role("owner", "admin"))):
    """One-glance summary across every точка this staff member can work in
    (core.store_access) — all-time, same scope as the per-точка /reports
    page above (neither core.reports nor core.cash.cash_balance support a
    date range, only core.cash.period_summary does, which isn't used here)."""
    rows = []
    with get_conn() as conn:
        for store in store_access.stores_for_staff(staff):
            location_id = store.location_id
            statuses = {r["status"]: r["n"] for r in core_reports.repairs_by_status(conn, location_id)}
            open_repairs = sum(n for status, n in statuses.items() if status not in ("issued", "cancelled"))
            sales = core_reports.sales_by_channel(conn, location_id)
            rows.append({
                "name": core_store_settings.get_settings(conn, location_id)["name"],
                "open_repairs": open_repairs,
                "repairs_revenue": core_reports.repairs_revenue(conn, location_id),
                "sales_orders": sum(r["orders"] for r in sales),
                "sales_revenue": sum(r["revenue"] for r in sales),
                "cash_balance": core_cash.cash_balance(conn, location_id),
                "low_stock_count": len(core_inventory.low_stock_report(conn, location_id)),
            })
    totals = {key: sum(r[key] for r in rows) for key in _ALL_STORES_KEYS}
    return render(request, "all_stores.html", staff=staff, rows=rows, totals=totals)
