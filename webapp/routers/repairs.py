from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core import clients as core_clients
from core import device_catalog
from core import inventory as core_inventory
from core import notify as core_notify
from core import repairs as core_repairs
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, optional_int, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/repairs")

_REPAIR_WRITE_ROLES = ("owner", "admin", "master")


@router.get("")
def list_view(request: Request, status: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _list_context(conn, status)
    return render(request, "repairs_list.html", staff=staff, **ctx)


def _list_context(conn, status: str | None) -> dict:
    return {
        "repairs": core_repairs.list_repairs(conn, status=status),
        "status": status,
        "masters": core_auth.list_staff(conn),
        "statuses": core_repairs.STATUSES,
        "device_types": device_catalog.list_device_types(conn),
        "device_brands": device_catalog.list_brands(conn),
        "device_catalog": [dict(r) for r in device_catalog.list_all(conn)],
    }


@router.post("")
def create_view(
    request: Request,
    client_name: str = Form(""),
    client_phone: str = Form(""),
    device_type: str = Form(""),
    brand: str = Form(""),
    model: str = Form(""),
    serial_number: str = Form(""),
    defect_description: str = Form(""),
    channel: str = Form("offline"),
    master_id: str = Form(""),
    price_estimate: str = Form(""),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    # Required fields arrive as plain strings (not typed Form(...)) so a form
    # submitted with one of them blank/missing never hits FastAPI's raw 422
    # — it re-renders the same page with a plain-Russian error instead.
    if not client_name.strip() or not client_phone.strip() or not device_type.strip():
        with get_conn() as conn:
            ctx = _list_context(conn, None)
        return render(
            request, "repairs_list.html", staff=staff, error="Заполните имя, телефон клиента и тип устройства.", **ctx
        )

    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel)
        order_id = core_repairs.create_repair(
            conn, client_id, device_type.strip(), brand.strip() or None, model.strip() or None,
            serial_number.strip() or None, defect_description.strip() or None, channel,
            optional_int(master_id), optional_int(price_estimate), staff["id"],
        )
        device_catalog.remember(conn, device_type, brand, model)
        repair = core_repairs.get_repair(conn, order_id)

    keyboard = core_repairs.render_keyboard(order_id, repair["status"])
    sent = core_notify.notify_repair_card(core_repairs.render_card_text(repair), reply_markup=keyboard)
    if sent:
        with get_conn() as conn:
            core_repairs.save_order_messages(conn, order_id, sent)
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
    request: Request, order_id: int, status: str = Form(...), comment: str = Form(""),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    with get_conn() as conn:
        core_repairs.update_status(conn, order_id, status, staff["id"], comment.strip() or None)
        repair = core_repairs.get_repair(conn, order_id)
        messages = core_repairs.get_order_messages(conn, order_id)

    core_notify.sync_repair_cards(
        messages, core_repairs.render_card_text(repair), core_repairs.render_keyboard(order_id, repair["status"])
    )
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/assign")
def assign_view(
    request: Request, order_id: int, master_id: str = Form(""),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    with get_conn() as conn:
        core_repairs.assign_master(conn, order_id, optional_int(master_id))
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/price")
def price_view(
    request: Request, order_id: int,
    price_estimate: str = Form(""), price_final: str = Form(""),
    warranty_until: str = Form(""), staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    with get_conn() as conn:
        core_repairs.set_price(conn, order_id, optional_int(price_estimate), optional_int(price_final))
        core_repairs.set_warranty(conn, order_id, warranty_until.strip() or None)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/parts")
def add_part_view(
    request: Request, order_id: int,
    product_id: str = Form(""), cell_id: str = Form(""), qty: str = Form(""),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    with get_conn() as conn:
        pid, cid, q = optional_int(product_id), optional_int(cell_id), optional_int(qty)
        if not pid or not cid or not q:
            ctx = _detail_context(conn, order_id)
            return render(request, "repair_detail.html", staff=staff, error="Выберите товар, ячейку и количество.", **ctx)
        try:
            core_inventory.record_movement(
                conn, pid, q, "repair_use", staff["id"],
                from_cell_id=cid, ref_type="repair_order", ref_id=order_id,
            )
        except InsufficientStockError as exc:
            ctx = _detail_context(conn, order_id)
            return render(request, "repair_detail.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)
