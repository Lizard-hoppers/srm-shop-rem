"""«Услуги» (25.10, core.services): продать услугу формой — что за услуга,
сумма, куда оплата — последние продажи и справочник запомненных услуг (их
бот предлагает кнопками быстрого выбора)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import accounts as core_accounts
from core import documents as core_documents
from core import services as core_services
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/services")

_CATALOG_ROLES = ("owner", "admin", "manager")


def _page(request: Request, staff, error: str | None = None, success: str | None = None):
    location_id = loc(request)
    with get_conn() as conn:
        accounts = [a for a in core_accounts.list_accounts(conn, location_id) if a["currency"] == core_accounts.BASE_CURRENCY]
        catalog = core_services.list_all(conn, location_id)
        sales = core_services.recent(conn, location_id)
    return render(
        request, "services.html", staff=staff, accounts=accounts, catalog=catalog, sales=sales,
        labels={s["id"]: core_documents.label("service", s["id"]) for s in sales},
        can_edit=staff["role"] in _CATALOG_ROLES, error=error, success=success,
    )


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    return _page(request, staff)


@router.post("")
def sell_view(
    request: Request, name: str = Form(""), amount: str = Form(""), account_id: int = Form(0), idem: str = Form(""),
    staff=Depends(require_staff),
):
    key = idem_key("service", idem)
    try:
        with get_conn() as conn:
            if core_documents.find_by_key(conn, key):
                return RedirectResponse(link(request, "/services"), status_code=303)
            parsed = core_accounts.parse_amount(amount)
            sale_id = core_services.sell(conn, loc(request), name, parsed if parsed is not None else amount, account_id,
                                         staff["id"], key=key)
    except core_services.ServiceError as exc:
        return _page(request, staff, error=str(exc))
    return _page(request, staff, success=f"{core_documents.label('service', sale_id)} проведена.")


@router.post("/{service_id}/edit")
def edit_view(
    request: Request, service_id: int, name: str = Form(""), price: str = Form(""),
    staff=Depends(require_role(*_CATALOG_ROLES)),
):
    try:
        with get_conn() as conn:
            service = core_services.get(conn, service_id)
            if not service or service["location_id"] != loc(request):
                raise core_services.ServiceError("Услуга не найдена.")
            core_services.update(conn, service_id, name, core_accounts.parse_amount(price) if price.strip() else None)
    except core_services.ServiceError as exc:
        return _page(request, staff, error=str(exc))
    return RedirectResponse(link(request, "/services"), status_code=303)


@router.post("/{service_id}/hide")
def hide_view(request: Request, service_id: int, staff=Depends(require_role(*_CATALOG_ROLES))):
    with get_conn() as conn:
        service = core_services.get(conn, service_id)
        if service and service["location_id"] == loc(request):
            core_services.hide(conn, service_id)
    return RedirectResponse(link(request, "/services"), status_code=303)
