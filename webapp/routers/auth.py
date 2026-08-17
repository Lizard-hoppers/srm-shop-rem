from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core.storage import get_conn
from webapp.templating import render

router = APIRouter()


@router.get("/login")
def login_form(request: Request):
    if request.session.get("staff_id"):
        return RedirectResponse("/", status_code=302)
    return render(request, "login.html", staff=None)


@router.post("/login")
def login_submit(
    request: Request, login: str = Form(...), password: str = Form(...), source: str = Form("web")
):
    template = "miniapp.html" if source == "miniapp" else "login.html"
    with get_conn() as conn:
        staff = core_auth.get_staff_by_login(conn, login.strip())
    if not staff or not core_auth.verify_password(password, staff["password_hash"]):
        return render(request, template, staff=None, error="Неверный логин или пароль")
    request.session["staff_id"] = staff["id"]
    return RedirectResponse("/", status_code=302)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)
