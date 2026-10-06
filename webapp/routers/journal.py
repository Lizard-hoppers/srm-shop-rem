"""Единый журнал документов (core.documents) — every проведённая operation
of the whole business in one chronological feed, with the filters from the
презентация: период, точка, сотрудник, тип документа. Owner/admin only: it
shows every точка's money at once, the same boundary as Отчёты.

A document's own page shows its trail (document_events) and is where a
mistake gets cancelled — with a reason, never deleted (core.doc_cancel)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core import channel_posts
from core import doc_cancel
from core import documents as core_documents
from core import locations as core_locations
from core.storage import get_conn
from core.timefmt import kyiv_date_range_utc, kyiv_today
from webapp.deps import link, optional_int, require_role
from webapp.templating import render

router = APIRouter(prefix="/journal")

_JOURNAL_ROLES = ("owner", "admin")


def _view(doc) -> dict:
    """A journal row + everything the templates would otherwise have to
    compute themselves."""
    return {
        **dict(doc),
        "label": core_documents.doc_label(doc),
        "type_name": core_documents.type_name(doc["doc_type"]),
        "source_path": core_documents.source_path(doc),
        "cancellable": doc["status"] == "posted" and doc["doc_type"] in doc_cancel.CANCELLABLE_TYPES,
    }


@router.get("")
def journal_view(
    request: Request, date_from: str = "", date_to: str = "", location: str = "", employee: str = "",
    doc_type: str = "", staff=Depends(require_role(*_JOURNAL_ROLES)),
):
    today = kyiv_today()
    date_from = date_from or today
    date_to = date_to or today
    try:
        utc_start, utc_end = kyiv_date_range_utc(date_from, date_to)
    except ValueError:
        date_from = date_to = today
        utc_start, utc_end = kyiv_date_range_utc(today, today)
    if doc_type not in core_documents.DOC_TYPES:
        doc_type = ""

    with get_conn() as conn:
        rows = core_documents.list_journal(
            conn, utc_start=utc_start, utc_end=utc_end, location_id=optional_int(location) if location.isdigit() else None,
            staff_id=optional_int(employee) if employee.isdigit() else None, doc_type=doc_type or None,
        )
        ctx = {
            "documents": [_view(r) for r in rows],
            "locations": core_locations.list_locations(conn),
            "employees": core_auth.list_staff(conn),
            "doc_types": {key: name for key, (_prefix, name) in core_documents.DOC_TYPES.items()},
            "date_from": date_from, "date_to": date_to,
            "f_location": location, "f_employee": employee, "f_doc_type": doc_type,
        }
    return render(request, "journal.html", staff=staff, **ctx)


def _detail_context(conn, doc_id: int) -> dict | None:
    doc = core_documents.get(conn, doc_id)
    if not doc:
        return None
    return {"doc": _view(doc), "events": core_documents.get_events(conn, doc_id)}


@router.get("/{doc_id}")
def document_view(request: Request, doc_id: int, staff=Depends(require_role(*_JOURNAL_ROLES))):
    with get_conn() as conn:
        ctx = _detail_context(conn, doc_id)
    if not ctx:
        return RedirectResponse(link(request, "/journal"), status_code=303)
    return render(request, "journal_document.html", staff=staff, **ctx)


@router.post("/{doc_id}/cancel")
async def document_cancel(
    request: Request, doc_id: int, reason: str = Form(""), staff=Depends(require_role(*_JOURNAL_ROLES)),
):
    try:
        with get_conn() as conn:
            doc = doc_cancel.cancel_document(conn, doc_id, staff["id"], reason)
            product_ids = []
            if doc["doc_type"] == "sale":
                product_ids = [
                    row["product_id"] for row in conn.execute(
                        "SELECT product_id FROM sales_order_items WHERE order_id = ?", (doc["ref_id"],)
                    ).fetchall()
                ]
    except core_documents.DocumentError as exc:
        with get_conn() as conn:
            ctx = _detail_context(conn, doc_id)
        if not ctx:
            return RedirectResponse(link(request, "/journal"), status_code=303)
        return render(request, "journal_document.html", staff=staff, error=str(exc), **ctx)

    if product_ids and doc["location_id"]:
        # The stock is back — a live sales-channel card (if one survived)
        # gets refreshed. Blocking httpx to Telegram, so off the event loop.
        await run_in_threadpool(channel_posts.sync_products, product_ids, str(doc["location_id"]), None)
    return RedirectResponse(link(request, f"/journal/{doc_id}"), status_code=303)
