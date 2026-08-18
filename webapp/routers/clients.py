from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from core import clients as core_clients
from core import qr as core_qr
from core import repairs as core_repairs
from core import sales as core_sales
from core.storage import get_conn
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter(prefix="/clients")


@router.get("")
def list_view(request: Request, q: str | None = None, source: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        rows = core_clients.list_clients(conn, search=q, source=source)
    return render(request, "clients_list.html", staff=staff, clients=rows, query=q, source=source)


@router.post("")
def create_view(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    if not name.strip():
        with get_conn() as conn:
            rows = core_clients.list_clients(conn)
        return render(request, "clients_list.html", staff=staff, clients=rows, query=None, source=None, error="Введите имя клиента.")

    with get_conn() as conn:
        client_id = core_clients.create_client(
            conn, name=name.strip(), phone=core_clients.normalize_phone(phone) or None, notes=notes.strip() or None
        )
    return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)


@router.get("/find")
def find_view(request: Request, code: str = "", staff=Depends(require_staff)):
    client_id = core_qr.parse_client_code(code)
    with get_conn() as conn:
        client = core_clients.get_client(conn, client_id) if client_id else None
        if client:
            return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)
        rows = core_clients.list_clients(conn)
    return render(request, "clients_list.html", staff=staff, clients=rows, query=None, source=None, error="QR-код не распознан — клиент не найден.")


@router.get("/{client_id}")
def detail_view(request: Request, client_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        client = core_clients.get_client(conn, client_id)
        if not client:
            return RedirectResponse(link(request, "/clients"), status_code=303)
        repair_history = core_repairs.list_repairs_by_client(conn, client_id)
        sales_history = core_sales.list_sales_by_client(conn, client_id)
    return render(
        request, "client_detail.html", staff=staff, client=client,
        repair_history=repair_history, sales_history=sales_history,
    )


@router.get("/{client_id}/qr.png")
def qr_view(request: Request, client_id: int, staff=Depends(require_staff)):
    png = core_qr.generate_png(core_qr.client_code(client_id))
    return Response(content=png, media_type="image/png")


@router.post("/{client_id}/edit")
def edit_view(
    request: Request,
    client_id: int,
    name: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    with get_conn() as conn:
        if not name.strip():
            client = core_clients.get_client(conn, client_id)
            repair_history = core_repairs.list_repairs_by_client(conn, client_id)
            sales_history = core_sales.list_sales_by_client(conn, client_id)
            return render(
                request, "client_detail.html", staff=staff, client=client,
                repair_history=repair_history, sales_history=sales_history, error="Введите имя клиента.",
            )
        core_clients.update_client(
            conn, client_id, name=name.strip(), phone=core_clients.normalize_phone(phone) or None, notes=notes.strip() or None
        )
    return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)
