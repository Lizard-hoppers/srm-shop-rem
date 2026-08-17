from __future__ import annotations

from fastapi import HTTPException, Request

from core import auth
from core.session_token import read_token
from core.storage import get_conn

ROLE_LABELS = {
    "owner": "Владелец",
    "admin": "Администратор",
    "master": "Мастер",
    "storekeeper": "Кладовщик",
}


def request_token(request: Request) -> str:
    return request.query_params.get("t", "")


def current_staff(request: Request):
    token = request_token(request)
    staff_id = read_token(token)
    if not staff_id:
        return None
    with get_conn() as c:
        return auth.get_staff_by_id(c, staff_id)


def require_staff(request: Request):
    staff = current_staff(request)
    if not staff:
        raise HTTPException(status_code=303, headers={"Location": "/miniapp"})
    return staff


def link(request: Request, path: str) -> str:
    """Build an internal URL that keeps the current auth token attached."""
    token = request_token(request)
    if not token:
        return path
    sep = "&" if "?" in path else "?"
    return f"{path}{sep}t={token}"
