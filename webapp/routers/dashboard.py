from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core.clients import list_clients
from core.inventory import list_products, low_stock_report
from core.repairs import list_repairs
from core.sales import list_sales
from core.storage import get_conn
from webapp.deps import require_staff
from webapp.templating import render

router = APIRouter()


@router.get("/")
def dashboard(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        clients_count = len(list_clients(conn))
        products_count = len(list_products(conn))
        low_stock_count = len(low_stock_report(conn))
        open_repairs_count = len([r for r in list_repairs(conn) if r["status"] not in ("issued", "cancelled")])
        sales_today_count = len(list_sales(conn, limit=1000))
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
