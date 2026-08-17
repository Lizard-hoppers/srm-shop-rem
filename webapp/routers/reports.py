from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core import inventory as core_inventory
from core import reports as core_reports
from core import repairs as core_repairs
from core.storage import get_conn
from webapp.deps import require_staff
from webapp.templating import render

router = APIRouter(prefix="/reports")


@router.get("")
def reports_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        by_status = core_reports.repairs_by_status(conn)
        by_master = core_reports.repairs_by_master(conn)
        avg_days = core_reports.avg_repair_turnaround_days(conn)
        by_channel = core_reports.sales_by_channel(conn)
        revenue = core_reports.repairs_revenue(conn)
        low_stock = core_inventory.low_stock_report(conn)
    return render(
        request, "reports.html", staff=staff,
        by_status=by_status, by_master=by_master, avg_days=avg_days,
        by_channel=by_channel, revenue=revenue, low_stock=low_stock,
        status_labels=core_repairs.STATUS_LABELS,
    )
