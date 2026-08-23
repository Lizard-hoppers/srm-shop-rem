"""Stateless auth token carried in the URL (?t=...) instead of a cookie.

Telegram's in-app WebView was observed to drop session cookies between
separate requests (POST /miniapp/auto set-cookie, the following GET / came
back unauthenticated). A signed token that travels with the URL itself has
no such dependency — every request carries its own proof.

Фаза A (23.08, мультимагазинность): the payload now also carries store_id,
so a request knows which store's SQLite file to read/write without a
second lookup. A token minted before this change has no store_id in its
payload — read_token() fills that gap with the default store rather than
rejecting the token, so nobody already logged in gets kicked out by the
upgrade.
"""
from __future__ import annotations

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from core.stores import default_store_id

MAX_AGE_SECONDS = 30 * 24 * 3600

_secret = os.environ.get("CRM_SECRET_KEY")
_serializer = URLSafeTimedSerializer(_secret, salt="miniapp-token") if _secret else None


def make_token(staff_id: int, store_id: str | None = None) -> str:
    return _serializer.dumps({"staff_id": staff_id, "store_id": store_id or default_store_id()})


def read_token(token: str) -> dict | None:
    if not token or not _serializer:
        return None
    try:
        data = _serializer.loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    staff_id = data.get("staff_id")
    if staff_id is None:
        return None
    return {"staff_id": staff_id, "store_id": data.get("store_id") or default_store_id()}
