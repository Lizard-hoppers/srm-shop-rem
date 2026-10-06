"""Перемещение товара между складами (core.stock_transfers) — «Отправил →
В пути → Принял». The list shows what is waiting for THIS person to
confirm and what they sent that is still on its way; a transfer's own page
is where it gets received (per-line quantities, the destination cell, a
note when something is missing) and where an owner settles a недостача.

Who may send from where and who may say «Принял» is decided in
core.warehouses (sendable_from / may_receive): a master — only his own
склад; stock staff — their точка's; owner/admin — any.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from core import documents as core_documents
from core import inventory as core_inventory
from core import stock_transfers as core_transfers
from core import warehouses as core_warehouses
from core.storage import get_conn
from core.store_access import stores_for_staff
from webapp.deps import idem_key, link, loc, optional_int, require_staff
from webapp.templating import render

router = APIRouter(prefix="/transfers")

INITIAL_ROWS = 1


def _location_ids(staff) -> set[int]:
    return {s.location_id for s in stores_for_staff(staff)}


def receivable_warehouse_ids(conn, staff) -> list[int]:
    location_ids = _location_ids(staff)
    return [
        w["id"] for w in core_warehouses.list_warehouses(conn, include_inactive_masters=True)
        if w["kind"] != "transit" and core_warehouses.may_receive(conn, staff, w, location_ids)
    ]


def pending_count(conn, staff) -> int:
    """How many transfers are waiting for this person's «Принял»."""
    return len(core_transfers.list_transfers(conn, status="sent", to_warehouse_ids=receivable_warehouse_ids(conn, staff)))


def _line_label(line) -> str:
    parts = [line["product_name"]]
    if line["imei"]:
        parts.append(f"IMEI {line['imei']}")
    parts.append(f"партия №{line['batch_id']}")
    if line["supplier_name"]:
        parts.append(line["supplier_name"])
    parts.append(f"{line['cell_code']} · {line['qty']} {line['unit']}")
    return " · ".join(parts)


def _view(transfer) -> dict:
    return {**dict(transfer), "route": core_transfers.route(transfer)}


def _list_context(conn, request: Request, staff, source_id: int | None) -> dict:
    sendable = core_warehouses.sendable_from(conn, staff, loc(request))
    own_point = core_warehouses.point_warehouse(conn, loc(request))
    source = next((w for w in sendable if w["id"] == source_id), None)
    if source is None:
        # Default to the склад of the точка the person is working in.
        source = next((w for w in sendable if own_point and w["id"] == own_point["id"]), sendable[0] if sendable else None)
    lines = core_inventory.stock_lines(conn, warehouse_id=source["id"]) if source else []
    sendable_ids = [w["id"] for w in sendable]
    return {
        "incoming": [_view(t) for t in core_transfers.list_transfers(
            conn, status="sent", to_warehouse_ids=receivable_warehouse_ids(conn, staff))],
        "outgoing": [_view(t) for t in core_transfers.list_transfers(conn, status="sent", from_warehouse_ids=sendable_ids)],
        "history": [_view(t) for t in core_transfers.list_transfers(conn, limit=30) if t["status"] != "sent"],
        "sendable": [{**dict(w), "display_name": core_warehouses.display_name(w)} for w in sendable],
        "source": source,
        "targets": [
            {**dict(w), "display_name": core_warehouses.display_name(w)}
            for w in core_warehouses.list_warehouses(conn)
            if w["kind"] != "transit" and (not source or w["id"] != source["id"])
        ],
        "lines_for_picker": [
            {"id": f"{line['batch_id']}:{line['cell_id']}", "label": _line_label(line), "qty": line["qty"],
             "serial": bool(line["is_serial"])}
            for line in lines
        ],
        "item_rows": range(INITIAL_ROWS),
    }


@router.get("")
def list_view(request: Request, source: str = "", staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _list_context(conn, request, staff, optional_int(source) if source.isdigit() else None)
    return render(request, "transfers_list.html", staff=staff, **ctx)


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    source_id = optional_int(form.get("from_warehouse_id") or "")
    target_id = optional_int(form.get("to_warehouse_id") or "")
    row_count = int(form.get("row_count") or INITIAL_ROWS)
    comment = (form.get("comment") or "").strip() or None

    lines, error = [], None
    for i in range(row_count):
        line_id = (form.get(f"line_id_{i}") or "").strip()
        typed = (form.get(f"line_{i}") or "").strip()
        qty = (form.get(f"qty_{i}") or "").strip()
        if not line_id and not typed and not qty:
            continue
        batch_part, _, cell_part = line_id.partition(":")
        if not batch_part.isdigit() or not cell_part.isdigit():
            error = f"Строка {i + 1}: выберите позицию из списка склада."
            break
        if not qty.isdigit() or int(qty) <= 0:
            error = f"Строка {i + 1}: укажите количество."
            break
        lines.append((int(batch_part), int(cell_part), int(qty)))

    try:
        with get_conn() as conn:
            key = idem_key("stock_transfer", form.get("idem"))
            already = core_documents.find_by_key(conn, key)
            if already:
                return RedirectResponse(link(request, f"/transfers/{already['ref_id']}"), status_code=303)
            if error:
                raise core_transfers.TransferError(error)
            allowed = [w["id"] for w in core_warehouses.sendable_from(conn, staff, loc(request))]
            if source_id not in allowed:
                raise core_transfers.TransferError("С этого склада вы отправлять не можете.")
            transfer_id = core_transfers.send(conn, source_id, target_id, lines, staff["id"], comment, key=key)
    except core_transfers.TransferError as exc:
        with get_conn() as conn:
            ctx = _list_context(conn, request, staff, source_id)
        return render(request, "transfers_list.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/transfers/{transfer_id}"), status_code=303)


def _detail_context(conn, request: Request, staff, transfer_id: int) -> dict | None:
    transfer = core_transfers.get_transfer(conn, transfer_id)
    if not transfer:
        return None
    target = core_warehouses.get_warehouse(conn, transfer["to_warehouse_id"])
    document = core_transfers.document(conn, transfer_id)
    return {
        "transfer": _view(transfer),
        "items": core_transfers.get_items(conn, transfer_id),
        "can_receive": transfer["status"] == "sent" and core_warehouses.may_receive(conn, staff, target, _location_ids(staff)),
        # A точка's склад has shelves to choose from; a master's has one.
        "target_cells": core_warehouses.cells(conn, target["id"]) if target["kind"] == "point" else [],
        "shortage": core_transfers.shortage(conn, transfer_id),
        "can_settle": staff["role"] in ("owner", "admin"),
        "document": document,
        "document_label": core_documents.doc_label(document) if document else None,
    }


@router.get("/{transfer_id}")
def detail_view(request: Request, transfer_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _detail_context(conn, request, staff, transfer_id)
    if not ctx:
        return RedirectResponse(link(request, "/transfers"), status_code=303)
    return render(request, "transfer_detail.html", staff=staff, **ctx)


@router.post("/{transfer_id}/receive")
async def receive_view(request: Request, transfer_id: int, staff=Depends(require_staff)):
    form = await request.form()
    try:
        with get_conn() as conn:
            transfer = core_transfers.get_transfer(conn, transfer_id)
            if not transfer:
                return RedirectResponse(link(request, "/transfers"), status_code=303)
            target = core_warehouses.get_warehouse(conn, transfer["to_warehouse_id"])
            if not core_warehouses.may_receive(conn, staff, target, _location_ids(staff)):
                raise core_transfers.TransferError("Принять может только тот, кто отвечает за склад-получатель.")
            received = {}
            for item in core_transfers.get_items(conn, transfer_id):
                value = (form.get(f"recv_{item['id']}") or "").strip()
                if value:
                    if not value.isdigit():
                        raise core_transfers.TransferError(f"«{item['product_name']}»: количество должно быть числом.")
                    received[item["id"]] = int(value)
            cell = (form.get("cell_id") or "").strip()
            core_transfers.receive(
                conn, transfer_id, staff["id"], received, int(cell) if cell.isdigit() else None, form.get("note"),
            )
    except core_transfers.TransferError as exc:
        with get_conn() as conn:
            ctx = _detail_context(conn, request, staff, transfer_id)
        return render(request, "transfer_detail.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/transfers/{transfer_id}"), status_code=303)


@router.post("/{transfer_id}/shortage")
async def shortage_view(request: Request, transfer_id: int, staff=Depends(require_staff)):
    form = await request.form()
    try:
        with get_conn() as conn:
            if staff["role"] not in ("owner", "admin"):
                raise core_transfers.TransferError("Списать недостачу может только владелец.")
            core_transfers.write_off_shortage(conn, transfer_id, staff["id"], form.get("reason") or "")
    except core_transfers.TransferError as exc:
        with get_conn() as conn:
            ctx = _detail_context(conn, request, staff, transfer_id)
        if not ctx:
            return RedirectResponse(link(request, "/transfers"), status_code=303)
        return render(request, "transfer_detail.html", staff=staff, error=str(exc), **ctx)
    return RedirectResponse(link(request, f"/transfers/{transfer_id}"), status_code=303)
