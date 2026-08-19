from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response

from core import barcode_label
from core import inventory as core_inventory
from core import print_queue
from core import vision_ocr
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, optional_int, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/inventory")

_STOCK_WRITE_ROLES = ("owner", "admin", "storekeeper")

_PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "product_photos")
_PHOTO_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)


@router.get("/products")
def products_view(
    request: Request, q: str | None = None, low_stock: bool | None = None, staff=Depends(require_staff)
):
    with get_conn() as conn:
        rows = core_inventory.list_products_with_stock(conn, search=q, low_stock_only=bool(low_stock))
    return render(request, "inventory_products.html", staff=staff, products=rows, query=q, low_stock=low_stock)


@router.post("/products")
def products_create(
    request: Request,
    name: str = Form(""),
    sku: str = Form(""),
    category: str = Form(""),
    unit: str = Form("шт"),
    min_qty: str = Form("0"),
    price: str = Form(""),
    is_repair_part: bool = Form(False),
    is_sellable: bool = Form(False),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    if not name.strip():
        with get_conn() as conn:
            rows = core_inventory.list_products_with_stock(conn)
        return render(request, "inventory_products.html", staff=staff, products=rows, query=None, low_stock=None, error="Введите название товара.")

    with get_conn() as conn:
        product_id = core_inventory.create_product(
            conn,
            name=name.strip(),
            sku=sku.strip() or None,
            category=category.strip() or None,
            unit=unit.strip() or "шт",
            is_repair_part=is_repair_part,
            is_sellable=is_sellable,
            min_qty=optional_int(min_qty) or 0,
            price=optional_int(price),
        )
    # Straight to the new product's card — that's where stock actually
    # gets added (Добавить остаток), not on this list page.
    return RedirectResponse(link(request, f"/inventory/products/{product_id}"), status_code=303)


@router.post("/products/scan-label")
async def scan_product_label_view(photo: UploadFile = File(...), staff=Depends(require_role(*_STOCK_WRITE_ROLES))):
    """Scan-to-fill button next to the SKU field (create form and product
    card edit form): photo of a barcode/label -> OpenAI vision -> {name,
    sku} to fill in client-side. No product_id — used before a product
    exists too. Deliberately never returns a price (see
    core.vision_ocr.extract_product_label). Read-only: never writes to
    the DB."""
    photo_bytes = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(photo_bytes) > _MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)

    try:
        result = vision_ocr.extract_product_label(photo_bytes)
    except vision_ocr.VisionOcrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, "name": result["name"], "sku": result["sku"]})


@router.get("/products/{product_id}")
def product_detail_view(request: Request, product_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        product = core_inventory.get_product(conn, product_id)
        if not product:
            return RedirectResponse(link(request, "/inventory/products"), status_code=303)
        stock_by_cell = core_inventory.product_stock_by_cell(conn, product_id)
        cells = core_inventory.list_cells(conn)
    return render(
        request, "inventory_product_detail.html", staff=staff,
        product=product, stock_by_cell=stock_by_cell, cells=cells,
    )


@router.get("/products/{product_id}/barcode.png")
def product_barcode_view(product_id: int, compact: bool = False, staff=Depends(require_staff)):
    """Regenerated fresh from the DB on every request — editing the name
    or price and hitting Сохранить is the only "reissue" step needed,
    the next view/print already reflects it. Encodes the product's own
    SKU verbatim (never an app-invented id), so scanning either this
    label or the part's original manufacturer barcode finds the same
    product (core.inventory.get_product_by_sku).

    ?compact=1 drops the product name — used by the print view (tap the
    barcode to flip it, "Печать этикетки") sized for a physical
    Xprinter XP-420B 30x20mm label, where a wrapped name would just
    crowd out the barcode."""
    with get_conn() as conn:
        product = core_inventory.get_product(conn, product_id)
    if not product or not product["sku"]:
        raise HTTPException(status_code=404, detail="У товара нет SKU для штрих-кода.")
    png = barcode_label.generate_label_png(product["sku"], product["name"], product["price"], compact=compact)
    return Response(content=png, media_type="image/png")


@router.post("/products/{product_id}/print-label")
def product_print_label_view(product_id: int, staff=Depends(require_role(*_STOCK_WRITE_ROLES))):
    """Enqueues a print job for print_agent.py (running on a Linux box
    on the same LAN as the Xprinter) to pick up — the CRM server has no
    network path to a printer behind a shop/home router, so this can
    only ask, not print directly. See core.print_queue."""
    with get_conn() as conn:
        product = core_inventory.get_product(conn, product_id)
        if not product or not product["sku"]:
            return JSONResponse({"ok": False, "error": "У товара нет SKU для штрих-кода."}, status_code=400)
        job_id = print_queue.create_job(conn, product_id, staff["id"])
    return JSONResponse({"ok": True, "job_id": job_id})


@router.get("/print-jobs/{job_id}/status")
def print_job_status_view(job_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        status = print_queue.job_status(conn, job_id)
    if not status:
        return JSONResponse({"ok": False, "error": "Задание не найдено."}, status_code=404)
    return JSONResponse({"ok": True, "status": status})


@router.post("/products/{product_id}/receive")
def product_receive_view(
    request: Request, product_id: int, cell_id: str = Form(""), qty: str = Form(""),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    """"Добавить остаток" on the product card — the same
    core.inventory.receive_stock() the full Приход/Склад→Движения forms
    use, just scoped to this one product so setting initial/extra stock
    doesn't require leaving the card. Stock is tracked per storage cell
    (record_movement's ledger), not as a bare number on the product, so a
    cell is required same as everywhere else stock gets adjusted."""
    with get_conn() as conn:
        cid, q = optional_int(cell_id), optional_int(qty)
        if not cid or not q:
            product = core_inventory.get_product(conn, product_id)
            stock_by_cell = core_inventory.product_stock_by_cell(conn, product_id)
            cells = core_inventory.list_cells(conn)
            return render(
                request, "inventory_product_detail.html", staff=staff,
                product=product, stock_by_cell=stock_by_cell, cells=cells,
                error="Выберите ячейку и укажите количество.",
            )
        core_inventory.receive_stock(conn, product_id, cid, q, staff["id"])
    return RedirectResponse(link(request, f"/inventory/products/{product_id}"), status_code=303)


@router.post("/products/{product_id}/edit")
def product_edit_view(
    request: Request,
    product_id: int,
    name: str = Form(""),
    sku: str = Form(""),
    category: str = Form(""),
    unit: str = Form("шт"),
    min_qty: str = Form("0"),
    price: str = Form(""),
    is_repair_part: bool = Form(False),
    is_sellable: bool = Form(False),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    with get_conn() as conn:
        if not name.strip():
            product = core_inventory.get_product(conn, product_id)
            stock_by_cell = core_inventory.product_stock_by_cell(conn, product_id)
            cells = core_inventory.list_cells(conn)
            return render(
                request, "inventory_product_detail.html", staff=staff, product=product,
                stock_by_cell=stock_by_cell, cells=cells, error="Введите название товара.",
            )
        core_inventory.update_product(
            conn, product_id,
            name=name.strip(), sku=sku.strip() or None, category=category.strip() or None,
            unit=unit.strip() or "шт", is_repair_part=is_repair_part, is_sellable=is_sellable,
            min_qty=optional_int(min_qty) or 0, price=optional_int(price),
        )
    return RedirectResponse(link(request, f"/inventory/products/{product_id}"), status_code=303)


@router.post("/products/{product_id}/photo")
async def product_photo_view(
    product_id: int, photo: UploadFile = File(...),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    """AJAX upload (see inventory_product_detail.html) — returns JSON
    rather than redirecting, so the page can show a friendly error instead
    of the native-navigation hang a plain <form> gives when e.g. a real
    phone photo trips a size limit: no response ever reaches the page to
    render, it just sits there looking dead."""
    ext = _PHOTO_EXT_BY_CONTENT_TYPE.get(photo.content_type)
    if not ext:
        return JSONResponse({"ok": False, "error": "Фото должно быть JPEG, PNG или WebP."}, status_code=400)

    data = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)

    os.makedirs(_PHOTO_DIR, exist_ok=True)
    filename = f"{product_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_PHOTO_DIR, filename), "wb") as f:
        f.write(data)

    with get_conn() as conn:
        old = core_inventory.get_product(conn, product_id)
        core_inventory.set_product_photo(conn, product_id, filename)
    if old and old["photo_path"]:
        old_path = os.path.join(_PHOTO_DIR, old["photo_path"])
        if os.path.exists(old_path):
            os.remove(old_path)

    return JSONResponse({"ok": True, "photo_url": f"/static/product_photos/{filename}"})


@router.get("/cells")
def cells_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        rows = core_inventory.list_cells(conn)
    return render(request, "inventory_cells.html", staff=staff, cells=rows)


@router.post("/cells")
def cells_create(
    request: Request,
    code: str = Form(""),
    zone: str = Form(""),
    note: str = Form(""),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    if not code.strip():
        with get_conn() as conn:
            rows = core_inventory.list_cells(conn)
        return render(request, "inventory_cells.html", staff=staff, cells=rows, error="Введите код ячейки.")

    with get_conn() as conn:
        core_inventory.create_cell(conn, code=code.strip(), zone=zone.strip() or None, note=note.strip() or None)
    return RedirectResponse(link(request, "/inventory/cells"), status_code=303)


def _movements_context(conn):
    return {
        "products": core_inventory.list_products(conn),
        "cells": core_inventory.list_cells(conn),
        "movements": core_inventory.list_movements(conn),
    }


@router.get("/movements")
def movements_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _movements_context(conn)
    return render(request, "inventory_movements.html", staff=staff, **ctx)


@router.post("/movements/receive")
def movements_receive(
    request: Request,
    product_id: str = Form(""),
    cell_id: str = Form(""),
    qty: str = Form(""),
    comment: str = Form(""),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    with get_conn() as conn:
        pid, cid, q = optional_int(product_id), optional_int(cell_id), optional_int(qty)
        if not pid or not cid or not q:
            ctx = _movements_context(conn)
            return render(request, "inventory_movements.html", staff=staff, error="Выберите товар, ячейку и количество.", **ctx)
        core_inventory.receive_stock(conn, pid, cid, q, staff["id"], comment=comment.strip() or None)
    return RedirectResponse(link(request, "/inventory/movements"), status_code=303)


@router.post("/movements/writeoff")
def movements_writeoff(
    request: Request,
    product_id: str = Form(""),
    cell_id: str = Form(""),
    qty: str = Form(""),
    comment: str = Form(""),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    with get_conn() as conn:
        pid, cid, q = optional_int(product_id), optional_int(cell_id), optional_int(qty)
        if not pid or not cid or not q or not comment.strip():
            ctx = _movements_context(conn)
            return render(request, "inventory_movements.html", staff=staff, error="Выберите товар, ячейку, количество и укажите комментарий.", **ctx)
        try:
            core_inventory.write_off_stock(conn, pid, cid, q, staff["id"], comment=comment.strip())
        except InsufficientStockError as exc:
            ctx = _movements_context(conn)
            return render(request, "inventory_movements.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, "/inventory/movements"), status_code=303)


@router.post("/movements/transfer")
def movements_transfer(
    request: Request,
    product_id: str = Form(""),
    from_cell_id: str = Form(""),
    to_cell_id: str = Form(""),
    qty: str = Form(""),
    staff=Depends(require_role(*_STOCK_WRITE_ROLES)),
):
    with get_conn() as conn:
        pid, fcid, tcid, q = optional_int(product_id), optional_int(from_cell_id), optional_int(to_cell_id), optional_int(qty)
        if not pid or not fcid or not tcid or not q:
            ctx = _movements_context(conn)
            return render(request, "inventory_movements.html", staff=staff, error="Выберите товар и обе ячейки, укажите количество.", **ctx)
        try:
            core_inventory.transfer_stock(conn, pid, fcid, tcid, q, staff["id"])
        except InsufficientStockError as exc:
            ctx = _movements_context(conn)
            return render(request, "inventory_movements.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, "/inventory/movements"), status_code=303)
