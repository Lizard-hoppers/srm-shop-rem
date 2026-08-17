from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import inventory as core_inventory
from core import purchases as core_purchases
from core.storage import get_conn
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter(prefix="/purchases")

ITEM_ROWS = 3


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        receipts = core_purchases.list_receipts(conn)
        suppliers = core_purchases.list_suppliers(conn)
        products = core_inventory.list_products(conn)
        cells = core_inventory.list_cells(conn)
    return render(
        request, "purchases_list.html", staff=staff, receipts=receipts, suppliers=suppliers,
        products=products, cells=cells, item_rows=range(ITEM_ROWS),
    )


@router.post("/suppliers")
def create_supplier_view(request: Request, name: str = Form(...), contact: str = Form(""), staff=Depends(require_staff)):
    with get_conn() as conn:
        core_purchases.create_supplier(conn, name.strip(), contact.strip() or None)
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.post("")
async def create_receipt_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    supplier_id = form.get("supplier_id")
    invoice_no = form.get("invoice_no", "")

    items = []
    for i in range(ITEM_ROWS):
        product_id = form.get(f"product_id_{i}")
        cell_id = form.get(f"cell_id_{i}")
        qty = form.get(f"qty_{i}")
        if not product_id or not cell_id or not qty:
            continue
        unit_cost = form.get(f"unit_cost_{i}") or None
        items.append((int(product_id), int(cell_id), int(qty), int(unit_cost) if unit_cost else None))

    if items:
        with get_conn() as conn:
            core_purchases.create_receipt(
                conn, int(supplier_id) if supplier_id else None, invoice_no.strip() or None, staff["id"], items
            )
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.get("/{receipt_id}")
def detail_view(request: Request, receipt_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        receipt = core_purchases.get_receipt(conn, receipt_id)
        if not receipt:
            return RedirectResponse(link(request, "/purchases"), status_code=303)
        items = core_purchases.get_receipt_items(conn, receipt_id)
    return render(request, "purchase_detail.html", staff=staff, receipt=receipt, items=items)
