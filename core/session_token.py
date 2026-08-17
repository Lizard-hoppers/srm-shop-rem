"""Stateless auth token carried in the URL (?t=...) instead of a cookie.

Telegram's in-app WebView was observed to drop session cookies between
separate requests (POST /miniapp/auto set-cookie, the following GET / came
back unauthenticated). A signed token that travels with the URL itself has
no such dependency — every request carries its own proof.
"""
from __future__ import annotations

import os

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

MAX_AGE_SECONDS = 30 * 24 * 3600

_secret = os.environ.get("CRM_SECRET_KEY")
_serializer = URLSafeTimedSerializer(_secret, salt="miniapp-token") if _secret else None


def make_token(staff_id: int) -> str:
    return _serializer.dumps({"staff_id": staff_id})


def read_token(token: str) -> int | None:
    if not token or not _serializer:
        return None
    try:
        data = _serializer.loads(token, max_age=MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data.get("staff_id")
