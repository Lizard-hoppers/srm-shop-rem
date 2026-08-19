"""Client loyalty QR codes. The QR payload is just `CRMCID:<client_id>` —
plain enough that both the staff scanner (Telegram's native QR popup) and
the bot's own generated codes agree on one format with zero ambiguity.

Products use a real Code128 barcode of their own SKU instead (see
core.barcode_label) — Павел wanted scanning to work off the part's
existing barcode digits, not an app-invented id, so products were moved
off this QR scheme entirely (18.08).
"""
from __future__ import annotations

import io

import qrcode

_CLIENT_PREFIX = "CRMCID:"


def client_code(client_id: int) -> str:
    return f"{_CLIENT_PREFIX}{client_id}"


def parse_client_code(text: str) -> int | None:
    text = text.strip()
    if not text.startswith(_CLIENT_PREFIX):
        return None
    try:
        return int(text[len(_CLIENT_PREFIX):])
    except ValueError:
        return None


def generate_png(data: str) -> bytes:
    img = qrcode.make(data, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
