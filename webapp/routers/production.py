"""Наши ремонты и производство (core.production) — the Mini App pages for
the макет «Карточка и производство»: подобрать детали со всех складов →
зарезервировать → выбрать мастера → передать в производство → отчёт
мастера → принять результат. One page per order; what it shows depends on
the order's status.

Who does what: starting an order, picking parts, the master and the
handover, accepting the result — stock staff (owner/admin/storekeeper).
The report — the order's own master, or stock staff on his behalf.
Everyone on staff can look.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core import documents as core_documents
from core import inventory as core_inventory
from core import links as core_links
from core import masters as core_masters
from core import notify as core_notify
from core import production as core_production
from core import warehouses as core_warehouses
from core.production import ProductionError
from core.stock_transfers import TransferError
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, optional_int, require_staff
from webapp.templating import render

router = APIRouter(prefix="/production")

_MANAGE_ROLES = ("owner", "admin", "storekeeper")
EXTRA_ROWS = 4


def _can_manage(staff) -> bool:
    return staff["role"] in _MANAGE_ROLES


def _can_report(staff, order) -> bool:
    return _can_manage(staff) or (staff["role"] == "master" and order["master_id"] == staff["id"])


def _view(order) -> dict:
    return {
        **dict(order), "label": core_documents.label("production", order["id"]),
        "status_label": core_production.STATUS_LABELS[order["status"]],
    }


@router.get("")
def list_view(request: Request, show: str = "active", staff=Depends(require_staff)):
    """A master sees his own orders; everyone else — the точка's."""
    statuses = core_production.ACTIVE_STATUSES if show != "done" else ("done", "cancelled")
    with get_conn() as conn:
        if staff["role"] == "master":
            orders = core_production.list_orders(conn, statuses=statuses, master_id=staff["id"])
        else:
            orders = core_production.list_orders(conn, statuses=statuses, location_id=loc(request))
        # Phones of this точка that can be sent to производство: serial
        # units on hand, free, not already in an order.
        units = [
            line for line in core_inventory.stock_lines(conn, location_id=loc(request))
            if line["is_serial"] and line["imei"] and line["qty"] - line["reserved"] > 0
        ] if _can_manage(staff) else []
    return render(
        request, "production_list.html", staff=staff, orders=[_view(o) for o in orders], show=show,
        units=units, can_manage=_can_manage(staff),
    )


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    if not _can_manage(staff):
        return RedirectResponse(link(request, "/production"), status_code=303)
    key = idem_key("production", form.get("idem"))
    try:
        with get_conn() as conn:
            already = core_documents.find_by_key(conn, key)
            if already:
                return RedirectResponse(link(request, f"/production/{already['ref_id']}"), status_code=303)
            order_id = core_production.create_order(
                conn, imei=form.get("imei") or "", staff_id=staff["id"], location_id=loc(request),
                task=form.get("task"), key=key,
            )
    except ProductionError as exc:
        with get_conn() as conn:
            orders = core_production.list_orders(conn, statuses=core_production.ACTIVE_STATUSES, location_id=loc(request))
            units = [
                line for line in core_inventory.stock_lines(conn, location_id=loc(request))
                if line["is_serial"] and line["imei"] and line["qty"] - line["reserved"] > 0
            ]
        return render(
            request, "production_list.html", staff=staff, orders=[_view(o) for o in orders], show="active",
            units=units, can_manage=True, error=str(exc),
        )
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


def _detail_context(conn, staff, order_id: int, search: str = "", kind: str = "") -> dict | None:
    order = core_production.get_order(conn, order_id)
    if not order:
        return None
    parts = core_production.get_parts(conn, order_id)
    picked = {(p["batch_id"], p["cell_id"]): p["qty"] for p in parts}
    available = []
    if order["status"] == "draft":
        for line in core_production.available_parts(conn, order_id, search):
            warehouse = core_warehouses.get_warehouse(conn, line["warehouse_id"])
            available.append({
                **line, "warehouse_display": core_warehouses.display_name(warehouse),
                "picked": picked.get((line["batch_id"], line["cell_id"]), 0),
            })
        # A search must never silently drop what is already picked.
        shown = {(l["batch_id"], l["cell_id"]) for l in available}
        hidden_picked = [p for p in parts if (p["batch_id"], p["cell_id"]) not in shown]
    else:
        hidden_picked = []
    masters = [m for m in core_auth.list_masters(conn) if not kind or (m["master_kind"] or "staff") == kind]
    document = core_documents.get_for(conn, "production", order_id)
    return {
        "order": _view(order),
        "parts": parts,
        "extras": core_production.get_extras(conn, order_id),
        "available": available,
        "hidden_picked": hidden_picked,
        "search": search,
        "kind": kind,
        "masters": masters,
        "master_kinds": core_auth.MASTER_KINDS,
        "numbers": core_production.preview(conn, order_id),
        "extra_rows": range(EXTRA_ROWS),
        "can_manage": _can_manage(staff),
        "can_report": _can_report(staff, order),
        "document": document,
        "master_owed": core_masters.accrued_for(conn, "production_order", order_id),
    }


def _error(request: Request, staff, order_id: int, message: str):
    with get_conn() as conn:
        ctx = _detail_context(conn, staff, order_id)
    if not ctx:
        return RedirectResponse(link(request, "/production"), status_code=303)
    return render(request, "production_detail.html", staff=staff, error=message, **ctx)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, q: str = "", kind: str = "", staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _detail_context(conn, staff, order_id, q, kind if kind in core_auth.MASTER_KINDS else "")
    if not ctx:
        return RedirectResponse(link(request, "/production"), status_code=303)
    return render(request, "production_detail.html", staff=staff, **ctx)


@router.post("/{order_id}/parts")
async def parts_view(request: Request, order_id: int, staff=Depends(require_staff)):
    """«Зарезервировать»: every `qty_<batch>_<cell>` field with a number in
    it is a part the order holds; everything else it held is let go."""
    form = await request.form()
    lines = []
    for name, value in form.items():
        if not name.startswith("qty_") or not isinstance(value, str) or not value.strip():
            continue
        pieces = name.split("_")
        if len(pieces) == 3 and pieces[1].isdigit() and pieces[2].isdigit() and value.strip().isdigit():
            lines.append((int(pieces[1]), int(pieces[2]), int(value.strip())))
    try:
        with get_conn() as conn:
            if not _can_manage(staff):
                raise ProductionError("Подбирать детали может кладовщик, админ или владелец.")
            core_production.set_parts(conn, order_id, lines, staff["id"])
    except (ProductionError, core_inventory.InsufficientStockError) as exc:
        return _error(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


@router.post("/{order_id}/master")
async def master_view(request: Request, order_id: int, staff=Depends(require_staff)):
    form = await request.form()
    try:
        with get_conn() as conn:
            if not _can_manage(staff):
                raise ProductionError("Назначать мастера может кладовщик, админ или владелец.")
            core_production.assign_master(
                conn, order_id, optional_int(form.get("master_id") or ""), form.get("work_price"),
            )
    except ProductionError as exc:
        return _error(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


def notify_master(store, order_id: int) -> None:
    """Tell the master a job has been handed to him, with a button
    straight to its report page. Private chat with him only (the link
    carries his own session). Best effort; blocking httpx — runs as a
    background task."""
    with get_conn(store.db_path) as conn:
        order = core_production.get_order(conn, order_id)
        master = core_auth.get_master(conn, order["master_id"]) if order and order["master_id"] else None
        parts = core_production.get_parts(conn, order_id) if order else []
    if not master or not master["telegram_id"]:
        return
    label = core_documents.label("production", order_id)
    lines = [f"🛠 <b>{label} — передано вам</b>", f"{order['product_name']} · IMEI {order['imei']}"]
    if order["task"]:
        lines.append(f"Задача: {order['task']}")
    lines += [f"• {p['product_name']} · партия {p['batch_id']} — {p['qty']} {p['unit']}" for p in parts]
    lines.append("")
    lines.append("Когда закончите — сдайте отчёт: что использовали, что добавили своего.")
    url = core_links.miniapp_link(f"/production/{order_id}", master["id"], store.id)
    markup = {"inline_keyboard": [[{"text": "📝 Отчёт мастера", "web_app": {"url": url}}]]} if url else None
    core_notify.send_card(master["telegram_id"], "\n".join(lines), reply_markup=markup)


@router.post("/{order_id}/handover")
def handover_view(
    request: Request, background_tasks: BackgroundTasks, order_id: int, staff=Depends(require_staff),
):
    try:
        with get_conn() as conn:
            if not _can_manage(staff):
                raise ProductionError("Передавать в производство может кладовщик, админ или владелец.")
            core_production.hand_over(conn, order_id, staff["id"])
    except (ProductionError, TransferError, core_inventory.InsufficientStockError) as exc:
        return _error(request, staff, order_id, str(exc))
    background_tasks.add_task(notify_master, request.state.store, order_id)
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


@router.post("/{order_id}/report")
async def report_view(request: Request, order_id: int, staff=Depends(require_staff)):
    form = await request.form()
    try:
        with get_conn() as conn:
            order = core_production.get_order(conn, order_id)
            if not order:
                return RedirectResponse(link(request, "/production"), status_code=303)
            if not _can_report(staff, order):
                raise ProductionError("Отчёт сдаёт мастер этого заказа.")
            used = {}
            for part in core_production.get_parts(conn, order_id):
                value = (form.get(f"used_{part['id']}") or "").strip()
                if value:
                    if not value.isdigit():
                        raise ProductionError(f"«{part['product_name']}»: количество должно быть числом.")
                    used[part["id"]] = int(value)
            extras = [(form.get(f"extra_title_{i}") or "", form.get(f"extra_amount_{i}") or "") for i in range(EXTRA_ROWS)]
            core_production.submit_report(
                conn, order_id, staff["id"], used=used, extras=extras,
                work_price=form.get("work_price"), note=form.get("note"),
            )
    except ProductionError as exc:
        return _error(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


@router.post("/{order_id}/accept")
def accept_view(request: Request, order_id: int, staff=Depends(require_staff)):
    try:
        with get_conn() as conn:
            if not _can_manage(staff):
                raise ProductionError("Принять результат может кладовщик, админ или владелец.")
            core_production.accept(conn, order_id, staff["id"])
    except (ProductionError, TransferError, core_inventory.InsufficientStockError) as exc:
        return _error(request, staff, order_id, str(exc))
    return RedirectResponse(link(request, f"/production/{order_id}"), status_code=303)


@router.post("/{order_id}/cancel")
def cancel_view(request: Request, order_id: int, staff=Depends(require_staff)):
    try:
        with get_conn() as conn:
            if not _can_manage(staff):
                raise ProductionError("Отменить заказ может кладовщик, админ или владелец.")
            returning = core_production.cancel(conn, order_id, staff["id"])
    except (ProductionError, TransferError, core_inventory.InsufficientStockError) as exc:
        return _error(request, staff, order_id, str(exc))
    # Cancelled after handover: stay on the order — it now points at the
    # перемещение that brings everything back, which the точка must receive.
    return RedirectResponse(link(request, f"/production/{order_id}" if returning else "/production"), status_code=303)
