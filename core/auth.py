"""Staff password hashing and login. No extra dependency: stdlib pbkdf2."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3

_ITERATIONS = 260_000

PAY_TYPES = {"percent": "Процент от прибыли", "fixed": "Фиксированная ставка за ремонт"}


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


def set_staff_language(conn: sqlite3.Connection, staff_id: int, language: str) -> None:
    conn.execute("UPDATE staff SET language = ? WHERE id = ?", (language, staff_id))


# ---- masters (21.08) ----
#
# login/password_hash are dead weight for a master specifically: the real
# Mini App login (webapp.routers.miniapp) is Telegram-id-only, so nothing
# ever checks a master's password. create_master() fills those columns
# with throwaway values (a slugified-name login, a random password) purely
# to satisfy the schema's NOT NULL/UNIQUE — Павел never sees or picks
# either. telegram_id is optional here: masters don't have Mini App access
# yet (21.08), so a master can be added purely for pay-rate/stats tracking
# and linked to Telegram later, either by editing this same row or the
# older core.link_telegram CLI.

def _slugify_login(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "master"


def create_master(
    conn: sqlite3.Connection,
    name: str,
    telegram_id: int | None,
    pay_type: str | None,
    pay_value: int | None,
) -> int:
    base_login = _slugify_login(name)
    login, suffix = base_login, 0
    while conn.execute("SELECT 1 FROM staff WHERE login = ?", (login,)).fetchone():
        suffix += 1
        login = f"{base_login}-{suffix}"
    cur = conn.execute(
        """INSERT INTO staff (login, password_hash, name, role, telegram_id, pay_type, pay_value)
           VALUES (?, ?, ?, 'master', ?, ?, ?)""",
        (login, hash_password(secrets.token_hex(16)), name.strip(), telegram_id, pay_type, pay_value),
    )
    return cur.lastrowid


def list_masters(conn: sqlite3.Connection, include_inactive: bool = False) -> list[sqlite3.Row]:
    if include_inactive:
        return conn.execute(
            "SELECT * FROM staff WHERE role = 'master' ORDER BY active DESC, name"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM staff WHERE role = 'master' AND active = 1 ORDER BY name"
    ).fetchall()


def get_master(conn: sqlite3.Connection, staff_id: int) -> sqlite3.Row | None:
    """Unlike get_staff_by_id, doesn't filter active=1 — an admin needs to
    open a deactivated master's card to review history or reactivate them."""
    return conn.execute("SELECT * FROM staff WHERE id = ? AND role = 'master'", (staff_id,)).fetchone()


def update_master(
    conn: sqlite3.Connection,
    staff_id: int,
    name: str,
    telegram_id: int | None,
    pay_type: str | None,
    pay_value: int | None,
) -> None:
    conn.execute(
        "UPDATE staff SET name = ?, telegram_id = ?, pay_type = ?, pay_value = ? WHERE id = ? AND role = 'master'",
        (name.strip(), telegram_id, pay_type, pay_value, staff_id),
    )


def set_master_active(conn: sqlite3.Connection, staff_id: int, active: bool) -> None:
    """«Удалить» a master is always a deactivation, never a row delete —
    repair_orders.master_id and stock_movements.staff_id reference this row,
    deleting it would either violate the FK or silently orphan history."""
    conn.execute("UPDATE staff SET active = ? WHERE id = ? AND role = 'master'", (int(active), staff_id))
