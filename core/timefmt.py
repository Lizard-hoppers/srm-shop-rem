"""Display formatting for timestamps/dates. Everything is stored in SQLite
as UTC (`datetime('now')`); the shop is in Kyiv, so every place a timestamp
reaches the screen it gets converted to Europe/Kyiv and shown as
дд.мм.гггг — never the raw UTC string.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")


def kyiv_datetime(value: str | None) -> str:
    """SQLite 'YYYY-MM-DD HH:MM:SS' (UTC, naive) -> 'дд.мм.гггг чч:мм' (Kyiv)."""
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=ZoneInfo("UTC"))
    except ValueError:
        return value
    return dt.astimezone(KYIV).strftime("%d.%m.%Y %H:%M")


def ru_date(value: str | None) -> str:
    """Plain date field like warranty_until ('YYYY-MM-DD', no time/tz) -> 'дд.мм.гггг'."""
    if not value:
        return "—"
    try:
        dt = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return value
    return dt.strftime("%d.%m.%Y")
