from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from core import clients as core_clients
from core import inventory as core_inventory
from core import sales as core_sales
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter(prefix="/sales")

ITEM_ROWS = 3


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        sales = core_sales.list_sales(conn)
        products = core_inventory.list_products(conn)
    return render(request, "sales_list.html", staff=staff, sales=sales, products=products, item_rows=range(ITEM_ROWS))


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = (form.get("client_phone") or "").strip()
    channel = form.get("channel", "offline")

    items = []
    for i in range(ITEM_ROWS):
        product_id = form.get(f"product_id_{i}")
        qty = form.get(f"qty_{i}")
        price = form.get(f"price_{i}")
        if not product_id or not qty or not price:
            continue
        items.append((int(product_id), int(qty), int(price)))

    if not items:
        return RedirectResponse(link(request, "/sales"), status_code=303)

    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel) if client_phone else None
        try:
            order_id = core_sales.create_sale(conn, client_id, channel, staff["id"], items)
        except InsufficientStockError as exc:
            sales = core_sales.list_sales(conn)
            products = core_inventory.list_products(conn)
            return render(
                request, "sales_list.html", staff=staff, sales=sales, products=products,
                item_rows=range(ITEM_ROWS), error=str(exc),
            )
    return RedirectResponse(link(request, f"/sales/{order_id}"), status_code=303)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        sale = core_sales.get_sale(conn, order_id)
        if not sale:
            return RedirectResponse(link(request, "/sales"), status_code=303)
        items = core_sales.get_sale_items(conn, order_id)
    return render(request, "sale_detail.html", staff=staff, sale=sale, items=items)
