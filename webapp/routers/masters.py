from __future__ import annotations

from core.timefmt import kyiv_date_range_utc, kyiv_today
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import accounts as core_accounts
from core import auth as core_auth
from core import cash as core_cash
from core import documents as core_documents
from core import locations as core_locations
from core import masters as core_masters
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, optional_int, require_role
from webapp.payments import payments_from_form
from webapp.templating import render

router = APIRouter(prefix="/masters")

_MASTERS_ROLES = ("owner", "admin")


def _clean_pay(pay_type: str, pay_value: str) -> tuple[str | None, float | None]:
    """A rate only means something paired with a value — an empty/unknown
    pay_type or a blank value leaves the master with no rate configured
    (payout computes to 0) rather than half-saving one side of it. «1/3» /
    «треть» is exactly a third (core.masters.parse_rate)."""
    if pay_type not in core_auth.PAY_TYPES:
        return None, None
    value = core_masters.parse_rate(pay_value)
    if value is None:
        return None, None
    return pay_type, (int(value) if value == int(value) else value)


def _list_context(conn) -> dict:
    """Masters are one list for the whole business — a master's stats
    follow the master, whichever точка's repairs they worked on. `locations`
    feeds the «Точка» picker (which точка they log into / belong to)."""
    masters = core_auth.list_masters(conn, include_inactive=True)
    return {
        "masters": masters,
        "summaries": {m["id"]: core_masters.master_summary(conn, m) for m in masters},
        "locations": core_locations.list_locations(conn),
        "master_kinds": core_auth.MASTER_KINDS,
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
    master_kind: str = Form("staff"), skills: str = Form(""),
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
            location_id=optional_int(location_id), master_kind=master_kind, skills=skills,
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
        money_ctx = _money_context(conn, request, master_id)
    return render(
        request, "master_detail.html", staff=staff, master=master, summary=summary, locations=locations,
        master_kinds=core_auth.MASTER_KINDS, **money_ctx,
    )


def _report_context(conn, request: Request, master_id: int) -> dict:
    """«Сводка за период»: each repair he handed over, as in Гибрид's old
    bot. This month by default."""
    today = kyiv_today()
    date_from = request.query_params.get("date_from") or today[:8] + "01"
    date_to = request.query_params.get("date_to") or today
    try:
        start, end = kyiv_date_range_utc(date_from, date_to)
    except ValueError:
        date_from, date_to = today[:8] + "01", today
        start, end = kyiv_date_range_utc(date_from, date_to)
    lines = core_masters.repair_lines(conn, master_id, start, end)
    return {"report_lines": lines, "report_total": core_masters.lines_total(lines),
            "report_from": date_from, "report_to": date_to}


def _money_context(conn, request: Request, master_id: int) -> dict:
    """«Начислено − выплачено = мы должны» and the two lists behind it —
    plus the period's «Сводка»."""
    return _report_context(conn, request, master_id) | {
        "accrued": core_masters.accrued_total(conn, master_id),
        "paid": core_masters.paid_total(conn, master_id),
        "owed": core_masters.owed(conn, master_id),
        "accruals": core_masters.list_accruals(conn, master_id, limit=30),
        "payouts": core_masters.list_payouts(conn, master_id, limit=30),
        "accounts": core_accounts.list_accounts(conn, loc(request)),
    }


@router.post("/{master_id}/payout")
async def payout_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    """«Выплата мастеру» — РКО from the accounts of the current точка."""
    form = await request.form()
    key = idem_key("master_payout", form.get("idem"))
    try:
        with get_conn() as conn:
            if not core_documents.find_by_key(conn, key):
                core_masters.pay_out(
                    conn, master_id, payments_from_form(form), paid_by=staff["id"], location_id=loc(request),
                    comment=form.get("comment"), key=key,
                )
    except (core_masters.PayoutError, core_cash.PaymentError) as exc:
        with get_conn() as conn:
            master = core_auth.get_master(conn, master_id)
            if not master:
                return RedirectResponse(link(request, "/masters"), status_code=303)
            return render(
                request, "master_detail.html", staff=staff, master=master,
                summary=core_masters.master_summary(conn, master), locations=core_locations.list_locations(conn),
                master_kinds=core_auth.MASTER_KINDS, error=str(exc), **_money_context(conn, request, master_id),
            )
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


async def _back_with_error(request: Request, staff, master_id: int, error: str):
    with get_conn() as conn:
        master = core_auth.get_master(conn, master_id)
        if not master:
            return RedirectResponse(link(request, "/masters"), status_code=303)
        return render(
            request, "master_detail.html", staff=staff, master=master,
            summary=core_masters.master_summary(conn, master), locations=core_locations.list_locations(conn),
            master_kinds=core_auth.MASTER_KINDS, error=error, **_money_context(conn, request, master_id),
        )


@router.post("/{master_id}/payout-nocash")
async def payout_nocash_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    """«Выплата без кассы» — for a точка whose касса isn't kept here yet."""
    form = await request.form()
    try:
        with get_conn() as conn:
            core_masters.pay_out_without_cash(
                conn, master_id, core_accounts.parse_amount(form.get("amount")) or 0, paid_by=staff["id"],
                location_id=loc(request), comment=form.get("comment"),
            )
    except core_masters.PayoutError as exc:
        return await _back_with_error(request, staff, master_id, str(exc))
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


@router.post("/{master_id}/opening")
async def opening_view(request: Request, master_id: int, staff=Depends(require_role(*_MASTERS_ROLES))):
    """«Стартовый остаток»: + мы должны, − переплатили."""
    form = await request.form()
    raw = str(form.get("amount") or "").strip().replace(" ", "").replace(",", ".")
    try:
        amount = float(raw)
    except ValueError:
        return await _back_with_error(request, staff, master_id, "Введите сумму числом: плюс — мы должны, минус — переплатили.")
    with get_conn() as conn:
        core_masters.add_opening_balance(conn, master_id, amount, by=staff["id"], comment=form.get("comment"))
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


@router.post("/{master_id}/edit")
def edit_view(
    request: Request, master_id: int, name: str = Form(""), telegram_id: str = Form(""),
    pay_type: str = Form(""), pay_value: str = Form(""), location_id: str = Form(""),
    master_kind: str = Form("staff"), skills: str = Form(""),
    topic_id: str = Form(""), personal_chat_id: str = Form(""),
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
                locations=core_locations.list_locations(conn), master_kinds=core_auth.MASTER_KINDS,
                error="Введите имя мастера.", **_money_context(conn, request, master_id),
            )
        clean_type, clean_value = _clean_pay(pay_type, pay_value)
        core_auth.update_master(
            conn, master_id, name.strip(), optional_int(telegram_id), clean_type, clean_value,
            location_id=optional_int(location_id), master_kind=master_kind, skills=skills,
        )
        core_auth.set_master_chats(conn, master_id, _chat(topic_id), _chat(personal_chat_id))
    return RedirectResponse(link(request, f"/masters/{master_id}"), status_code=303)


def _chat(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


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
