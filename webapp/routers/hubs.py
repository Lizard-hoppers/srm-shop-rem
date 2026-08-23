"""Section landing pages for the bottom tab bar — Склад and Ещё each group
several pages that don't fit as their own top-level tab."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse

from core import clients as core_clients
from core import inventory as core_inventory
from core import qr as core_qr
from core import vision_ocr
from core.storage import get_conn
from core.store_access import accessible_stores
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter()

_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)


@router.get("/warehouse")
def warehouse_hub(request: Request, staff=Depends(require_staff)):
    return render(request, "warehouse_hub.html", staff=staff)


@router.get("/warehouse/find")
def warehouse_find(request: Request, code: str = "", staff=Depends(require_staff)):
    """Cross-entity scan-to-find (Склад → «Сканировать код») — unlike
    /clients/find (client QR codes only, used by the Clients page's own
    scanner), this tries every known code kind and jumps straight to
    wherever that thing actually lives. A product resolves by exact SKU
    match — the scanned value is the part's own barcode digits, not an
    app-invented id (see core.barcode_label) — landing on the product's
    own card, which already lists exactly which cell(s) hold its stock.
    Lives under Склад (moved from Ещё 19.08 — most scans are for a
    product, so Склад reads more naturally), but still recognizes a
    client QR code too, same as before."""
    with get_conn() as conn:
        product = core_inventory.get_product_by_sku(conn, code)
        if product:
            return RedirectResponse(link(request, f"/inventory/products/{product['id']}"), status_code=303)

        client_id = core_qr.parse_client_code(code)
        if client_id and core_clients.get_client(conn, client_id):
            return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)

    return render(request, "warehouse_hub.html", staff=staff, error="Код не распознан — ничего не найдено.")


@router.post("/warehouse/scan-photo")
async def warehouse_scan_photo(photo: UploadFile = File(...), staff=Depends(require_staff)):
    """Photo-based alternative to a live camera feed (19.08) — snapping a
    single photo of a barcode and reading it via OpenAI vision
    (core.vision_ocr.extract_product_label, same function the SKU
    scan-fill button uses) turned out much more reliable in practice than
    a live getUserMedia+ZXing video stream, which was crashing/hanging
    Telegram Desktop's sandboxed WebKitGTK renderer. Returns a SKU for
    the client to hand to /warehouse/find, same as any other scan
    source — no redirect here, this endpoint never touches the DB."""
    photo_bytes = await photo.read(_MAX_PHOTO_BYTES + 1)
    if len(photo_bytes) > _MAX_PHOTO_BYTES:
        return JSONResponse({"ok": False, "error": "Фото слишком большое (максимум 15 МБ)."}, status_code=413)

    try:
        # See webapp/routers/repairs.py::scan_device_view for why this
        # needs run_in_threadpool — same blocking-httpx-in-async-route issue.
        result = await run_in_threadpool(vision_ocr.extract_product_label, photo_bytes)
    except vision_ocr.VisionOcrError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)

    if not result["sku"]:
        return JSONResponse({"ok": False, "error": "Не распознал штрихкод на фото — попробуйте чётче и ближе."})

    return JSONResponse({"ok": True, "sku": result["sku"]})


@router.get("/more")
def more_hub(request: Request, staff=Depends(require_staff)):
    multi_store_access = bool(staff["telegram_id"]) and len(accessible_stores(staff["telegram_id"])) > 1
    return render(request, "more_hub.html", staff=staff, multi_store_access=multi_store_access)
