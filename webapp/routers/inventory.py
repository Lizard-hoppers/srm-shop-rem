from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import inventory as core_inventory
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, optional_int, require_staff
from webapp.templating import render

router = APIRouter(prefix="/inventory")


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
    staff=Depends(require_staff),
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
    staff=Depends(require_staff),
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
    staff=Depends(require_staff),
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
    staff=Depends(require_staff),
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
    staff=Depends(require_staff),
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
