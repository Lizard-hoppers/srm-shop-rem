"""Telegram Mini App entry point: auto-login staff by telegram_id via initData,
or fall back to a one-time login+link if this Telegram account isn't linked yet.
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Request
from pydantic import BaseModel

from core import auth as core_auth
from core.storage import get_conn
from core.telegram_auth import validate_init_data
from webapp.templating import render

router = APIRouter(prefix="/miniapp")

BOT_TOKEN = os.environ.get("CRM_BOT_TOKEN")


class AuthPayload(BaseModel):
    initData: str


class LinkPayload(BaseModel):
    initData: str
    login: str
    password: str


@router.get("")
def miniapp_page(request: Request):
    return render(request, "miniapp.html", staff=None)


@router.post("/auth")
def miniapp_auth(request: Request, payload: AuthPayload):
    if not BOT_TOKEN:
        return {"status": "error", "message": "CRM_BOT_TOKEN не настроен на сервере"}
    user = validate_init_data(payload.initData, BOT_TOKEN)
    if not user:
        return {"status": "error", "message": "Не удалось подтвердить данные Telegram"}

    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, user["id"])
    if not staff:
        return {"status": "link_required"}

    request.session["staff_id"] = staff["id"]
    return {"status": "ok"}


@router.post("/link")
def miniapp_link(request: Request, payload: LinkPayload):
    if not BOT_TOKEN:
        return {"status": "error", "message": "CRM_BOT_TOKEN не настроен на сервере"}
    user = validate_init_data(payload.initData, BOT_TOKEN)
    if not user:
        return {"status": "error", "message": "Не удалось подтвердить данные Telegram"}

    with get_conn() as conn:
        staff = core_auth.get_staff_by_login(conn, payload.login.strip())
        if not staff or not core_auth.verify_password(payload.password, staff["password_hash"]):
            return {"status": "error", "message": "Неверный логин или пароль"}
        core_auth.link_staff_telegram(conn, staff["id"], user["id"])

    request.session["staff_id"] = staff["id"]
    return {"status": "ok"}
