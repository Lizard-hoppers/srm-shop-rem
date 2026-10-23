"""Журнал Telegram (core.tg_journal): кто пишет боту этой точки — и
«Сделать сотрудником» прямо из списка. Только владелец/админ."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import bots as core_bots
from core import tg_journal
from core.storage import get_conn
from webapp.deps import link, loc, require_role
from webapp.templating import render

router = APIRouter(prefix="/tg-users")


def _bot_ids(location_id: int) -> list[int]:
    bot = core_bots.bot_id(core_bots.token_for_location(location_id))
    return [bot] if bot else []


def _page(request: Request, staff, error: str | None = None, success: str | None = None):
    with get_conn() as conn:
        rows = tg_journal.list_for_bot(conn, _bot_ids(loc(request)))
    return render(request, "tg_users.html", staff=staff, rows=rows, roles=tg_journal.ROLES, error=error, success=success)


@router.get("")
def list_view(request: Request, staff=Depends(require_role("owner", "admin"))):
    return _page(request, staff)


@router.post("/make-staff")
def make_staff_view(
    request: Request, user_id: int = Form(...), name: str = Form(""), role: str = Form("manager"),
    staff=Depends(require_role("owner", "admin")),
):
    try:
        with get_conn() as conn:
            tg_journal.make_staff(conn, user_id, name, role, loc(request))
    except ValueError as exc:
        return _page(request, staff, error=str(exc).capitalize())
    return RedirectResponse(link(request, "/tg-users"), status_code=303)
