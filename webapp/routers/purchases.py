from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from core import inventory as core_inventory
from core import purchase_import as core_purchase_import
from core import purchases as core_purchases
from core import vision_ocr
from core.storage import get_conn
from webapp.deps import link, require_role
from webapp.templating import render

router = APIRouter(prefix="/purchases")

ITEM_ROWS = 3
_PURCHASE_ROLES = ("owner", "admin", "storekeeper")
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)


def _list_context(conn) -> dict:
    return {
        "receipts": core_purchases.list_receipts(conn),
        "suppliers": core_purchases.list_suppliers(conn),
        "cells": core_inventory.list_cells(conn),
        "item_rows": range(ITEM_ROWS),
        **_picker_context(conn),
    }


def _picker_context(conn) -> dict:
    """The product-search/default-cell data every receipt-editing page
    (intake form, photo-draft review) embeds for purchase-rows.js."""
    products = core_inventory.list_products(conn)
    return {
        "products_for_picker": [
            {"id": p["id"], "label": f"{p['name']} ({p['sku']})" if p["sku"] else p["name"]}
            for p in products
        ],
        "default_cell_by_product": core_inventory.default_cell_by_product(conn),
    }


def _parse_receipt_form_items(conn, form, row_count: int) -> list[tuple[int, int, int, int | None]]:
    """Shared by create_receipt_view and the photo-draft correction form —
    both submit the exact same product_id_N/product_name_N/cell_id_N/
    qty_N/unit_cost_N fields built by purchase-rows.js. A row whose typed
    name didn't resolve to an existing product gets created here rather
    than forcing a trip to Склад → Товары first."""
    items = []
    for i in range(row_count):
        cell_id = form.get(f"cell_id_{i}")
        qty = form.get(f"qty_{i}")
        product_id = form.get(f"product_id_{i}")
        product_name = (form.get(f"product_name_{i}") or "").strip()

        if not product_id and product_name:
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
    return items


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
        items = _parse_receipt_form_items(conn, form, row_count)
        if items:
            core_purchases.create_receipt(
                conn, int(supplier_id) if supplier_id else None, invoice_no.strip() or None, staff["id"], items
            )
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.post("/parse")
async def parse_invoice_view(request: Request, staff=Depends(require_role(*_PURCHASE_ROLES))):
    """Best-effort parse of a pasted invoice into draft rows for the intake
    form's JS to render — see purchases_list.html and core/purchase_import.py.
    Read-only: never writes to the DB."""
    body = await request.json()
    with get_conn() as conn:
        rows = core_purchase_import.parse_invoice_text(conn, body.get("text", ""))
    return JSONResponse({"rows": rows})


@router.post("/scan")
async def scan_invoice_view(
    photo: UploadFile = File(...), staff=Depends(require_role(*_PURCHASE_ROLES)),
):
    """Photo taken right in the Mini App (camera capture on the intake
    form, purchases_list.html) -> OpenAI vision -> matched draft rows, the
    same JSON shape /purchases/parse returns so the frontend reuses the
    exact same row-rendering code. No purchase_drafts row here — unlike
    the bot DM flow (bot/purchase_photo.py), this happens synchronously on
    the page the staff member is already reviewing/submitting, so there's
    nothing to persist for later. Read-only: never writes to the DB."""
    photo_bytes = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(photo_bytes) > _MAX_PHOTO_BYTES:
        return JSONResponse({"rows": [], "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)
    try:
        # See webapp/routers/repairs.py::scan_device_view for why this
        # needs run_in_threadpool — same blocking-httpx-in-async-route issue.
        raw_items = await run_in_threadpool(vision_ocr.extract_invoice_items, photo_bytes)
    except vision_ocr.VisionOcrError as exc:
        return JSONResponse({"rows": [], "error": str(exc)}, status_code=502)

    with get_conn() as conn:
        rows = core_purchase_import.match_items(conn, raw_items)
    return JSONResponse({"rows": rows})


@router.get("/draft/{draft_id}")
def draft_view(request: Request, draft_id: int, staff=Depends(require_role(*_PURCHASE_ROLES))):
    """Review page for a photo-of-invoice draft (bot/purchase_photo.py) —
    same row UI as the normal intake form, pre-filled from the OCR guess."""
    with get_conn() as conn:
        draft = core_purchases.get_draft(conn, draft_id)
        if not draft or draft["status"] != "pending":
            return RedirectResponse(link(request, "/purchases"), status_code=303)
        ctx = {
            "draft": draft,
            "draft_items": core_purchases.get_draft_items(conn, draft_id),
            "suppliers": core_purchases.list_suppliers(conn),
            "cells": core_inventory.list_cells(conn),
            **_picker_context(conn),
        }
    return render(request, "purchase_draft.html", staff=staff, **ctx)


@router.post("/draft/{draft_id}")
async def draft_submit_view(request: Request, draft_id: int, staff=Depends(require_role(*_PURCHASE_ROLES))):
    form = await request.form()
    supplier_id = form.get("supplier_id")
    invoice_no = form.get("invoice_no", "")
    row_count = int(form.get("row_count") or 0)

    with get_conn() as conn:
        draft = core_purchases.get_draft(conn, draft_id)
        if draft and draft["status"] == "pending":
            items = _parse_receipt_form_items(conn, form, row_count)
            if items:
                core_purchases.create_receipt(
                    conn, int(supplier_id) if supplier_id else None, invoice_no.strip() or None, staff["id"], items
                )
                core_purchases.mark_draft_applied(conn, draft_id)
    return RedirectResponse(link(request, "/purchases"), status_code=303)


@router.get("/{receipt_id}")
def detail_view(request: Request, receipt_id: int, staff=Depends(require_role(*_PURCHASE_ROLES))):
    with get_conn() as conn:
        receipt = core_purchases.get_receipt(conn, receipt_id)
        if not receipt:
            return RedirectResponse(link(request, "/purchases"), status_code=303)
        items = core_purchases.get_receipt_items(conn, receipt_id)
    return render(request, "purchase_detail.html", staff=staff, receipt=receipt, items=items)
