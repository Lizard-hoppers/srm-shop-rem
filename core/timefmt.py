"""Display formatting for timestamps/dates. Everything is stored in SQLite
as UTC (`datetime('now')`); the shop is in Kyiv, so every place a timestamp
reaches the screen it gets converted to Europe/Kyiv and shown as
дд.мм.гггг — never the raw UTC string.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

KYIV = ZoneInfo("Europe/Kyiv")
UTC = ZoneInfo("UTC")


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


def kyiv_today() -> str:
    """Today's date in Kyiv local time, as 'YYYY-MM-DD' — what a date
    picker/query param should default to (касса «за сегодня»), since the
    shop's day boundary is Kyiv midnight, not UTC midnight."""
    return datetime.now(KYIV).strftime("%Y-%m-%d")


def kyiv_date_range_utc(date_from: str, date_to: str) -> tuple[str, str]:
    """Both args are inclusive Kyiv-local dates ('YYYY-MM-DD') — a report
    period picker's "from"/"to". Returns (start, end) as naive UTC
    'YYYY-MM-DD HH:MM:SS' strings matching how SQLite's datetime('now')
    stores created_at, so callers can filter with
    `created_at >= start AND created_at < end` and get exact Kyiv-calendar
    days rather than an off-by-a-few-hours UTC day."""
    start_local = datetime.strptime(date_from, "%Y-%m-%d").replace(tzinfo=KYIV)
    end_local = datetime.strptime(date_to, "%Y-%m-%d").replace(tzinfo=KYIV) + timedelta(days=1)
    return (
        start_local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        end_local.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"),
    )
