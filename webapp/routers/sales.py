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

INITIAL_ROWS = 3


def _list_context(conn) -> dict:
    """The sale-list/new-sale page context — item_rows is just how many
    empty rows render on first load; "+ Добавить позицию" (sale-rows.js)
    grows the checkout past that with no fixed cap, unlike the old
    hardcoded 3-row form."""
    return {
        "sales": core_sales.list_sales(conn),
        "item_rows": range(INITIAL_ROWS),
        **_picker_context(conn),
    }


def _picker_context(conn) -> dict:
    """Product-search data for sale-rows.js: same {id, label} picker shape
    purchase-rows.js uses (label includes SKU when there is one, so
    typing/matching by SKU works same as by name), plus each product's
    price so picking/scanning a product can prefill it — the cashier can
    still override, e.g. a discount."""
    products = core_inventory.list_products(conn)
    return {
        "products_for_picker": [
            {"id": p["id"], "label": f"{p['name']} ({p['sku']})" if p["sku"] else p["name"]}
            for p in products
        ],
        "default_price_by_product": {p["id"]: p["price"] for p in products if p["price"] is not None},
    }


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _list_context(conn)
    return render(request, "sales_list.html", staff=staff, **ctx)


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = core_clients.normalize_phone((form.get("client_phone") or "").strip())
    channel = form.get("channel", "offline")
    warranty_until = (form.get("warranty_until") or "").strip() or None
    row_count = int(form.get("row_count") or INITIAL_ROWS)

    items = []
    unresolved = []
    for i in range(row_count):
        product_id = form.get(f"product_id_{i}")
        product_name = (form.get(f"product_name_{i}") or "").strip()
        qty = form.get(f"qty_{i}")
        price = form.get(f"price_{i}")

        if not product_id and not product_name and not qty and not price:
            continue  # untouched row — sparse checkout is fine, same as before
        if not product_id:
            unresolved.append(product_name or f"строка {i + 1}")
            continue
        if not qty or not price:
            continue  # started filling but incomplete — skip rather than 500
        items.append((int(product_id), int(qty), int(price)))

    error = None
    if unresolved:
        error = "Не найден в каталоге: " + ", ".join(unresolved) + " — выберите товар из списка или уберите строку."
    elif not items:
        error = "Добавьте хотя бы один товар в чек."

    if error:
        with get_conn() as conn:
            ctx = _list_context(conn)
        return render(request, "sales_list.html", staff=staff, error=error, **ctx)

    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel) if client_phone else None
        try:
            order_id = core_sales.create_sale(conn, client_id, channel, staff["id"], items, warranty_until)
        except InsufficientStockError as exc:
            ctx = _list_context(conn)
            return render(request, "sales_list.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/sales/{order_id}"), status_code=303)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        sale = core_sales.get_sale(conn, order_id)
        if not sale:
            return RedirectResponse(link(request, "/sales"), status_code=303)
        items = core_sales.get_sale_items(conn, order_id)
    return render(request, "sale_detail.html", staff=staff, sale=sale, items=items)
