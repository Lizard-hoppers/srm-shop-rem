"""Staff password hashing and login. No extra dependency: stdlib pbkdf2."""
from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3

_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    salt = bytes.fromhex(salt_hex)
    expected = bytes.fromhex(digest_hex)
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return hmac.compare_digest(actual, expected)


def get_staff_by_login(conn: sqlite3.Connection, login: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM staff WHERE login = ? AND active = 1", (login,)
    ).fetchone()


def get_staff_by_id(conn: sqlite3.Connection, staff_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM staff WHERE id = ? AND active = 1", (staff_id,)
    ).fetchone()


def create_staff(conn: sqlite3.Connection, login: str, password: str, name: str, role: str) -> int:
    cur = conn.execute(
        "INSERT INTO staff (login, password_hash, name, role) VALUES (?, ?, ?, ?)",
        (login, hash_password(password), name, role),
    )
    return cur.lastrowid


def list_staff(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM staff WHERE active = 1 ORDER BY name").fetchall()


def get_staff_by_telegram_id(conn: sqlite3.Connection, telegram_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM staff WHERE telegram_id = ? AND active = 1", (telegram_id,)
    ).fetchone()


def link_staff_telegram(conn: sqlite3.Connection, login: str, telegram_id: int) -> bool:
    cur = conn.execute("UPDATE staff SET telegram_id = ? WHERE login = ?", (telegram_id, login))
    return cur.rowcount > 0
