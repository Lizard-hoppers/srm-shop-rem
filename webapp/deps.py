from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from core import auth
from core.session_token import read_token
from core.storage import get_conn
from core.stores import StoreConfig, default_store_id, get_store

ROLE_LABELS = {
    "owner": "Владелец",
    "admin": "Администратор",
    "master": "Мастер",
    "storekeeper": "Кладовщик",
}


def request_token(request: Request) -> str:
    return request.query_params.get("t", "")


def resolve_store_for_request(request: Request) -> StoreConfig:
    """Which store's DB this request should read/write. Called by
    webapp.main's middleware before any route runs (so get_conn() with no
    explicit db_path resolves correctly), and safe to call again from a
    route body — cheap (itsdangerous verify + a small JSON read)."""
    data = read_token(request_token(request))
    store_id = data["store_id"] if data else default_store_id()
    try:
        return get_store(store_id)
    except KeyError:
        return get_store(default_store_id())


def current_staff(request: Request):
    token = request_token(request)
    data = read_token(token)
    if not data:
        return None
    with get_conn() as c:
        return auth.get_staff_by_id(c, data["staff_id"])


def require_staff(request: Request):
    staff = current_staff(request)
    if not staff:
        raise HTTPException(status_code=303, headers={"Location": "/miniapp"})
    return staff


def require_role(*roles: str):
    """Like require_staff, but also rejects staff whose role isn't in `roles`.
    Use on routes where the wrong role acting isn't just a UX mismatch but an
    actual privilege boundary (financial reports, stock write-off/transfer)."""

    def dependency(staff=Depends(require_staff)):
        if staff["role"] not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав для этого действия.")
        return staff

    return dependency


def link(request: Request, path: str) -> str:
    """Build an internal URL that keeps the current auth token attached."""
    token = request_token(request)
    if not token:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}t={token}"


def optional_int(value: str) -> int | None:
    """Parse an optional numeric form field. FastAPI/Pydantic reject an empty
    string for `int | None`, but an empty <select>/<input> submits exactly
    that (e.g. the "not assigned" option) — so routes take these as plain
    `str = Form("")` and convert with this instead of a typed Form(...)."""
    value = value.strip()
    return int(value) if value else None
