from __future__ import annotations

import os

from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from core.storage import init_db
from webapp.routers import auth, clients, dashboard, inventory, miniapp

SECRET_KEY = os.environ.get("CRM_SECRET_KEY")
if not SECRET_KEY:
    raise RuntimeError("CRM_SECRET_KEY env var is required (session signing key)")

app = FastAPI(title="Electronics CRM")
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    session_cookie="crm_session",
    same_site="lax",
    https_only=True,
)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(clients.router)
app.include_router(inventory.router)
app.include_router(miniapp.router)


@app.on_event("startup")
def on_startup():
    init_db()
