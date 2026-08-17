from __future__ import annotations

from fastapi import HTTPException, Request

from core import auth
from core.storage import get_conn


def db():
    with get_conn() as conn:
        yield conn


ROLE_LABELS = {
    "owner": "Владелец",
    "admin": "Администратор",
    "master": "Мастер",
    "storekeeper": "Кладовщик",
}


def current_staff(request: Request):
    staff_id = request.session.get("staff_id")
    if not staff_id:
        return None
    with get_conn() as c:
        return auth.get_staff_by_id(c, staff_id)


def require_staff(request: Request):
    staff = current_staff(request)
    if not staff:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return staff
