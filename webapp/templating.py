import os
import time

from fastapi import Request
from fastapi.templating import Jinja2Templates

from core.i18n import DEFAULT_LANGUAGE, t as translate
from core.repairs import STATUS_LABELS
from core.timefmt import kyiv_datetime, ru_date
from webapp.deps import ROLE_LABELS, current_staff, link, request_token

REASON_LABELS = {
    "receipt": "Приход",
    "sale": "Продажа",
    "repair_use": "Списано на ремонт",
    "adjustment": "Корректировка/списание",
    "transfer": "Перемещение",
}

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["role_labels"] = ROLE_LABELS
templates.env.globals["reason_labels"] = REASON_LABELS
templates.env.globals["status_labels"] = STATUS_LABELS
templates.env.filters["kyiv"] = kyiv_datetime
templates.env.filters["rudate"] = ru_date

# Telegram's WebView caches static/* by ETag and can keep serving a stale
# copy after a deploy; tying every static asset URL to this process's start
# time forces a fresh fetch after every deploy+restart, whatever changed.
templates.env.globals["asset_version"] = str(int(time.time()))


def render(request: Request, name: str, **ctx):
    ctx.setdefault("staff", current_staff(request))
    ctx["request"] = request
    ctx["token"] = request_token(request)
    ctx["link"] = lambda path: link(request, path)
    lang = (ctx["staff"]["language"] if ctx["staff"] else None) or DEFAULT_LANGUAGE
    ctx["t"] = lambda key: translate(key, lang)
    return templates.TemplateResponse(name, ctx)
