from __future__ import annotations

import os

from fastapi import FastAPI, Request
from starlette.staticfiles import StaticFiles

from core import storage
from core.storage import init_db
from core.store_prefs import init_db as init_store_prefs_db
from core.stores import load_stores
from webapp.deps import resolve_store_for_request
from webapp.routers import cash, clients, dashboard, hubs, inventory, masters, miniapp, print_agent, purchases, reports, repairs, sales, settings, store

if not os.environ.get("CRM_SECRET_KEY"):
    raise RuntimeError("CRM_SECRET_KEY env var is required (auth token signing key)")

app = FastAPI(title="Electronics CRM")
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

app.include_router(dashboard.router)
app.include_router(clients.router)
app.include_router(inventory.router)
app.include_router(repairs.router)
app.include_router(purchases.router)
app.include_router(sales.router)
app.include_router(reports.router)
app.include_router(hubs.router)
app.include_router(miniapp.router)
app.include_router(print_agent.router)
app.include_router(settings.router)
app.include_router(cash.router)
app.include_router(masters.router)
app.include_router(store.router)


@app.middleware("http")
async def store_context_middleware(request: Request, call_next):
    """Resolves which store this request belongs to (from its ?t= token,
    default store if none/unrecognized) and points core.storage.get_conn()
    at that store's DB file for the duration of the request. See
    core/stores.py and webapp/deps.py::resolve_store_for_request."""
    store = resolve_store_for_request(request)
    request.state.store = store
    token = storage.set_current_db_path(store.db_path)
    try:
        return await call_next(request)
    finally:
        storage.reset_current_db_path(token)


@app.on_event("startup")
def on_startup():
    for store_config in load_stores():
        init_db(store_config.db_path)
    init_store_prefs_db()
