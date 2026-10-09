from __future__ import annotations

import os

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from core import channel_posts
from core import accounts as core_accounts
from core import auth as core_auth
from core import cash as core_cash
from core import clients as core_clients
from core import device_catalog
from core import documents as core_documents
from core import inventory as core_inventory
from core import notify as core_notify
from core import repair_notes as core_repair_notes
from core import repairs as core_repairs
from core import vision_ocr
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, optional_int, require_role, require_staff
from webapp.payments import payments_from_form
from webapp.templating import render

router = APIRouter(prefix="/repairs")

_REPAIR_WRITE_ROLES = ("owner", "admin", "master")

_PHOTO_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)

INITIAL_DEVICE_ROWS = 1


async def _validate_intake_photo(upload) -> tuple[bytes, str] | None:
    """None if this device row's file input was left empty — create_view
    itself turns that into a "photo required" error for touched rows, so
    an empty file input isn't rejected here. Raises ValueError with a
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
        # run_in_threadpool: this is a plain sync httpx call to OpenAI
        # (core.vision_ocr) inside an async route — awaited directly it
        # would block the single uvicorn event loop (no --workers) for
        # every other request on the server for the whole OpenAI round
        # trip (up to the 30s timeout). Threadpool keeps this request
        # waiting on its own result (expected — "scanning a photo" takes a
        # moment) without freezing anyone else.
        result = await run_in_threadpool(vision_ocr.extract_device_info, photo_bytes)
    except vision_ocr.VisionOcrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    return JSONResponse({"ok": True, **result})


@router.get("")
def list_view(request: Request, status: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _list_context(conn, status, loc(request))
    return render(request, "repairs_list.html", staff=staff, **ctx)


def _list_context(conn, status: str | None, location_id: int | None = None) -> dict:
    return {
        "repairs": core_repairs.list_repairs(conn, status=status, location_id=location_id),
        "status": status,
        "masters": core_auth.list_staff(conn),
        "statuses": core_repairs.STATUSES,
        "device_types": device_catalog.list_device_types(conn),
        "device_brands": device_catalog.list_brands(conn),
        "device_catalog": [dict(r) for r in device_catalog.list_all(conn)],
        "device_rows": range(INITIAL_DEVICE_ROWS),
    }


@router.post("")
async def create_view(
    request: Request, background_tasks: BackgroundTasks, staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
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
            ctx = _list_context(conn, None, loc(request))
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
                ctx = _list_context(conn, None, loc(request))
            return render(
                request, "repairs_list.html", staff=staff,
                error=f"Укажите тип устройства для каждого добавленного устройства (устройство {i + 1}).", **ctx,
            )

        if not model:
            with get_conn() as conn:
                ctx = _list_context(conn, None, loc(request))
            return render(
                request, "repairs_list.html", staff=staff,
                error=f"Укажите модель устройства для каждого добавленного устройства (устройство {i + 1}).", **ctx,
            )

        if not defect_description:
            with get_conn() as conn:
                ctx = _list_context(conn, None, loc(request))
            return render(
                request, "repairs_list.html", staff=staff,
                error=f"Опишите неисправность для каждого добавленного устройства (устройство {i + 1}).", **ctx,
            )

        try:
            photo = await _validate_intake_photo(form.get(f"photo_{i}"))
        except ValueError as exc:
            with get_conn() as conn:
                ctx = _list_context(conn, None, loc(request))
            return render(request, "repairs_list.html", staff=staff, error=str(exc), **ctx)

        if not photo:
            with get_conn() as conn:
                ctx = _list_context(conn, None, loc(request))
            return render(
                request, "repairs_list.html", staff=staff,
                error=f"Загрузите фото устройства для каждого добавленного устройства (устройство {i + 1}).", **ctx,
            )

        devices.append({
            "device_type": device_type, "brand": brand or None, "model": model or None,
            "serial_number": serial_number or None, "defect_description": defect_description or None,
            "price_estimate": optional_int(price_estimate), "photo": photo,
        })

    if not devices:
        with get_conn() as conn:
            ctx = _list_context(conn, None, loc(request))
        return render(request, "repairs_list.html", staff=staff, error="Добавьте хотя бы одно устройство.", **ctx)

    last_order_id = None
    store = request.state.store
    idem = form.get("idem")
    with get_conn() as conn:
        # A repeat of a submit that already went through (see
        # webapp.deps.idem_key) — land on what it created, create nothing.
        already = core_documents.find_by_key(conn, idem_key("repair", f"{idem}:0" if idem else None))
        if already:
            return RedirectResponse(link(request, f"/repairs/{already['ref_id']}"), status_code=303)
        for index, device in enumerate(devices):
            # core_repairs.create_repair_intake does the client/repair/photo/
            # catalog writes (shared with the bot's quick-intake FSM — see
            # its own docstring). Not threadpooled: it needs `conn`, which
            # is thread-affine (sqlite3, no check_same_thread=False) — the
            # brief Pillow compress it does inline is an acceptable trade
            # for one shared code path instead of two that could drift.
            order_id, card_text, keyboard, photo_for_notify = core_repairs.create_repair_intake(
                conn,
                client_name=client_name, client_phone=client_phone,
                device_type=device["device_type"], brand=device["brand"], model=device["model"],
                serial_number=device["serial_number"], defect_description=device["defect_description"],
                channel=channel, master_id=optional_int(master_id), price_estimate=device["price_estimate"],
                staff_id=staff["id"], photo=device["photo"],
                location_id=store.location_id, key=idem_key("repair", f"{idem}:{index}" if idem else None),
            )

            # The card only goes out once its device is fully on record —
            # photo included, if there is one — never text-first with the
            # photo trickling in later via a separate trip to the card.
            # Posting it (and persisting the resulting message ids) happens
            # as a background task, not here — see core_repairs.notify_and_save's
            # docstring: staff shouldn't wait on Telegram to see their own
            # intake confirmed.
            background_tasks.add_task(core_repairs.notify_and_save, store, order_id, card_text, keyboard, photo_for_notify)

            last_order_id = order_id

    return RedirectResponse(link(request, f"/repairs/{last_order_id}"), status_code=303)


def _parts_from(conn, repair) -> str | None:
    from core import warehouses as core_warehouses

    warehouse = core_repairs.parts_warehouse(conn, repair)
    return core_warehouses.display_name(warehouse) if warehouse else None


def _sync_cards(background_tasks: BackgroundTasks, conn, order_id: int) -> None:
    """Queue the Telegram cards' refresh after anything that changes what
    they show (status, the parts line)."""
    text, keyboard = core_repairs.card(conn, order_id)
    background_tasks.add_task(
        core_notify.sync_repair_cards, core_repairs.get_order_messages(conn, order_id), text, keyboard,
    )


def _detail_context(conn, order_id: int, location_id: int | None = None) -> dict:
    repair = core_repairs.get_repair(conn, order_id)
    return {
        "repair": repair,
        # The repair is paid into the accounts of the точка that took it
        # in, whichever точка the page happens to be opened from.
        "accounts": core_accounts.list_accounts(conn, repair["location_id"]) if repair else [],
        "payments": core_cash.payments_for(conn, "repair_order", order_id),
        # Parts come from the склад of whoever does the repair (the
        # master's own, see core.repairs.parts_warehouse) — one line per
        # партия; and the repair's money for whoever may see it.
        "part_lines": core_repairs.available_parts(conn, order_id) if repair else [],
        "parts_from": _parts_from(conn, repair) if repair else None,
        "finance": core_repairs.finance(conn, order_id) if repair else None,
        "needs_part": core_repairs.needs_part(conn, repair) if repair else False,
        "history": core_repairs.get_status_history(conn, order_id),
        "parts": core_repairs.get_used_parts(conn, order_id),
        "attachments": core_repairs.get_attachments(conn, order_id),
        "notes": core_repair_notes.list_notes(conn, order_id),
        "masters": core_auth.list_staff(conn),
        "products": core_inventory.list_products(conn),
        "cells": core_inventory.list_cells(conn, location_id),
        "document": (document := core_documents.get_for(conn, "repair", order_id)),
        "document_label": core_documents.doc_label(document) if document else None,
        "statuses": core_repairs.STATUSES,
        "status_labels": core_repairs.STATUS_LABELS,
    }


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _detail_context(conn, order_id, loc(request))
    if not ctx["repair"]:
        return RedirectResponse(link(request, "/repairs"), status_code=303)
    return render(request, "repair_detail.html", staff=staff, **ctx)


@router.post("/{order_id}/status")
async def status_view(
    request: Request, background_tasks: BackgroundTasks, order_id: int,
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    """A repair transitioning INTO 'issued' (not already there — re-saving
    an already-issued repair's comment must never double-charge the
    касса) with a price on it needs a payment method, and the price gets
    recorded as касса income right here — this is the one place a repair
    actually changes hands for money in this app's flow."""
    # The form is read by hand (not typed Form params): the «Оплата» block
    # has one field per account of the точка, a set no signature can list.
    form = await request.form()
    status = form.get("status") or ""
    comment = form.get("comment") or ""
    payment_method = form.get("payment_method") or ""
    payments = payments_from_form(form)

    with get_conn() as conn:
        current = core_repairs.get_repair(conn, order_id)
        if not current or status not in core_repairs.STATUS_LABELS:
            return RedirectResponse(link(request, "/repairs"), status_code=303)
        becoming_issued = status == "issued" and current["status"] != "issued"
        takes_money = becoming_issued and current["price_final"]

        resolved = None
        if takes_money:
            # How it was paid has to be said explicitly — into which
            # account(s) of the точка that took the repair in. (The old
            # single «способ оплаты» field still works: cash / card.)
            error = None
            if not payments and payment_method not in core_cash.METHODS:
                error = "Укажите способ оплаты, чтобы отметить ремонт как «Выдан»."
            else:
                try:
                    resolved = core_cash.resolve_payments(
                        conn, current["location_id"], current["price_final"], payments or None, payment_method,
                    )
                except core_cash.PaymentError as exc:
                    error = str(exc)
            if error:
                ctx = _detail_context(conn, order_id, loc(request))
                return render(request, "repair_detail.html", staff=staff, error=error, **ctx)

        core_repairs.update_status(conn, order_id, status, staff["id"], comment.strip() or None)
        repair = core_repairs.get_repair(conn, order_id)

        if resolved:
            core_cash.record_payments(conn, "income", resolved, "repair_order", order_id, staff["id"])

        # Background, not awaited here: the 1-2 sequential Telegram edit
        # calls are blocking httpx — in this async route they would hold
        # the event loop, and Павел's own click would wait on them.
        _sync_cards(background_tasks, conn, order_id)
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

    filename = await run_in_threadpool(core_repairs.write_device_photo, device_id, data, ext)

    with get_conn() as conn:
        core_repairs.set_device_photo(conn, device_id, filename)
    if old_photo_path:
        old_path = os.path.join(core_repairs.PHOTO_DIR, old_photo_path)
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


@router.post("/{order_id}/no-parts")
def no_parts_view(
    request: Request, background_tasks: BackgroundTasks, order_id: int,
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    """«Без запчасти» — said explicitly, so an empty parts list never
    reads as «forgot to say»."""
    with get_conn() as conn:
        if core_repairs.get_repair(conn, order_id):
            core_repairs.declare_no_parts(conn, order_id)
            _sync_cards(background_tasks, conn, order_id)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)


@router.post("/{order_id}/parts")
def add_part_view(
    request: Request, background_tasks: BackgroundTasks, order_id: int,
    product_id: str = Form(""), cell_id: str = Form(""), qty: str = Form(""), line: str = Form(""),
    staff=Depends(require_role(*_REPAIR_WRITE_ROLES)),
):
    # `line` is "batch:cell" — a specific партия on the executor's склад
    # (the form on repair_detail.html). The older product+cell fields
    # below it are still accepted (oldest партия of that cell first).
    batch_part, _, cell_part = line.partition(":")
    if batch_part.isdigit() and cell_part.isdigit():
        with get_conn() as conn:
            try:
                core_repairs.use_part(
                    conn, order_id, int(batch_part), int(cell_part), optional_int(qty) or 1, staff["id"],
                )
            except (core_repairs.RepairPartError, InsufficientStockError) as exc:
                ctx = _detail_context(conn, order_id, loc(request))
                if not ctx["repair"]:
                    return RedirectResponse(link(request, "/repairs"), status_code=303)
                return render(request, "repair_detail.html", staff=staff, error=str(exc), **ctx)
            product_id_used = conn.execute("SELECT product_id FROM batches WHERE id = ?", (int(batch_part),)).fetchone()
            _sync_cards(background_tasks, conn, order_id)
        store = request.state.store
        if product_id_used:
            channel_posts.sync_products([product_id_used["product_id"]], store.id, store.db_path)
        return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)

    with get_conn() as conn:
        pid, cid, q = optional_int(product_id), optional_int(cell_id), optional_int(qty)
        if not pid or not cid or not q:
            ctx = _detail_context(conn, order_id, loc(request))
            return render(request, "repair_detail.html", staff=staff, error="Выберите товар, ячейку и количество.", **ctx)
        try:
            core_inventory.record_movement(
                conn, pid, q, "repair_use", staff["id"],
                from_cell_id=cid, ref_type="repair_order", ref_id=order_id,
            )
        except InsufficientStockError as exc:
            ctx = _detail_context(conn, order_id, loc(request))
            return render(request, "repair_detail.html", staff=staff, error=str(exc), **ctx)
    store = request.state.store
    channel_posts.sync_products([pid], store.id, store.db_path)
    return RedirectResponse(link(request, f"/repairs/{order_id}"), status_code=303)
