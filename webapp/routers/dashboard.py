from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from core.inventory import list_products, low_stock_report
from core.clients import list_clients
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
    return render(
        request,
        "dashboard.html",
        staff=staff,
        clients_count=clients_count,
        products_count=products_count,
        low_stock_count=low_stock_count,
    )


@router.get("/repairs")
def repairs_placeholder(request: Request, staff=Depends(require_staff)):
    return render(request, "placeholder.html", staff=staff, title="Ремонты")


@router.get("/sales")
def sales_placeholder(request: Request, staff=Depends(require_staff)):
    return render(request, "placeholder.html", staff=staff, title="Продажи")


@router.get("/purchases")
def purchases_placeholder(request: Request, staff=Depends(require_staff)):
    return render(request, "placeholder.html", staff=staff, title="Приход комплектующих")


@router.get("/reports")
def reports_placeholder(request: Request, staff=Depends(require_staff)):
    return render(request, "placeholder.html", staff=staff, title="Отчёты")
