from __future__ import annotations

import os

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from core.storage import init_db
from webapp.routers import clients, dashboard, inventory, miniapp, purchases, reports, repairs, sales

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
app.include_router(miniapp.router)


@app.on_event("startup")
def on_startup():
    init_db()
