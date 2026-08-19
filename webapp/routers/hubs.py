"""Section landing pages for the bottom tab bar — Склад and Ещё each group
several pages that don't fit as their own top-level tab."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from core import clients as core_clients
from core import inventory as core_inventory
from core import qr as core_qr
from core.storage import get_conn
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter()


@router.get("/warehouse")
def warehouse_hub(request: Request, staff=Depends(require_staff)):
    return render(request, "warehouse_hub.html", staff=staff)


@router.get("/more")
def more_hub(request: Request, staff=Depends(require_staff)):
    return render(request, "more_hub.html", staff=staff)


@router.get("/more/find")
def more_find(request: Request, code: str = "", staff=Depends(require_staff)):
    """Cross-entity QR scan-to-find (Ещё → «Сканировать QR») — unlike
    /clients/find (client codes only, used by the Clients page's own
    scanner), this tries every known code kind and jumps straight to
    wherever that thing actually lives, e.g. a product's own card, which
    already lists exactly which cell(s) hold its stock."""
    with get_conn() as conn:
        product_id = core_qr.parse_product_code(code)
        if product_id and core_inventory.get_product(conn, product_id):
            return RedirectResponse(link(request, f"/inventory/products/{product_id}"), status_code=303)

        client_id = core_qr.parse_client_code(code)
        if client_id and core_clients.get_client(conn, client_id):
            return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)

    return render(request, "more_hub.html", staff=staff, error="QR-код не распознан — ничего не найдено.")
