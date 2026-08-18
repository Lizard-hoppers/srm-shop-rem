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


def normalize_phone(phone: str) -> str:
    """Canonicalize to one form so the same person always matches the same
    client record, whatever format the number arrived in:
      +380501234567  already canonical
      380501234567   how Telegram sends a shared contact, no leading +
      0501234567     how staff type it at the counter (local format)
    """
    phone = phone.strip().replace(" ", "").replace("-", "")
    if not phone:
        return phone
    if phone.startswith("+"):
        return phone
    if phone.startswith("380") and phone[3:].isdigit():
        return "+" + phone
    if phone.startswith("0") and phone.isdigit() and len(phone) == 10:
        return "+380" + phone[1:]
    if phone.isdigit():
        return "+" + phone
    return phone


def get_or_create_by_phone(conn: sqlite3.Connection, name: str, phone: str, source: str = "offline") -> int:
    """Reuse an existing client matched by phone, or register a new one on the spot."""
    phone = normalize_phone(phone)
    existing = conn.execute("SELECT id FROM clients WHERE phone = ?", (phone,)).fetchone()
    if existing:
        return existing["id"]
    return create_client(conn, name=name.strip(), phone=phone, source=source)


def get_by_telegram_id(conn: sqlite3.Connection, telegram_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM clients WHERE telegram_id = ?", (telegram_id,)).fetchone()


def link_telegram(conn: sqlite3.Connection, client_id: int, telegram_id: int) -> None:
    conn.execute("UPDATE clients SET telegram_id = ? WHERE id = ?", (telegram_id, client_id))
