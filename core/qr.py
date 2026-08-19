"""QR codes for entities staff might want to look up by camera scan.
Each kind has its own short prefix so a scanned code is unambiguous —
`CRMCID:<id>` for a client (loyalty card), `CRMPID:<id>` for a product
(shelf/part label) — both the staff scanner (Telegram's native QR popup)
and the app's own generated codes agree on the same format.
"""
from __future__ import annotations

import io

import qrcode

_CLIENT_PREFIX = "CRMCID:"
_PRODUCT_PREFIX = "CRMPID:"


def _parse_prefixed(text: str, prefix: str) -> int | None:
    text = text.strip()
    if not text.startswith(prefix):
        return None
    try:
        return int(text[len(prefix):])
    except ValueError:
        return None


def client_code(client_id: int) -> str:
    return f"{_CLIENT_PREFIX}{client_id}"


def parse_client_code(text: str) -> int | None:
    return _parse_prefixed(text, _CLIENT_PREFIX)


def product_code(product_id: int) -> str:
    return f"{_PRODUCT_PREFIX}{product_id}"


def parse_product_code(text: str) -> int | None:
    return _parse_prefixed(text, _PRODUCT_PREFIX)


def generate_png(data: str) -> bytes:
    img = qrcode.make(data, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
