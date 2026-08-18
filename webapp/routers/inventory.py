from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse

from core import inventory as core_inventory
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
        core_inventory.create_product(
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
    return RedirectResponse(link(request, "/inventory/products"), status_code=303)


@router.get("/products/{product_id}")
def product_detail_view(request: Request, product_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        product = core_inventory.get_product(conn, product_id)
        if not product:
            return RedirectResponse(link(request, "/inventory/products"), status_code=303)
        stock_by_cell = core_inventory.product_stock_by_cell(conn, product_id)
    return render(request, "inventory_product_detail.html", staff=staff, product=product, stock_by_cell=stock_by_cell)


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
            return render(
                request, "inventory_product_detail.html", staff=staff, product=product,
                stock_by_cell=stock_by_cell, error="Введите название товара.",
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
