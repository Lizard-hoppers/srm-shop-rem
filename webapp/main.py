from __future__ import annotations

import os

from fastapi import FastAPI, Request
from starlette.staticfiles import StaticFiles

from core import storage
from core.storage import init_db
from core.store_prefs import init_db as init_store_prefs_db
from core.stores import load_stores
from webapp.deps import resolve_store_for_request
from webapp.routers import buyback, cash, clients, dashboard, hubs, inventory, journal, masters, miniapp, print_agent, purchases, reports, repairs, sales, settings, store, transfers

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
app.include_router(buyback.router)
app.include_router(journal.router)
app.include_router(transfers.router)


@app.middleware("http")
async def store_context_middleware(request: Request, call_next):
    """Resolves which точка this request belongs to (from its ?t= token,
    the first one if none/unrecognized) — routes read it back through
    webapp.deps.loc(). Every точка lives in the same base since 06.10, so
    the db-path contextvar below now always gets the same file; it stays
    because get_conn() with no argument reads it."""
    store = resolve_store_for_request(request)
    request.state.store = store
    token = storage.set_current_db_path(store.db_path)
    try:
        return await call_next(request)
    finally:
        storage.reset_current_db_path(token)


@app.on_event("startup")
def on_startup():
    init_db(load_stores()[0].db_path)
    init_store_prefs_db()
