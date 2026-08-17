"""Telegram Mini App entry point. No password, ever: a staff member is
recognized purely by their Telegram id (linked ahead of time by an admin via
`python -m core.link_telegram <login> <telegram_id>`).

Auth here goes through a real browser form POST + server redirect that
carries a signed token in the URL (?t=...), not a cookie: Telegram's
in-app WebView was observed to drop Set-Cookie between separate requests
even across a same-origin 303 redirect, so nothing here depends on cookies.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from core import auth as core_auth
from core.session_token import make_token
from core.storage import get_conn
from core.telegram_auth import validate_init_data
from webapp.templating import render

router = APIRouter(prefix="/miniapp")

BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")


@router.get("")
def miniapp_page(request: Request):
    return render(request, "miniapp.html", staff=None)


@router.post("/auto")
def miniapp_auto(request: Request, initData: str = Form(...)):
    user = validate_init_data(initData, BOT_TOKEN) if BOT_TOKEN else None
    if not user:
        return render(
            request, "miniapp.html", staff=None,
            error="Не удалось подтвердить данные Telegram, откройте приложение заново.",
        )

    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, user["id"])
    if not staff:
        return render(
            request, "miniapp.html", staff=None,
            error="Этот Telegram-аккаунт не привязан к CRM. Обратитесь к владельцу, чтобы он вас добавил.",
        )

    token = make_token(staff["id"])
    return RedirectResponse(f"/?t={token}", status_code=303)
