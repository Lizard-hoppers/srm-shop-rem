"""Telegram Mini App entry point: same login form as the desktop panel,
opened inside Telegram's WebView via the bot's menu button.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

from webapp.templating import render

router = APIRouter(prefix="/miniapp")


@router.get("")
def miniapp_page(request: Request):
    return render(request, "miniapp.html", staff=None)
