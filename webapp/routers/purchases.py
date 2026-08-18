from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import inventory as core_inventory
from core import purchases as core_purchases
from core.storage import get_conn
from webapp.deps import link, require_role
from webapp.templating import render

router = APIRouter(prefix="/purchases")

ITEM_ROWS = 3
_PURCHASE_ROLES = ("owner", "admin", "storekeeper")


def _list_context(conn) -> dict:
    products = core_inventory.list_products(conn)
    return {
        "receipts": core_purchases.list_receipts(conn),
        "suppliers": core_purchases.list_suppliers(conn),
        "products": products,
        "cells": core_inventory.list_cells(conn),
        "item_rows": range(ITEM_ROWS),
        "products_for_picker": [
            {"id": p["id"], "label": f"{p['name']} ({p['sku']})" if p["sku"] else p["name"]}
            for p in products
        ],
        "default_cell_by_product": core_inventory.default_cell_by_product(conn),
    }


@router.get("")
def list_view(request: Request, staff=Depends(require_role(*_PURCHASE_ROLES))):
    with get_conn() as conn:
        ctx = _list_context(conn)
    return render(request, "purchases_list.html", staff=staff, **ctx)


@router.post("/suppliers")
def create_supplier_view(
    request: Request, name: str = Form(""), contact: str = Form(""),
    staff=Depends(require_role(*_PURCHASE_ROLES)),
):
    if not name.strip():
        with get_conn() as conn:
            ctx = _list_context(conn)
        return render(request, "purchases_list.html", staff=staff, error="Введите название поставщика.", **ctx)

    with get_conn() as conn:
        core_purchases.create_supplier(conn, name.strip(), contact.strip() or None)
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.post("")
async def create_receipt_view(request: Request, staff=Depends(require_role(*_PURCHASE_ROLES))):
    form = await request.form()
    supplier_id = form.get("supplier_id")
    invoice_no = form.get("invoice_no", "")
    row_count = int(form.get("row_count") or ITEM_ROWS)

    with get_conn() as conn:
        items = []
        for i in range(row_count):
            cell_id = form.get(f"cell_id_{i}")
            qty = form.get(f"qty_{i}")
            product_id = form.get(f"product_id_{i}")
            product_name = (form.get(f"product_name_{i}") or "").strip()

            if not product_id and product_name:
                # Typed a name that didn't resolve to an existing product in
                # the picker (see purchases_list.html JS) — create it on the
                # spot rather than forcing a trip to Склад → Товары first.
                # Sensible defaults; category/price can be filled in later.
                unit_cost_for_new = form.get(f"unit_cost_{i}") or None
                product_id = core_inventory.create_product(
                    conn, name=product_name, sku=None, category=None, unit="шт",
                    is_repair_part=True, is_sellable=True, min_qty=0,
                    price=int(unit_cost_for_new) if unit_cost_for_new else None,
                )

            if not product_id or not cell_id or not qty:
                continue
            unit_cost = form.get(f"unit_cost_{i}") or None
            items.append((int(product_id), int(cell_id), int(qty), int(unit_cost) if unit_cost else None))

        if items:
            core_purchases.create_receipt(
                conn, int(supplier_id) if supplier_id else None, invoice_no.strip() or None, staff["id"], items
            )
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.get("/{receipt_id}")
def detail_view(request: Request, receipt_id: int, staff=Depends(require_role(*_PURCHASE_ROLES))):
    with get_conn() as conn:
        receipt = core_purchases.get_receipt(conn, receipt_id)
        if not receipt:
            return RedirectResponse(link(request, "/purchases"), status_code=303)
        items = core_purchases.get_receipt_items(conn, receipt_id)
    return render(request, "purchase_detail.html", staff=staff, receipt=receipt, items=items)
