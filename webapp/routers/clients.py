from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import clients as core_clients
from core.storage import get_conn
from webapp.deps import require_staff
from webapp.templating import render

router = APIRouter(prefix="/clients")


@router.get("")
def list_view(request: Request, q: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        rows = core_clients.list_clients(conn, search=q)
    return render(request, "clients_list.html", staff=staff, clients=rows, query=q)


@router.post("")
def create_view(
    request: Request,
    name: str = Form(...),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    with get_conn() as conn:
        client_id = core_clients.create_client(
            conn, name=name.strip(), phone=phone.strip() or None, notes=notes.strip() or None
        )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@router.get("/{client_id}")
def detail_view(request: Request, client_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        client = core_clients.get_client(conn, client_id)
        devices = core_clients.get_client_devices(conn, client_id)
    if not client:
        return RedirectResponse("/clients", status_code=303)
    return render(request, "client_detail.html", staff=staff, client=client, devices=devices)


@router.post("/{client_id}/edit")
def edit_view(
    request: Request,
    client_id: int,
    name: str = Form(...),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    with get_conn() as conn:
        core_clients.update_client(
            conn, client_id, name=name.strip(), phone=phone.strip() or None, notes=notes.strip() or None
        )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
