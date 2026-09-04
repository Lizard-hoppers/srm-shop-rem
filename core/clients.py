"""Client CRUD."""
from __future__ import annotations

import sqlite3


def list_clients(
    conn: sqlite3.Connection, search: str | None = None, source: str | None = None
) -> list[sqlite3.Row]:
    query = "SELECT * FROM clients WHERE 1=1"
    params: list = []
    if search:
        query += " AND (name LIKE ? OR phone LIKE ?)"
        like = f"%{search}%"
        params.extend([like, like])
    if source:
        query += " AND source = ?"
        params.append(source)
    query += " ORDER BY created_at DESC"
    return conn.execute(query, params).fetchall()


def get_client(conn: sqlite3.Connection, client_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE id = ?", (client_id,)).fetchone()


def create_client(
    conn: sqlite3.Connection,
    name: str,
    phone: str | None = None,
    telegram_id: int | None = None,
    source: str = "offline",
    notes: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO clients (name, phone, telegram_id, source, notes) VALUES (?, ?, ?, ?, ?)",
        (name, phone, telegram_id, source, notes),
    )
    return cur.lastrowid


def update_client(
    conn: sqlite3.Connection,
    client_id: int,
    name: str,
    phone: str | None,
    notes: str | None,
) -> None:
    conn.execute(
        "UPDATE clients SET name = ?, phone = ?, notes = ? WHERE id = ?",
        (name, phone, notes, client_id),
    )


def get_client_devices(conn: sqlite3.Connection, client_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM devices WHERE client_id = ? ORDER BY created_at DESC", (client_id,)
    ).fetchall()


# Separators people put in a phone number by hand or paste out of a
# contact card: «+38 (050) 123-45-67». They carry no information, so they
# come off before anything else looks at the number.
_PHONE_SEPARATORS = str.maketrans("", "", " \u00a0-()")

# The form fields default to "+380" as a typing template (19.08) so staff
# don't retype the country code — nobody actually touched the field if
# that's all that's in it.
_PHONE_TEMPLATE = "+380"


def _strip_phone_separators(phone: str) -> str:
    return phone.strip().translate(_PHONE_SEPARATORS)


def phone_looks_entered(phone: str) -> bool:
    """Did someone actually type something into the phone field? Tells a
    blank (or untouched «+380» template) apart from a real but malformed
    attempt, so a form where the phone is OPTIONAL can still say «проверьте
    номер» instead of silently filing a client with no phone at all.
    Forms where the phone is REQUIRED don't need this — for them
    normalize_phone() returning "" is already the whole answer."""
    cleaned = _strip_phone_separators(phone)
    return bool(cleaned) and cleaned != _PHONE_TEMPLATE


def normalize_phone(phone: str) -> str:
    """Canonicalize to one form so the same person always matches the same
    client record, whatever format the number arrived in:
      +380501234567  already canonical
      380501234567   how Telegram sends a shared contact, no leading +
      0501234567     how staff type it at the counter (local format)

    Returns "" for anything that is not a usable number at all: blank, the
    untouched «+380» template, or free text («не помню», «спросить у
    Васи»). Every caller already reads "" as «no phone» and either refuses
    the form or stores NULL, so one return value covers both — use
    phone_looks_entered() above when the difference has to be reported
    back to the person. Before 04.09.2026 this function returned free text
    unchanged, so «не телефон» sailed through every check in the codebase
    and landed in the clients table as somebody's phone number."""
    phone = _strip_phone_separators(phone)
    if not phone or phone == _PHONE_TEMPLATE:
        return ""
    digits = phone[1:] if phone.startswith("+") else phone
    # E.164 caps a real number at 15 digits; below 7 nothing is reachable.
    # Anything with a letter in it is free text, not a phone number.
    if not digits.isdigit() or not 7 <= len(digits) <= 15:
        return ""
    if phone.startswith("+"):
        return phone
    if digits.startswith("380"):
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+380" + digits[1:]
    return "+" + digits


def get_or_create_by_phone(conn: sqlite3.Connection, name: str, phone: str, source: str = "offline") -> int:
    """Reuse an existing client matched by phone, or register a new one on the spot."""
    phone = normalize_phone(phone)
    if phone:
        # Only ever match on a real number. Looking up "" would merge every
        # phoneless walk-in client into whichever one was created first.
        existing = conn.execute("SELECT id FROM clients WHERE phone = ?", (phone,)).fetchone()
        if existing:
            return existing["id"]
    return create_client(conn, name=name.strip(), phone=phone or None, source=source)


def get_by_telegram_id(conn: sqlite3.Connection, telegram_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)).fetchone()


def link_telegram(conn: sqlite3.Connection, client_id: int, telegram_id: int) -> None:
    conn.execute("UPDATE clients SET telegram_id = ? WHERE id = ?", (telegram_id, client_id))
