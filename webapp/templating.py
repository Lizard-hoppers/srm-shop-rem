import os
import time

from fastapi import Request
from fastapi.templating import Jinja2Templates

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
                row = get_store_settings(conn)
            ctx["store_name"] = row["name"] if row else None
        else:
            ctx["store_name"] = None

    return templates.TemplateResponse(name, ctx)
