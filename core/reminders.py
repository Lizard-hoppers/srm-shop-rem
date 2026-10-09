"""Напоминания (10.10): «бот, напомни завтра в 10:20 спросить Андрея про
готовность», «бот, напомни в пятницу отдать 13 про мах» — at the time
named the bot posts the line into the chat, so nobody has to keep it in
their head.

This module is the book of reminders and the arithmetic of «когда»; who
asked, in which chat, and the sending itself are the bot's
(bot/assistant_chat.py, bot/bot.py). Times are kept in UTC like every
other timestamp in the base and shown in Kyiv time.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from core.timefmt import HUMAN_FORMAT, KYIV, UTC

DEFAULT_TIME = "10:00"      # «напомни в пятницу» — with no hour named
MAX_AHEAD_DAYS = 366
MAX_ATTEMPTS = 20           # ticks a reminder is retried for when Telegram won't take it
_FMT = "%Y-%m-%d %H:%M:%S"


class ReminderError(Exception):
    """A reminder that can't be set — message is shown to staff as-is."""


def resolve_due(date: str | None, time: str | None, now: datetime | None = None) -> datetime:
    """When to remind, as a Kyiv datetime, from what was understood of the
    request: a date ('YYYY-MM-DD') and/or a time ('HH:MM'), either may be
    missing. Only a time — today, or tomorrow if that hour has passed.
    Only a date — at DEFAULT_TIME. Neither, the past, or nonsense —
    ReminderError."""
    now = now or datetime.now(KYIV)
    if not date and not time:
        raise ReminderError("Не понял, когда напомнить. Скажите, например: «бот, напомни завтра в 15:00 отдать 13 про мах».")
    try:
        hour, minute = (int(part) for part in (time or DEFAULT_TIME).split(":")[:2])
        if date:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        else:
            day = now.date()
        due = datetime(day.year, day.month, day.day, hour, minute, tzinfo=KYIV)
    except (ValueError, TypeError) as exc:
        raise ReminderError("Не разобрал дату или время. Скажите, например: «завтра в 15:00» или «12.10 в 9:30».") from exc
    if not date and due <= now:
        due += timedelta(days=1)
    if due <= now:
        raise ReminderError(f"Это время уже прошло ({due.strftime(HUMAN_FORMAT)}). Назовите время в будущем.")
    if due - now > timedelta(days=MAX_AHEAD_DAYS):
        raise ReminderError("Слишком далеко — напоминание можно поставить не дальше чем на год.")
    return due


def kyiv(due_at_utc: str) -> str:
    """'12.10.2026 15:00' (Kyiv) for a stored due_at."""
    return datetime.strptime(due_at_utc, _FMT).replace(tzinfo=UTC).astimezone(KYIV).strftime(HUMAN_FORMAT)


def create(
    conn: sqlite3.Connection, *, text: str, due: datetime, chat_id: str | int, thread_id: int | None = None,
    location_id: int | None = None, order_id: int | None = None, staff_id: int | None = None,
    author_name: str | None = None,
) -> int:
    text = " ".join((text or "").split())
    if not text:
        raise ReminderError("О чём напомнить? Скажите, например: «бот, напомни завтра в 15:00 позвонить клиенту».")
    return conn.execute(
        """INSERT INTO reminders (location_id, chat_id, thread_id, order_id, text, due_at, staff_id, author_name)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (location_id, str(chat_id), thread_id, order_id, text[:500], due.astimezone(UTC).strftime(_FMT),
         staff_id, (author_name or "").strip() or None),
    ).lastrowid


_SELECT = """SELECT reminders.*, COALESCE(staff.name, reminders.author_name, '—') AS author
             FROM reminders LEFT JOIN staff ON staff.id = reminders.staff_id"""


def get(conn: sqlite3.Connection, reminder_id: int) -> sqlite3.Row | None:
    return conn.execute(_SELECT + " WHERE reminders.id = ?", (reminder_id,)).fetchone()


def due_now(conn: sqlite3.Connection, now: datetime | None = None) -> list[sqlite3.Row]:
    """Pending reminders whose time has come, oldest first."""
    stamp = (now or datetime.now(UTC)).astimezone(UTC).strftime(_FMT)
    return conn.execute(
        _SELECT + " WHERE reminders.status = 'pending' AND reminders.due_at <= ? ORDER BY reminders.due_at, reminders.id",
        (stamp,),
    ).fetchall()


def pending(conn: sqlite3.Connection, location_id: int | None = None, limit: int = 30) -> list[sqlite3.Row]:
    query, params = _SELECT + " WHERE reminders.status = 'pending'", []
    if location_id is not None:
        query += " AND reminders.location_id = ?"
        params.append(location_id)
    return conn.execute(query + " ORDER BY reminders.due_at, reminders.id LIMIT ?", [*params, limit]).fetchall()


def mark_sent(conn: sqlite3.Connection, reminder_id: int) -> None:
    conn.execute("UPDATE reminders SET status = 'sent', sent_at = datetime('now') WHERE id = ? AND status = 'pending'", (reminder_id,))


def mark_failed(conn: sqlite3.Connection, reminder_id: int) -> None:
    """Telegram didn't take it this tick: it stays pending and is tried
    again, MAX_ATTEMPTS times — then given up on rather than retried forever."""
    conn.execute("UPDATE reminders SET attempts = attempts + 1 WHERE id = ?", (reminder_id,))
    conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND attempts >= ?", (reminder_id, MAX_ATTEMPTS))


def cancel(conn: sqlite3.Connection, reminder_id: int) -> bool:
    """True if a pending reminder was cancelled (False — already sent or cancelled)."""
    cur = conn.execute("UPDATE reminders SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (reminder_id,))
    return cur.rowcount > 0


def snooze(conn: sqlite3.Connection, reminder_id: int, minutes: int = 60, now: datetime | None = None) -> str | None:
    """«Напомнить ещё раз через час» under a reminder that has gone out:
    it becomes pending again. Returns the new time (Kyiv) or None."""
    row = get(conn, reminder_id)
    if not row or row["status"] == "pending":
        return None
    due = ((now or datetime.now(UTC)).astimezone(UTC) + timedelta(minutes=minutes)).strftime(_FMT)
    conn.execute("UPDATE reminders SET status = 'pending', due_at = ?, attempts = 0, sent_at = NULL WHERE id = ?", (due, reminder_id))
    return kyiv(due)
