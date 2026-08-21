"""Личные настройки сотрудника — пока только выбор языка интерфейса
(19.08). Живёт отдельно от core.auth's staff CRUD (это self-service —
любой залогиненный сотрудник меняет только свой собственный язык, не
чужие данные), поэтому просто require_staff, без require_role."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core.i18n import LANGUAGES
from core.storage import get_conn
from webapp.deps import link, require_staff
from webapp.templating import render

router = APIRouter(prefix="/settings")


@router.get("")
def settings_view(request: Request, staff=Depends(require_staff)):
    return render(request, "settings.html", staff=staff, languages=LANGUAGES)


@router.post("/language")
def set_language_view(request: Request, language: str = Form(...), staff=Depends(require_staff)):
    if language in LANGUAGES:
        with get_conn() as conn:
            core_auth.set_staff_language(conn, staff["id"], language)
    return RedirectResponse(link(request, "/settings"), status_code=303)
