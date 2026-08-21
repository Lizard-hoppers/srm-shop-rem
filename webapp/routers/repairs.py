from __future__ import annotations

import os
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from core import auth as core_auth
from core import clients as core_clients
from core import device_catalog
from core import inventory as core_inventory
from core import notify as core_notify
from core import photos as core_photos
from core import repairs as core_repairs
from core import vision_ocr
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import link, optional_int, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/repairs")

_REPAIR_WRITE_ROLES = ("owner", "admin", "master")

_PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "device_photos")
_PHOTO_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)

INITIAL_DEVICE_ROWS = 1


def _write_device_photo(device_id: int, data: bytes, ext: str) -> str:
    compressed = core_photos.compress_photo(data)
    if compressed is not None:
        data, ext = compressed, ".jpg"

    os.makedirs(_PHOTO_DIR, exist_ok=True)
    filename = f"{device_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(_PHOTO_DIR, filename), "wb") as f:
        f.write(data)
    return filename


async def _validate_intake_photo(upload) -> tuple[bytes, str] | None:
    """None if this device row's file input was left empty — perfectly
    normal, a photo at intake is optional. Raises ValueError with a
    user-facing message on a real but invalid upload (wrong type, too
    big) so create_view can reject the whole submission before creating
    anything, rather than silently dropping just that one photo."""
    # request.form() (used here instead of typed File() params, so a
    # dynamic device_count can drive how many photo_N fields exist) hands
    # back Starlette's UploadFile, not fastapi.UploadFile — the two are
    # unrelated classes in this FastAPI version, so isinstance must check
    # against the Starlette one or every real upload silently reads as
    # "no file chosen".
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        return None
    ext = _PHOTO_EXT_BY_CONTENT_TYPE.get(upload.content_type)
    if not ext:
        raise ValueError("Фото устройства должно быть JPEG, PNG или WebP.")
    data = await upload.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        raise ValueError("Фото устройства слишком большое (максимум 15 МБ).")
    return data, ext


@router.post("/scan-device")
async def scan_device_view(photo: UploadFile = File(...), staff=Depends(require_role(*_REPAIR_WRITE_ROLES))):
    """Scan-to-fill button next to Серийный №/IMEI on the intake form
    (repairs_list.html): photo of the device (box label, back-panel
    engraving, or an "About phone" settings screen) -> OpenAI vision ->
    best-effort device_type/brand/model/serial_number to fill in
    client-side. No repair exists yet at this point — read-only, never
    writes to the DB."""
    photo_bytes = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(photo_bytes) > _MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)

    try:
        result = vision_ocr.extract_device_info(photo_bytes)
    except vision_ocr.VisionOcrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, **result})


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
        "device_rows": range(INITIAL_DEVICE_ROWS),
    }


@router.post("")
async def create_view(request: Request, staff=Depends(require_role(*_REPAIR_WRITE_ROLES))):
    """One client can drop off several devices in the same visit —
    "+ Добавить ещё устройство" on the intake form grows device_count
    past INITIAL_DEVICE_ROWS, each row becoming its own repair order
    (client/phone/channel/master shared, everything else per device).
    Every field arrives as a plain form value (not typed Form(...)) so a
    submission missing something never hits FastAPI's raw 422 — it
    re-renders the same page with a plain-Russian error instead."""
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = core_clients.normalize_phone((form.get("client_phone") or "").strip())
    channel = form.get("channel") or "offline"
    master_id = form.get("master_id") or ""
    device_count = int(form.get("device_count") or INITIAL_DEVICE_ROWS)

    if not client_name or not client_phone:
        with get_conn() as conn:
            ctx = _list_context(conn, None)
        return render(request, "repairs_list.html", staff=staff, error="Заполните имя и телефон клиента.", **ctx)

    devices = []
    for i in range(device_count):
        device_type = (form.get(f"device_type_{i}") or "").strip()
        brand = (form.get(f"brand_{i}") or "").strip()
        model = (form.get(f"model_{i}") or "").strip()
        serial_number = (form.get(f"serial_number_{i}") or "").strip()
        defect_description = (form.get(f"defect_description_{i}") or "").strip()
        price_estimate = form.get(f"price_estimate_{i}") or ""

        if not any((device_type, brand, model, serial_number, defect_description)):
            continue  # untouched row past the first one — sparse rows are fine

        if not device_type:
            with get_conn() as conn:
                ctx = _list_context(conn, None)
            return render(
                request, "repairs_list.html", staff=staff,
                error=f"Укажите тип устройства для каждого добавленного устройства (устройство {i + 1}).", **ctx,
            )

        try:
            photo = await _validate_intake_photo(form.get(f"photo_{i}"))
        except ValueError as exc:
            with get_conn() as conn:
                ctx = _list_context(conn, None)
            return render(request, "repairs_list.html", staff=staff, error=str(exc), **ctx)

        devices.append({
            "device_type": device_type, "brand": brand or None, "model": model or None,
            "serial_number": serial_number or None, "defect_description": defect_description or None,
            "price_estimate": optional_int(price_estimate), "photo": photo,
        })

    if not devices:
        with get_conn() as conn:
            ctx = _list_context(conn, None)
        return render(request, "repairs_list.html", staff=staff, error="Добавьте хотя бы одно устройство.", **ctx)

    last_order_id = None
    with get_conn() as conn:
        client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel)
        for device in devices:
            order_id = core_repairs.create_repair(
                conn, client_id, device["device_type"], device["brand"], device["model"],
                device["serial_number"], device["defect_description"], channel,
                optional_int(master_id), device["price_estimate"], staff["id"],
            )
            device_catalog.remember(conn, device["device_type"], device["brand"], device["model"])

            photo_for_notify = None
            if device["photo"]:
                data, ext = device["photo"]
                repair = core_repairs.get_repair(conn, order_id)
                filename = _write_device_photo(repair["device_id"], data, ext)
                core_repairs.set_device_photo(conn, repair["device_id"], filename)
                photo_for_notify = (data, filename)

            # The card only goes out once its device is fully on record —
            # photo included, if there is one — never text-first with the
            # photo trickling in later via a separate trip to the card.
            repair = core_repairs.get_repair(conn, order_id)
            keyboard = core_repairs.render_keyboard(order_id, repair["status"])
            sent = core_notify.notify_repair_card(
                core_repairs.render_card_text(repair), reply_markup=keyboard, photo=photo_for_notify
            )
            if sent:
                core_repairs.save_order_messages(conn, order_id, sent)

            last_order_id = order_id

    return RedirectResponse(link(request, f"/repairs/{last_order_id}"), status_code=303)


def _detail_context(conn, order_id: int) -> dict:
    return {
        "repair": core_repairs.get_repair(conn, order_id),
        "history": core_repairs.get_status_history(conn, order_id),
        "parts": core_repairs.get_used_parts(conn, order_id),
        "attachments": core_repairs.get_attachments(conn, order_id),
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


@router.post("/{order_id}/photo")
async def repair_photo_view(
    order_id: int, photo: UploadFile = File(...),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    """AJAX upload (see repair_detail.html), mirrors
    inventory.product_photo_view exactly — same size/type validation and
    JSON response shape, so the shared photo-upload.js works unchanged.
    The photo lives on devices.photo_path (not repair_orders), since the
    photo documents the device, not this particular repair pass."""
    ext = _PHOTO_EXT_BY_CONTENT_TYPE.get(photo.content_type)
    if not ext:
        return JSONResponse({"ok": False, "error": "Фото должно быть JPEG, PNG или WebP."}, status_code=400)

    data = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)

    with get_conn() as conn:
        repair = core_repairs.get_repair(conn, order_id)
        if not repair:
            return JSONResponse({"ok": False, "error": "Ремонт не найден."}, status_code=404)
        device_id = repair["device_id"]
        old_photo_path = repair["device_photo_path"]

    filename = _write_device_photo(device_id, data, ext)

    with get_conn() as conn:
        core_repairs.set_device_photo(conn, device_id, filename)
    if old_photo_path:
        old_path = os.path.join(_PHOTO_DIR, old_photo_path)
        if os.path.exists(old_path):
            os.remove(old_path)

    return JSONResponse({"ok": True, "photo_url": f"/static/device_photos/{filename}"})


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
