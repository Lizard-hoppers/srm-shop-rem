"""Журнал Telegram-пользователей (23.10, из старого бота Гибрида): кто
пишет каждому боту CRM или жмёт его кнопки — в личке и в группах, сколько
раз, в каком чате последний раз. По нему заводят людей точки в CRM: из
журнала видно Telegram-id того, кто реально работает, и одной кнопкой
он становится сотрудником (webapp.routers.tg_users).

One row per (bot, person): each bot is a точка (core.bots), so the same
person seen by two bots is two lines — «где он работает» is the bot.
"""
from __future__ import annotations

import sqlite3

from core import auth


def record(conn: sqlite3.Connection, bot_id: int, user, chat, kind: str) -> None:
    """kind: 'private' | 'group' | 'callback'. user/chat — aiogram objects
    (or anything with the same attributes)."""
    column = {"private": "private_messages", "group": "group_messages", "callback": "callbacks"}[kind]
    title = getattr(chat, "title", None) or ("личка" if getattr(chat, "type", "") == "private" else None)
    conn.execute(
        f"""INSERT INTO tg_users (bot_id, user_id, username, full_name, {column}, last_chat, first_seen, last_seen)
            VALUES (?, ?, ?, ?, 1, ?, datetime('now'), datetime('now'))
            ON CONFLICT(bot_id, user_id) DO UPDATE SET
              username = excluded.username, full_name = excluded.full_name,
              {column} = {column} + 1, last_chat = COALESCE(excluded.last_chat, last_chat),
              last_seen = excluded.last_seen""",
        (bot_id, user.id, getattr(user, "username", None), getattr(user, "full_name", None) or "", title),
    )


def list_for_bot(conn: sqlite3.Connection, bot_ids: list[int]) -> list[sqlite3.Row]:
    """The journal of these bots, the most recently seen first, with the
    CRM staff each person already is (if any)."""
    if not bot_ids:
        return []
    marks = ",".join("?" * len(bot_ids))
    return conn.execute(
        f"""SELECT j.*, s.id AS staff_id, s.name AS staff_name, s.role AS staff_role
            FROM tg_users j LEFT JOIN staff s ON s.telegram_id = j.user_id AND s.active = 1
            WHERE j.bot_id IN ({marks}) ORDER BY j.last_seen DESC""",
        bot_ids,
    ).fetchall()


ROLES = ("manager", "master", "storekeeper")


def make_staff(conn: sqlite3.Connection, user_id: int, name: str, role: str, location_id: int) -> int:
    """A person from the journal becomes CRM staff of the точка, linked to
    his Telegram. A master goes through create_master (склад мастера);
    anyone else gets a throwaway password — login is by Telegram only."""
    import secrets

    if role not in ROLES:
        raise ValueError("неизвестная роль")
    if auth.get_staff_by_telegram_id(conn, user_id):
        raise ValueError("этот Telegram уже привязан к сотруднику")
    name = " ".join(str(name or "").split()) or f"tg{user_id}"
    if role == "master":
        staff_id = auth.create_master(conn, name, user_id, None, None)
        conn.execute("UPDATE staff SET location_id = ? WHERE id = ?", (location_id, staff_id))
        return staff_id
    login = f"tg{user_id}"
    staff_id = auth.create_staff(conn, login, secrets.token_urlsafe(16), name, role, location_id)
    auth.link_staff_telegram(conn, login, user_id)
    return staff_id
