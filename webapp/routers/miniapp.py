"""Telegram Mini App entry point. No password, ever: a staff member is
recognized purely by their Telegram id (linked ahead of time by an admin via
`python -m core.link_telegram <login> <telegram_id> [--store=<id>]`).

Auth here goes through a real browser form POST + server redirect that
carries a signed token in the URL (?t=...), not a cookie: Telegram's
in-app WebView was observed to drop Set-Cookie between separate requests
even across a same-origin 303 redirect, so nothing here depends on cookies.

Фаза B (23.08): a telegram_id can have a staff identity in more than one
store (owner/admin get a separate row in each store's DB — see
core/stores.py), so login scans every configured store rather than only
the default one (core.store_access.accessible_stores), then picks a target
via core.store_access.pick_default_store (single match, or the last store
this Telegram user switched to).
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from core.session_token import make_token
from core.store_access import accessible_stores, pick_default_store
from core.store_prefs import set_last_store
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

    accessible = accessible_stores(user["id"])
    if not accessible:
        return render(
            request, "miniapp.html", staff=None,
            error="Этот Telegram-аккаунт не привязан к CRM. Обратитесь к владельцу, чтобы он вас добавил.",
        )

    target_store = pick_default_store(user["id"], accessible)
    target_staff = next(st for s, st in accessible if s.id == target_store.id)
    if len(accessible) > 1:
        set_last_store(user["id"], target_store.id)

    token = make_token(target_staff["id"], target_store.id)
    return RedirectResponse(f"/?t={token}", status_code=303)
