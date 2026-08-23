"""Store switcher + Кабинет магазина (Фаза B, 23.08). Two small self-
service-shaped pairs of routes sharing a file since both are "about the
current store" rather than a specific business entity — same GET-renders-
a-form / POST-validates-and-redirects shape as webapp/routers/settings.py
(personal language preference)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import store_settings as core_store_settings
from core.session_token import make_token
from core.storage import get_conn
from core.store_access import accessible_stores
from core.store_prefs import set_last_store
from webapp.deps import require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/store")


@router.get("/settings")
def store_settings_view(request: Request, staff=Depends(require_role("owner", "admin"))):
    with get_conn() as conn:
        settings = core_store_settings.get_settings(conn)
    return render(request, "store_settings.html", staff=staff, settings=settings)


@router.post("/settings")
def store_settings_save(
    request: Request,
    name: str = Form(...),
    address: str = Form(""),
    phone: str = Form(""),
    working_hours: str = Form(""),
    staff=Depends(require_role("owner", "admin")),
):
    if not name.strip():
        with get_conn() as conn:
            settings = core_store_settings.get_settings(conn)
        return render(
            request, "store_settings.html", staff=staff, settings=settings,
            error="Название магазина не может быть пустым.",
        )
    with get_conn() as conn:
        core_store_settings.update_settings(conn, name, address, phone, working_hours)
        settings = core_store_settings.get_settings(conn)
    return render(request, "store_settings.html", staff=staff, settings=settings, success="Изменения сохранены.")


@router.get("/switch")
def store_switch_view(request: Request, staff=Depends(require_staff)):
    accessible = accessible_stores(staff["telegram_id"]) if staff["telegram_id"] else []
    return render(
        request, "store_switch.html", staff=staff, accessible=accessible,
        current_store_id=request.state.store.id,
    )


@router.post("/switch")
def store_switch_do(request: Request, store_id: str = Form(...), staff=Depends(require_staff)):
    accessible = accessible_stores(staff["telegram_id"]) if staff["telegram_id"] else []
    match = next(((s, st) for s, st in accessible if s.id == store_id), None)
    if not match:
        return render(
            request, "store_switch.html", staff=staff, accessible=accessible,
            current_store_id=request.state.store.id,
            error="У вас нет доступа к этому магазину.",
        )
    target_store, target_staff = match
    set_last_store(staff["telegram_id"], target_store.id)
    token = make_token(target_staff["id"], target_store.id)
    return RedirectResponse(f"/?t={token}", status_code=303)
