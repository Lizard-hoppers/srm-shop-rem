from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core import locations as core_locations
from core import masters as core_masters
from core.storage import get_conn
from webapp.deps import link, optional_int, require_role
from webapp.templating import render

router = APIRouter(prefix="/masters")

_MASTERS_ROLES = ("owner", "admin")


def _clean_pay(pay_type: str, pay_value: str) -> tuple[str | None, int | None]:
    """A rate only means something paired with a value — an empty/unknown
    pay_type or a blank value leaves the master with no rate configured
    (payout computes to 0) rather than half-saving one side of it."""
    if pay_type not in core_auth.PAY_TYPES:
        return None, None
    value = optional_int(pay_value)
    if value is None:
        return None, None
    return pay_type, value


def _list_context(conn) -> dict:
    """Masters are one list for the whole business — a master's stats
    follow the master, whichever точка's repairs they worked on. `locations`
    feeds the «Точка» picker (which точка they log into / belong to)."""
    masters = core_auth.list_masters(conn, include_inactive=True)
    return {
        "masters": masters,
        "summaries": {m["id"]: core_masters.master_summary(conn, m) for m in masters},
        "locations": core_locations.list_locations(conn),
    }


@router.get("")
def list_view(request: Request, staff=Depends(require_role(*_MASTERS_ROLES))):
    with get_conn() as conn:
        ctx = _list_context(conn)
    return render(request, "masters_list.html", staff=staff, **ctx)


@router.post("")
def create_view(
    request: Request, name: str = Form(""), telegram_id: str = Form(""),
    pay_type: str = Form(""), pay_value: str = Form(""), location_id: str = Form(""),
    staff=Depends(require_role(*_MASTERS_ROLES)),
):
    if not name.strip():
        with get_conn() as conn:
            ctx = _list_context(conn)
        return render(request, "masters_list.html", staff=staff, error="Введите имя мастера.", **ctx)

    clean_type, clean_value = _clean_pay(pay_type, pay_value)
    with get_conn() as conn:
        master_id = core_auth.create_master(
            conn, name.strip(), optional_int(telegram_id), clean_type, clean_value,
            location_id=optional_int(location_id),
        )
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


@router.get("/{master_id}")
def detail_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    with get_conn() as conn:
        master = core_auth.get_master(conn, master_id)
        if not master:
            return RedirectResponse(link(request, "/masters"), status_code=303)
        summary = core_masters.master_summary(conn, master)
        locations = core_locations.list_locations(conn)
    return render(request, "master_detail.html", staff=staff, master=master, summary=summary, locations=locations)


@router.post("/{master_id}/edit")
def edit_view(
    request: Request, master_id: int, name: str = Form(""), telegram_id: str = Form(""),
    pay_type: str = Form(""), pay_value: str = Form(""), location_id: str = Form(""),
    staff=Depends(require_role(*_MASTERS_ROLES)),
):
    with get_conn() as conn:
        master = core_auth.get_master(conn, master_id)
        if not master:
            return RedirectResponse(link(request, "/masters"), status_code=303)
        if not name.strip():
            summary = core_masters.master_summary(conn, master)
            return render(
                request, "master_detail.html", staff=staff, master=master, summary=summary,
                locations=core_locations.list_locations(conn), error="Введите имя мастера.",
            )
        clean_type, clean_value = _clean_pay(pay_type, pay_value)
        core_auth.update_master(
            conn, master_id, name.strip(), optional_int(telegram_id), clean_type, clean_value,
            location_id=optional_int(location_id),
        )
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


@router.post("/{master_id}/deactivate")
def deactivate_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    with get_conn() as conn:
        core_auth.set_master_active(conn, master_id, False)
    return RedirectResponse(link(request, "/masters"), status_code=303)


@router.post("/{master_id}/activate")
def activate_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    with get_conn() as conn:
        core_auth.set_master_active(conn, master_id, True)
    return RedirectResponse(link(request, "/masters"), status_code=303)
