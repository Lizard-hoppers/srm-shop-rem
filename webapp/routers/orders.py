"""Заказы клиентов (core.orders): товар отложен за клиентом на 24 часа,
«Оплатить» и «Выдать» — отдельные кнопки. A заказ is started from the
sale form itself («Отложить на 24 часа» next to «Продать») — same rows,
same client fields."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from core import accounts as core_accounts
from core import cash as core_cash
from core import channel_posts
from core import clients as core_clients
from core import documents as core_documents
from core import orders as core_orders
from core import sales as core_sales
from core import settlements as core_settlements
from core.inventory import InsufficientStockError
from core.orders import OrderError
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, require_staff
from webapp.payments import payments_from_form
from webapp.routers import sales as sales_router
from webapp.templating import render

router = APIRouter(prefix="/orders")

_ERRORS = (OrderError, InsufficientStockError, core_cash.PaymentError, core_sales.SaleError,
           core_settlements.SettlementError)


def _view(order) -> dict:
    return {
        **dict(order), "label": core_documents.label("client_order", order["id"]),
        "status_label": core_orders.STATUS_LABELS[order["status"]],
    }


@router.get("")
def list_view(request: Request, show: str = "active", staff=Depends(require_staff)):
    with get_conn() as conn:
        core_orders.expire_due(conn)
        statuses = ("reserved", "expired") if show != "all" else None
        orders = [
            {**_view(o), **core_orders.numbers(conn, o["id"])}
            for o in core_orders.list_orders(conn, statuses=statuses, location_id=loc(request))
        ]
    return render(request, "orders_list.html", staff=staff, orders=orders, show=show)


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    """The sale form, submitted with «Отложить на 24 часа»."""
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = core_clients.normalize_phone((form.get("client_phone") or "").strip())
    items, error = sales_router.parse_rows(form)
    if not error and not client_phone:
        error = "Заказ оформляется на клиента: укажите его номер телефона."
    key = idem_key("client_order", form.get("idem"))
    order_id = None
    if not error:
        try:
            with get_conn() as conn:
                already = core_documents.find_by_key(conn, key)
                if already:
                    return RedirectResponse(link(request, f"/orders/{already['ref_id']}"), status_code=303)
                client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source="offline")
                order_id = core_orders.create_order(
                    conn, client_id, sales_router.resolve_units(conn, items), staff["id"],
                    location_id=loc(request), key=key,
                )
        except _ERRORS as exc:
            error = str(exc)
    if error:
        with get_conn() as conn:
            ctx = sales_router._list_context(conn, loc(request))
        return render(request, "sales_list.html", staff=staff, error=error, **ctx)
    return RedirectResponse(link(request, f"/orders/{order_id}"), status_code=303)


def _detail_context(conn, order_id: int) -> dict | None:
    core_orders.expire_due(conn)
    order = core_orders.get_order(conn, order_id)
    if not order:
        return None
    return {
        "order": _view(order),
        "items": core_orders.get_items(conn, order_id),
        "numbers": core_orders.numbers(conn, order_id),
        "accounts": core_accounts.list_accounts(conn, order["location_id"]),
        "position": core_settlements.client_position(conn, order["client_id"]),
        "document": core_documents.get_for(conn, "client_order", order_id),
        "sale_label": core_documents.label("sale", order["sale_id"]) if order["sale_id"] else None,
    }


def _page(request: Request, staff, order_id: int, error: str | None = None):
    with get_conn() as conn:
        ctx = _detail_context(conn, order_id)
    if not ctx:
        return RedirectResponse(link(request, "/orders"), status_code=303)
    return render(request, "order_detail.html", staff=staff, error=error, **ctx)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    return _page(request, staff, order_id)


@router.post("/{order_id}/pay")
async def pay_view(request: Request, order_id: int, staff=Depends(require_staff)):
    form = await request.form()
    key = idem_key("order_pay", form.get("idem"))
    try:
        with get_conn() as conn:
            if not core_documents.find_by_key(conn, key):
                core_orders.pay(conn, order_id, payments_from_form(form), staff["id"], key=key)
    except _ERRORS as exc:
        return _page(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/orders/{order_id}"), status_code=303)


@router.post("/{order_id}/issue")
async def issue_view(request: Request, order_id: int, staff=Depends(require_staff)):
    form = await request.form()
    key = idem_key("order_issue", form.get("idem"))
    try:
        with get_conn() as conn:
            already = core_documents.find_by_key(conn, key)
            if already:
                return RedirectResponse(link(request, f"/sales/{already['ref_id']}"), status_code=303)
            sale_id = core_orders.issue(conn, order_id, staff["id"], payments=payments_from_form(form), key=key)
            product_ids = [i["product_id"] for i in core_orders.get_items(conn, order_id)]
    except _ERRORS as exc:
        return _page(request, staff, order_id, str(exc))
    store = request.state.store
    await run_in_threadpool(channel_posts.sync_products, product_ids, store.id, store.db_path)
    return RedirectResponse(link(request, f"/sales/{sale_id}"), status_code=303)


@router.post("/{order_id}/extend")
def extend_view(request: Request, order_id: int, staff=Depends(require_staff)):
    try:
        with get_conn() as conn:
            core_orders.extend(conn, order_id, staff["id"])
    except _ERRORS as exc:
        return _page(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/orders/{order_id}"), status_code=303)


@router.post("/{order_id}/cancel")
def cancel_view(request: Request, order_id: int, staff=Depends(require_staff)):
    try:
        with get_conn() as conn:
            core_orders.cancel(conn, order_id, staff["id"])
    except _ERRORS as exc:
        return _page(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/orders/{order_id}"), status_code=303)
