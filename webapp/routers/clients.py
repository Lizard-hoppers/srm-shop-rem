from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from core import accounts as core_accounts
from core import cash as core_cash
from core import clients as core_clients
from core import documents as core_documents
from core import orders as core_orders
from core import qr as core_qr
from core import repairs as core_repairs
from core import sales as core_sales
from core import settlements as core_settlements
from core.storage import get_conn
from core.timefmt import kyiv_today
from webapp.deps import idem_key, link, loc, require_staff
from webapp.payments import payments_from_form
from webapp.templating import render

router = APIRouter(prefix="/clients")

# Имя обязательно, телефон — нет. Но «введён и не разобран» ≠ «не введён»:
# без этой развилки опечатка в номере молча сохранила бы клиента вообще без
# телефона (normalize_phone отдаёт "" в обоих случаях, см. её docstring).
_BAD_PHONE = "Проверьте номер телефона — например 0501234567."
# One phone number is one контрагент (core.clients) — a second card with
# the same number would split that person's history in two.
_PHONE_TAKEN = "Клиент с таким номером уже есть: {name}. Откройте его карточку вместо создания новой."


def _checked_phone(raw: str) -> tuple[str | None, str | None]:
    """(phone_to_store, error) — телефон в каноничном виде либо None, и
    сообщение об ошибке, если человек что-то ввёл, а это не номер."""
    normalized = core_clients.normalize_phone(raw)
    if not normalized and core_clients.phone_looks_entered(raw):
        return None, _BAD_PHONE
    return normalized or None, None


@router.get("")
def list_view(request: Request, q: str | None = None, source: str | None = None, staff=Depends(require_staff)):
    with get_conn() as conn:
        rows = core_clients.list_clients(conn, search=q, source=source)
    return render(request, "clients_list.html", staff=staff, clients=rows, query=q, source=source)


@router.post("")
def create_view(
    request: Request,
    name: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    stored_phone, phone_error = _checked_phone(phone)
    error = "Введите имя клиента." if not name.strip() else phone_error
    with get_conn() as conn:
        existing = core_clients.get_by_phone(conn, stored_phone) if stored_phone and not error else None
        if existing:
            error = _PHONE_TAKEN.format(name=existing["name"])
        if error:
            rows = core_clients.list_clients(conn)
            return render(request, "clients_list.html", staff=staff, clients=rows, query=None, source=None, error=error)

    with get_conn() as conn:
        client_id = core_clients.create_client(
            conn, name=name.strip(), phone=stored_phone, notes=notes.strip() or None
        )
    return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)


@router.get("/find")
def find_view(request: Request, code: str = "", staff=Depends(require_staff)):
    client_id = core_qr.parse_client_code(code)
    with get_conn() as conn:
        client = core_clients.get_client(conn, client_id) if client_id else None
        if client:
            return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)
        rows = core_clients.list_clients(conn)
    return render(request, "clients_list.html", staff=staff, clients=rows, query=None, source=None, error="QR-код не распознан — клиент не найден.")


# Handing money OUT to a контрагент is the owner's/admin's call (the same
# line as correcting a касса balance); taking money in — anyone on shift.
_PAYOUT_ROLES = ("owner", "admin")
_PROFIT_ROLES = ("owner", "admin")


def _detail_context(conn, request: Request, staff, client_id: int) -> dict | None:
    """Карточка контрагента: who he is, what we owe each other, every
    document with him (with its profit, for those who may see profit),
    and the actions of the макет."""
    client = core_clients.get_client(conn, client_id)
    if not client:
        return None
    core_orders.expire_due(conn)
    documents = [
        {**dict(d), "label": core_documents.doc_label(d), "source_path": core_documents.source_path(d)}
        for d in core_documents.list_journal(conn, client_id=client_id, limit=200)
    ]
    live = [d for d in documents if d["status"] != "cancelled"]
    return {
        "client": client,
        "repair_history": core_repairs.list_repairs_by_client(conn, client_id),
        "sales_history": core_sales.list_sales_by_client(conn, client_id),
        "position": core_settlements.client_position(conn, client_id),
        "documents": documents,
        "profit_total": sum(d["profit"] or 0 for d in live),
        "show_profit": staff["role"] in _PROFIT_ROLES,
        "can_pay_out": staff["role"] in _PAYOUT_ROLES,
        "accounts": core_accounts.list_accounts(conn, loc(request)),
        "open_orders": [
            {**dict(o), "label": core_documents.label("client_order", o["id"])}
            for o in core_orders.list_orders(conn, statuses=("reserved", "expired"), client_id=client_id)
        ],
    }


def _detail(request: Request, staff, client_id: int, error: str | None = None):
    with get_conn() as conn:
        ctx = _detail_context(conn, request, staff, client_id)
    if not ctx:
        return RedirectResponse(link(request, "/clients"), status_code=303)
    return render(request, "client_detail.html", staff=staff, error=error, **ctx)


@router.get("/{client_id}")
def detail_view(request: Request, client_id: int, staff=Depends(require_staff)):
    return _detail(request, staff, client_id)


async def _money(request: Request, client_id: int, staff, direction: str):
    form = await request.form()
    key = idem_key(f"client_money_{direction}", form.get("idem"))
    try:
        with get_conn() as conn:
            if direction == "out" and staff["role"] not in _PAYOUT_ROLES:
                raise core_settlements.SettlementError("Выдать деньги контрагенту может владелец или админ.")
            if not core_documents.find_by_key(conn, key):
                move = core_settlements.receive_money if direction == "in" else core_settlements.pay_out_money
                move(
                    conn, client_id, payments_from_form(form), staff_id=staff["id"], location_id=loc(request),
                    comment=form.get("comment"), key=key,
                )
    except (core_settlements.SettlementError, core_cash.PaymentError) as exc:
        return _detail(request, staff, client_id, str(exc))
    return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)


@router.post("/{client_id}/money-in")
async def money_in_view(request: Request, client_id: int, staff=Depends(require_staff)):
    """«Принять деньги» — ПКО."""
    return await _money(request, client_id, staff, "in")


@router.post("/{client_id}/money-out")
async def money_out_view(request: Request, client_id: int, staff=Depends(require_staff)):
    """«Выдать деньги» — РКО."""
    return await _money(request, client_id, staff, "out")


@router.get("/{client_id}/statement")
def statement_view(
    request: Request, client_id: int, date_from: str = "", date_to: str = "", staff=Depends(require_staff),
):
    """«Сверка за период» — by default the current month."""
    today = kyiv_today()
    date_from, date_to = date_from or today[:8] + "01", date_to or today
    with get_conn() as conn:
        client = core_clients.get_client(conn, client_id)
        if not client:
            return RedirectResponse(link(request, "/clients"), status_code=303)
        try:
            statement = core_settlements.statement(conn, client_id, date_from, date_to)
        except ValueError:
            date_from, date_to = today[:8] + "01", today
            statement = core_settlements.statement(conn, client_id, date_from, date_to)
    return render(
        request, "client_statement.html", staff=staff, client=client, statement=statement,
        date_from=date_from, date_to=date_to,
    )


@router.get("/{client_id}/qr.png")
def qr_view(request: Request, client_id: int, staff=Depends(require_staff)):
    png = core_qr.generate_png(core_qr.client_code(client_id))
    return Response(content=png, media_type="image/png")


@router.post("/{client_id}/edit")
def edit_view(
    request: Request,
    client_id: int,
    name: str = Form(""),
    phone: str = Form(""),
    notes: str = Form(""),
    staff=Depends(require_staff),
):
    stored_phone, phone_error = _checked_phone(phone)
    error = "Введите имя клиента." if not name.strip() else phone_error
    with get_conn() as conn:
        existing = core_clients.get_by_phone(conn, stored_phone) if stored_phone and not error else None
        if existing and existing["id"] != client_id:
            error = _PHONE_TAKEN.format(name=existing["name"])
        if not error:
            core_clients.update_client(
                conn, client_id, name=name.strip(), phone=stored_phone, notes=notes.strip() or None
            )
    if error:
        return _detail(request, staff, client_id, error)
    return RedirectResponse(link(request, f"/clients/{client_id}"), status_code=303)
