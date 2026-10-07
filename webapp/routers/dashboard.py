"""Главная (Заход 7): what happened in the chosen period, where things
stand, what is waiting on a person («Проблемы»), the sections of the
business, and the tail of the journal. Money — profit, касса, долги — is
the owner's/admin's view; everyone else gets the counters and problems of
the точка they work in.

«Чистая прибыль» (/profit) is the same period filter over
core.overview.profit, owner/admin only.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request

from core import documents as core_documents
from core import locations as core_locations
from core import orders as core_orders
from core import overview as core_overview
from core.clients import list_clients
from core.inventory import list_products, low_stock_report
from core.shifts import current_shift
from core.storage import get_conn
from core.timefmt import kyiv_date_range_utc, kyiv_today
from webapp.deps import loc, require_role, require_staff
from webapp.templating import render

router = APIRouter()

_MONEY_ROLES = ("owner", "admin")
_JOURNAL_TAIL = 8


def period_dates(period: str, date_from: str = "", date_to: str = "") -> tuple[str, str, str]:
    """(period, date_from, date_to) as inclusive Kyiv dates. An explicit,
    valid date range wins («custom»); otherwise день / неделя (the last 7
    days) / месяц (from the 1st)."""
    today = kyiv_today()
    if date_from and date_to:
        try:
            kyiv_date_range_utc(date_from, date_to)
            return "custom", date_from, date_to
        except ValueError:
            pass
    if period == "week":
        start = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=6)).strftime("%Y-%m-%d")
        return "week", start, today
    if period == "month":
        return "month", today[:8] + "01", today
    return "day", today, today


def _scope(request: Request, staff, point: str, conn) -> tuple[int | None, str, list]:
    """Which точка the numbers are for: the one the person works in, or —
    for owner/admin — any one of them or all («all»)."""
    locations = core_locations.list_locations(conn) if staff["role"] in _MONEY_ROLES else []
    if staff["role"] in _MONEY_ROLES and len(locations) > 1:
        if point == "all":
            return None, "all", locations
        if point.isdigit() and any(l["id"] == int(point) for l in locations):
            return int(point), point, locations
    return loc(request), str(loc(request)), locations


@router.get("/")
def dashboard(request: Request, period: str = "day", point: str = "", staff=Depends(require_staff)):
    period, date_from, date_to = period_dates(period)
    utc_start, utc_end = kyiv_date_range_utc(date_from, date_to)
    show_money = staff["role"] in _MONEY_ROLES
    with get_conn() as conn:
        core_orders.expire_due(conn)
        location_id, point, locations = _scope(request, staff, point, conn)
        activity = core_overview.activity(conn, utc_start, utc_end, location_id)
        standing = core_overview.standing(conn, location_id)
        journal = [
            {**dict(d), "label": core_documents.doc_label(d), "source_path": core_documents.source_path(d)}
            for d in core_documents.list_journal(conn, location_id=location_id, limit=_JOURNAL_TAIL)
        ]
        return render(
            request,
            "dashboard.html",
            staff=staff,
            period=period, point=point, locations=locations, show_money=show_money,
            activity=activity, standing=standing,
            profit=core_overview.profit(conn, utc_start, utc_end, location_id) if show_money else None,
            problems=core_overview.problems(conn, location_id),
            journal=journal,
            clients_count=len(list_clients(conn)),
            products_count=len(list_products(conn)),
            low_stock_count=len(low_stock_report(conn, location_id)),
            open_repairs_count=standing["open_repairs"],
            sales_count=activity["sales"],
            shift=current_shift(conn, staff["id"], loc(request)),
        )


@router.get("/profit")
def profit_view(
    request: Request, period: str = "month", point: str = "", date_from: str = "", date_to: str = "",
    staff=Depends(require_role(*_MONEY_ROLES)),
):
    period, date_from, date_to = period_dates(period, date_from, date_to)
    utc_start, utc_end = kyiv_date_range_utc(date_from, date_to)
    with get_conn() as conn:
        location_id, point, locations = _scope(request, staff, point, conn)
        return render(
            request, "profit.html", staff=staff, period=period, point=point, locations=locations,
            date_from=date_from, date_to=date_to,
            profit=core_overview.profit(conn, utc_start, utc_end, location_id),
            standing=core_overview.standing(conn, location_id),
        )
