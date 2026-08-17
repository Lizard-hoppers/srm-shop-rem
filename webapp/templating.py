import os

from fastapi import Request
from fastapi.templating import Jinja2Templates

from core.repairs import STATUS_LABELS
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


def render(request: Request, name: str, **ctx):
    ctx.setdefault("staff", current_staff(request))
    ctx["request"] = request
    ctx["token"] = request_token(request)
    ctx["link"] = lambda path: link(request, path)
    return templates.TemplateResponse(name, ctx)
