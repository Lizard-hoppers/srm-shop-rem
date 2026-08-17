"""Client loyalty QR codes. The QR payload is just `CRMCID:<client_id>` —
plain enough that both the staff scanner (Telegram's native QR popup) and
the bot's own generated codes agree on one format with zero ambiguity.
"""
from __future__ import annotations

import io

import qrcode

_PREFIX = "CRMCID:"


def client_code(client_id: int) -> str:
    return f"{_PREFIX}{client_id}"


def parse_client_code(text: str) -> int | None:
    text = text.strip()
    if not text.startswith(_PREFIX):
        return None
    try:
        return int(text[len(_PREFIX):])
    except ValueError:
        return None


def generate_png(data: str) -> bytes:
    img = qrcode.make(data, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
