"""Касса (Заход 2, 06.10): the точка's денежные счета and everything that
moves money between or out of them by hand — расход, корректировка, обмен
валют, перемещение, plus the shift and the accounts list themselves.

Money that comes in with a sale or a repair is recorded by those documents
(core.sales, webapp.routers.repairs), not here.

Roles: the page and day-to-day operations — owner/admin/storekeeper, as
before. Correcting a balance («внести/изъять») and arranging the accounts —
owner/admin only: «видят все, корректируют только владельцы».
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import accounts as core_accounts
from core import cash as core_cash
from core import documents as core_documents
from core import money_ops
from core import shifts as core_shifts
from core.accounts import AccountError
from core.storage import get_conn
from core.store_access import stores_for_staff
from core.timefmt import kyiv_date_range_utc, kyiv_today
from webapp.deps import idem_key, link, loc, optional_int, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/cash")

_CASH_ROLES = ("owner", "admin", "storekeeper")
_OWNER_ROLES = ("owner", "admin")


def _dashboard_context(conn, date_from: str, date_to: str, location_id: int, staff) -> dict:
    utc_start, utc_end = kyiv_date_range_utc(date_from, date_to)
    accounts = core_accounts.balances(conn, location_id)
    active = [a for a in accounts if a["active"]]
    return {
        "balance": core_cash.cash_balance(conn, location_id),
        "accounts": accounts,
        "active_accounts": active,
        # Where a перемещение can go: any active account of the business
        # (this точка's own included) — the page filters by currency.
        "all_accounts": core_accounts.list_accounts(conn),
        "incoming": money_ops.list_in_transit(conn, location_id, "incoming"),
        "outgoing": money_ops.list_in_transit(conn, location_id, "outgoing"),
        "shift": core_shifts.current_shift(conn, staff["id"], location_id),
        "summary": core_cash.period_summary(conn, utc_start, utc_end, location_id),
        "transactions": core_cash.list_transactions(conn, location_id=location_id),
        "date_from": date_from,
        "date_to": date_to,
        "can_adjust": staff["role"] in _OWNER_ROLES,
        "location_id": location_id,
    }


def _page(request: Request, staff, error: str | None = None):
    today = kyiv_today()
    with get_conn() as conn:
        ctx = _dashboard_context(conn, today, today, loc(request), staff)
    return render(request, "cash_dashboard.html", staff=staff, error=error, **ctx)


def _own_account(conn, request: Request, account_id: int | None):
    """An active account of the точка this request works in, or None —
    a form may only take money FROM where its author actually is."""
    account = core_accounts.get_account(conn, account_id)
    if not account or not account["active"] or account["location_id"] != loc(request):
        return None
    return account


@router.get("")
def dashboard_view(
    request: Request, date_from: str = "", date_to: str = "",
    staff=Depends(require_role(*_CASH_ROLES)),
):
    today = kyiv_today()
    date_from = date_from or today
    date_to = date_to or today
    try:
        kyiv_date_range_utc(date_from, date_to)
    except ValueError:
        date_from = date_to = today
    with get_conn() as conn:
        ctx = _dashboard_context(conn, date_from, date_to, loc(request), staff)
    return render(request, "cash_dashboard.html", staff=staff, **ctx)


@router.post("/expense")
def expense_view(
    request: Request, method: str = Form("cash"), amount: str = Form(""),
    category: str = Form("other"), comment: str = Form(""), idem: str = Form(""),
    account_id: str = Form(""), rate: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    if category not in core_cash.EXPENSE_CATEGORIES:
        category = "other"
    amount_value = core_accounts.parse_amount(amount)
    if not amount_value:
        return _page(request, staff, "Укажите сумму расхода.")

    with get_conn() as conn:
        account = None
        if account_id.strip():
            account = _own_account(conn, request, optional_int(account_id) if account_id.strip().isdigit() else None)
            if not account:
                return _page(request, staff, "Выберите счёт этой точки.")
        key = idem_key("cash_out", idem)
        if not core_documents.find_by_key(conn, key):
            core_cash.record_expense(
                conn, method, amount_value, category, comment.strip() or None, staff["id"],
                location_id=loc(request), key=key, account_id=account["id"] if account else None,
                rate=core_accounts.parse_amount(rate),
            )
    return RedirectResponse(link(request, "/cash"), status_code=303)


@router.post("/adjustment")
def adjustment_view(
    request: Request, direction: str = Form("in"), amount: str = Form(""), comment: str = Form(""),
    idem: str = Form(""), account_id: str = Form(""),
    staff=Depends(require_role(*_OWNER_ROLES)),
):
    amount_value = core_accounts.parse_amount(amount)
    if not amount_value:
        return _page(request, staff, "Укажите сумму.")

    with get_conn() as conn:
        account = None
        if account_id.strip():
            account = _own_account(conn, request, optional_int(account_id) if account_id.strip().isdigit() else None)
            if not account:
                return _page(request, staff, "Выберите счёт этой точки.")
        signed = amount_value if direction == "in" else -amount_value
        key = idem_key("cash_adjust", idem)
        if not core_documents.find_by_key(conn, key):
            core_cash.record_adjustment(
                conn, signed, comment.strip() or None, staff["id"], location_id=loc(request), key=key,
                account_id=account["id"] if account else None,
            )
    return RedirectResponse(link(request, "/cash"), status_code=303)


@router.post("/exchange")
def exchange_view(
    request: Request, from_account_id: str = Form(""), to_account_id: str = Form(""),
    amount_from: str = Form(""), amount_to: str = Form(""), comment: str = Form(""), idem: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    try:
        with get_conn() as conn:
            key = idem_key("exchange", idem)
            if not core_documents.find_by_key(conn, key):
                source = _own_account(conn, request, optional_int(from_account_id) if from_account_id.isdigit() else None)
                if not source:
                    raise AccountError("Выберите счёт этой точки, с которого отдаёте.")
                money_ops.exchange(
                    conn, source["id"], optional_int(to_account_id) if to_account_id.isdigit() else None,
                    amount_from, amount_to, staff["id"], comment.strip() or None, key=key,
                )
    except AccountError as exc:
        return _page(request, staff, str(exc))
    return RedirectResponse(link(request, "/cash"), status_code=303)


@router.post("/transfer")
def transfer_view(
    request: Request, from_account_id: str = Form(""), to_account_id: str = Form(""),
    amount: str = Form(""), comment: str = Form(""), idem: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    try:
        with get_conn() as conn:
            key = idem_key("money_transfer", idem)
            if not core_documents.find_by_key(conn, key):
                source = _own_account(conn, request, optional_int(from_account_id) if from_account_id.isdigit() else None)
                if not source:
                    raise AccountError("Выберите счёт этой точки, с которого отправляете.")
                money_ops.send_transfer(
                    conn, source["id"], optional_int(to_account_id) if to_account_id.isdigit() else None,
                    amount, staff["id"], comment.strip() or None, key=key,
                )
    except AccountError as exc:
        return _page(request, staff, str(exc))
    return RedirectResponse(link(request, "/cash"), status_code=303)


@router.post("/transfer/{transfer_id}/receive")
def transfer_receive_view(
    request: Request, transfer_id: int, received_amount: str = Form(""), note: str = Form(""),
    staff=Depends(require_role(*_CASH_ROLES)),
):
    """«Принял» — only by someone who works at the точка the money was
    sent TO (an owner/admin works at every one)."""
    try:
        with get_conn() as conn:
            transfer = money_ops.get_transfer(conn, transfer_id)
            allowed = {s.location_id for s in stores_for_staff(staff)}
            if not transfer or transfer["to_location_id"] not in allowed:
                raise AccountError("Это перемещение адресовано другой точке.")
            money_ops.receive_transfer(conn, transfer_id, staff["id"], received_amount, note)
    except AccountError as exc:
        return _page(request, staff, str(exc))
    return RedirectResponse(link(request, "/cash"), status_code=303)


# ---- смена ----

@router.get("/shift")
def shift_view(request: Request, staff=Depends(require_staff)):
    """«Сверить остатки» — any employee opens their own shift."""
    with get_conn() as conn:
        ctx = {
            "shift": core_shifts.current_shift(conn, staff["id"], loc(request)),
            "snapshot": core_shifts.snapshot(conn, loc(request)),
        }
    return render(request, "cash_shift.html", staff=staff, **ctx)


@router.post("/shift/open")
def shift_open_view(
    request: Request, mismatch: str = Form(""), note: str = Form(""), staff=Depends(require_staff),
):
    note = note.strip()
    if mismatch and not note:
        with get_conn() as conn:
            ctx = {
                "shift": core_shifts.current_shift(conn, staff["id"], loc(request)),
                "snapshot": core_shifts.snapshot(conn, loc(request)),
            }
        return render(
            request, "cash_shift.html", staff=staff, error="Напишите, что именно не сходится.", **ctx,
        )
    with get_conn() as conn:
        core_shifts.open_shift(conn, staff["id"], loc(request), note if mismatch else None)
    return RedirectResponse(link(request, "/cash/shift"), status_code=303)


@router.post("/shift/close")
def shift_close_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        shift = core_shifts.current_shift(conn, staff["id"], loc(request))
        if shift:
            core_shifts.close_shift(conn, shift["id"], staff["id"])
    return RedirectResponse(link(request, "/cash/shift"), status_code=303)


# ---- счета точки ----

def _accounts_page(request: Request, staff, error: str | None = None):
    with get_conn() as conn:
        accounts = core_accounts.balances(conn, loc(request), include_inactive=True)
    return render(
        request, "cash_accounts.html", staff=staff, error=error, accounts=accounts,
        kinds=core_accounts.KINDS, currencies=core_accounts.CURRENCIES,
    )


@router.get("/accounts")
def accounts_view(request: Request, staff=Depends(require_role(*_OWNER_ROLES))):
    return _accounts_page(request, staff)


@router.post("/accounts")
def accounts_create(
    request: Request, name: str = Form(""), kind: str = Form(""), currency: str = Form(""),
    staff=Depends(require_role(*_OWNER_ROLES)),
):
    try:
        with get_conn() as conn:
            core_accounts.create_account(conn, loc(request), kind, currency, name)
    except AccountError as exc:
        return _accounts_page(request, staff, str(exc))
    return RedirectResponse(link(request, "/cash/accounts"), status_code=303)


@router.post("/accounts/{account_id}")
def accounts_update(
    request: Request, account_id: int, name: str = Form(""), action: str = Form("rename"),
    staff=Depends(require_role(*_OWNER_ROLES)),
):
    try:
        with get_conn() as conn:
            account = core_accounts.get_account(conn, account_id)
            if not account or account["location_id"] != loc(request):
                raise AccountError("Счёт не найден у этой точки.")
            if action == "rename":
                core_accounts.rename_account(conn, account_id, name)
            elif action in ("off", "on"):
                core_accounts.set_active(conn, account_id, action == "on")
    except AccountError as exc:
        return _accounts_page(request, staff, str(exc))
    return RedirectResponse(link(request, "/cash/accounts"), status_code=303)
