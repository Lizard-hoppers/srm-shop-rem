from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import cash as core_cash
from core.storage import get_conn
from core.timefmt import kyiv_date_range_utc, kyiv_today
from webapp.deps import link, require_role
from webapp.templating import render

router = APIRouter(prefix="/cash")

_CASH_ROLES = ("owner", "admin", "storekeeper")


def _dashboard_context(conn, date_from: str, date_to: str) -> dict:
    utc_start, utc_end = kyiv_date_range_utc(date_from, date_to)
    return {
        "balance": core_cash.cash_balance(conn),
        "summary": core_cash.period_summary(conn, utc_start, utc_end),
        "transactions": core_cash.list_transactions(conn),
        "date_from": date_from,
        "date_to": date_to,
    }


@router.get("")
def dashboard_view(
    request: Request, date_from: str = "", date_to: str = "",
    staff=Depends(require_role(*_CASH_ROLES)),
):
    today = kyiv_today()
    date_from = date_from or today
    date_to = date_to or today
    with get_conn() as conn:
        ctx = _dashboard_context(conn, date_from, date_to)
    return render(request, "cash_dashboard.html", staff=staff, **ctx)


@router.post("/expense")
def expense_view(
    request: Request, method: str = Form("cash"), amount: str = Form(""),
    category: str = Form("other"), comment: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    if method not in core_cash.METHODS:
        method = "cash"
    if category not in core_cash.EXPENSE_CATEGORIES:
        category = "other"
    try:
        amount_int = int(amount)
    except ValueError:
        amount_int = 0

    with get_conn() as conn:
        if amount_int <= 0:
            ctx = _dashboard_context(conn, kyiv_today(), kyiv_today())
            return render(request, "cash_dashboard.html", staff=staff, error="Укажите сумму расхода.", **ctx)
        core_cash.record_expense(conn, method, amount_int, category, comment.strip() or None, staff["id"])
    return RedirectResponse(link(request, "/cash"), status_code=303)


@router.post("/adjustment")
def adjustment_view(
    request: Request, direction: str = Form("in"), amount: str = Form(""), comment: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    try:
        amount_int = int(amount)
    except ValueError:
        amount_int = 0

    with get_conn() as conn:
        if amount_int <= 0:
            ctx = _dashboard_context(conn, kyiv_today(), kyiv_today())
            return render(request, "cash_dashboard.html", staff=staff, error="Укажите сумму.", **ctx)
        signed = amount_int if direction == "in" else -amount_int
        core_cash.record_adjustment(conn, signed, comment.strip() or None, staff["id"])
    return RedirectResponse(link(request, "/cash"), status_code=303)
