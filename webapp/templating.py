import os
import time
import uuid

from fastapi import Request
from fastapi.templating import Jinja2Templates

from core.accounts import money as normalize_money
from core.i18n import DEFAULT_LANGUAGE, t as translate
from core.storage import get_conn
from core.store_settings import get_settings as get_store_settings
from core.stores import load_stores
from core.timefmt import kyiv_datetime, ru_date
from webapp.deps import ROLE_LABELS, current_staff, link, request_token

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.globals["role_labels"] = ROLE_LABELS
templates.env.filters["kyiv"] = kyiv_datetime
templates.env.filters["rudate"] = ru_date


def _money(value) -> str:
    """Amounts as staff read them: «500», «12.5», «1 200» is NOT applied
    (existing screens show bare numbers) — only the float tail goes: a
    whole amount never renders as «500.0»."""
    if value is None:
        return "—"
    normalized = normalize_money(value)
    return str(normalized)


templates.env.filters["money"] = _money

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
    # One token per rendered page, dropped into every document-creating
    # form as a hidden field — see webapp.deps.idem_key.
    ctx["idem"] = uuid.uuid4().hex

    # Фаза B (23.08): a small "which store am I in" indicator in the
    # appbar — only worth the extra query when more than one store is
    # even configured (multi_store), and only meaningful once staff is
    # logged in. A route that already fetched settings itself (webapp/
    # routers/store.py) passes store_name/multi_store explicitly and this
    # is skipped for it.
    if ctx["staff"] and "store_name" not in ctx:
        ctx["multi_store"] = len(load_stores()) > 1
        if ctx["multi_store"]:
            with get_conn() as conn:
                row = get_store_settings(conn, request.state.store.location_id)
            ctx["store_name"] = row["name"] if row else None
        else:
            ctx["store_name"] = None

    return templates.TemplateResponse(name, ctx)
