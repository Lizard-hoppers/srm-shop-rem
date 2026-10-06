from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core.clients import list_clients
from core.inventory import list_products, low_stock_report
from core.repairs import list_repairs
from core.sales import list_sales
from core.storage import get_conn
from webapp.deps import loc, require_staff
from webapp.templating import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, staff=Depends(require_staff)):
    """Counters for the точка the staff member is working in; clients and
    the product catalog are one for the whole business."""
    location_id = loc(request)
    with get_conn() as conn:
        clients_count = len(list_clients(conn))
        products_count = len(list_products(conn))
        low_stock_count = len(low_stock_report(conn, location_id))
        open_repairs_count = len([
            r for r in list_repairs(conn, location_id=location_id) if r["status"] not in ("issued", "cancelled")
        ])
        sales_today_count = len(list_sales(conn, limit=1000, location_id=location_id, include_cancelled=False))
    return render(
        request,
        "dashboard.html",
        staff=staff,
        clients_count=clients_count,
        products_count=products_count,
        low_stock_count=low_stock_count,
        open_repairs_count=open_repairs_count,
        sales_count=sales_today_count,
    )
