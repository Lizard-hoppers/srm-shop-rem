from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core import clients as core_clients
from core import inventory as core_inventory
from core import repairs as core_repairs
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, optional_int, require_staff
from webapp.templating import render

router = APIRouter(prefix="/repairs")


@router.get("")
def list_view(request: Request, status: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        rows = core_repairs.list_repairs(conn, status=status)
        masters = core_auth.list_staff(conn)
    return render(
        request, "repairs_list.html", staff=staff, repairs=rows, status=status, masters=masters,
        statuses=core_repairs.STATUSES,
    )


@router.post("")
def create_view(
    request: Request,
    client_name: str = Form(...),
    client_phone: str = Form(...),
    device_type: str = Form(...),
    brand: str = Form(""),
    model: str = Form(""),
    serial_number: str = Form(""),
    defect_description: str = Form(""),
    channel: str = Form("offline"),
    master_id: str = Form(""),
    price_estimate: str = Form(""),
    staff=Depends(require_staff),
):
    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel)
        order_id = core_repairs.create_repair(
            conn, client_id, device_type.strip(), brand.strip() or None, model.strip() or None,
            serial_number.strip() or None, defect_description.strip() or None, channel,
            optional_int(master_id), optional_int(price_estimate), staff["id"],
        )
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


def _detail_context(conn, order_id: int) -> dict:
    return {
        "repair": core_repairs.get_repair(conn, order_id),
        "history": core_repairs.get_status_history(conn, order_id),
        "parts": core_repairs.get_used_parts(conn, order_id),
        "masters": core_auth.list_staff(conn),
        "products": core_inventory.list_products(conn),
        "cells": core_inventory.list_cells(conn),
        "statuses": core_repairs.STATUSES,
        "status_labels": core_repairs.STATUS_LABELS,
    }


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _detail_context(conn, order_id)
    if not ctx["repair"]:
        return RedirectResponse(link(request, "/repairs"), status_code=303)
    return render(request, "repair_detail.html", staff=staff, **ctx)


@router.post("/{order_id}/status")
def status_view(
    request: Request, order_id: int, status: str = Form(...), comment: str = Form(""), staff=Depends(require_staff)
):
    with get_conn() as conn:
        core_repairs.update_status(conn, order_id, status, staff["id"], comment.strip() or None)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/assign")
def assign_view(request: Request, order_id: int, master_id: str = Form(""), staff=Depends(require_staff)):
    with get_conn() as conn:
        core_repairs.assign_master(conn, order_id, optional_int(master_id))
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/price")
def price_view(
    request: Request, order_id: int,
    price_estimate: str = Form(""), price_final: str = Form(""),
    warranty_until: str = Form(""), staff=Depends(require_staff),
):
    with get_conn() as conn:
        core_repairs.set_price(conn, order_id, optional_int(price_estimate), optional_int(price_final))
        core_repairs.set_warranty(conn, order_id, warranty_until.strip() or None)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/parts")
def add_part_view(
    request: Request, order_id: int,
    product_id: int = Form(...), cell_id: int = Form(...), qty: int = Form(...),
    staff=Depends(require_staff),
):
    with get_conn() as conn:
        try:
            core_inventory.record_movement(
                conn, product_id, qty, "repair_use", staff["id"],
                from_cell_id=cell_id, ref_type="repair_order", ref_id=order_id,
            )
        except InsufficientStockError as exc:
            ctx = _detail_context(conn, order_id)
            return render(request, "repair_detail.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)
