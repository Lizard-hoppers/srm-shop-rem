"""Переключение «Гибрид сервиса» со старого бота на CRM (23.10, этап 5).
Выполняется один раз (tools/gibrit_cutover.py), после последней
синхронизации (core.gibrit_sync) и остановки старого бота:

  * точка — в простой режим (core.simple_flow), без смены, с копией в ту же
    Google-таблицу (core.gsheet_mirror); её группа — «РМТ» старого бота;
  * мастерам — их темы в «РМТ» и личные группы из .env старого бота;
  * открытые заказы становятся обычными ремонтами CRM (ext_source →
    «gibrit_moved»): по ним начисляется по ставке, у штатных выплата снова
    считается, а не записана; история остаётся историей — по ней долг не
    появляется, даже если её снова перевести в «Готов»;
  * «наши» заказы старого бота помечаются «📦 Наш»;
  * карточки старого бота в «РМТ» привязываются к ремонтам: бот тот же, он
    может их править — открытые сразу перерисовываются в карточки CRM
    (convert_open), остальные — при первом же действии с ними;
  * журнал пользователей старого бота переносится в журнал Telegram.

После этого core.gibrit_sync отказывается работать (точка в простом режиме).
"""
from __future__ import annotations

import json
import sqlite3

SOURCE, MOVED = "gibrit", "gibrit_moved"
OUR_CLIENT_NAME = "Наш (Гибрид сервис)"
OPEN = ("new", "in_progress", "ready")


def read_env(path: str) -> dict:
    """KEY=VALUE lines of the old bot's .env (JSON values left as text)."""
    result = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def apply(conn: sqlite3.Connection, env: dict, location_id: int, bot_id: int | None, users_db: str | None = None) -> dict:
    """Everything but the bot token and the services. Idempotent."""
    report_chat = int(env["REPORT_CHAT_ID"])
    conn.execute(
        """UPDATE locations SET staff_group_chat_id = ?, repair_topic_id = NULL, simple_flow = 1, shift_required = 0,
                  gsheet_id = ?, updated_at = datetime('now') WHERE id = ?""",
        (report_chat, env.get("GOOGLE_SHEET_ID") or None, location_id),
    )
    topics = json.loads(env.get("EXECUTOR_THREADS_ID") or "{}")
    groups = json.loads(env.get("MASTER_GROUPS") or "{}")
    masters = {}
    for name in set(topics) | set(groups):
        row = conn.execute(
            "SELECT id FROM staff WHERE role = 'master' AND name = ? AND active = 1 ORDER BY (location_id = ?) DESC LIMIT 1",
            (name, location_id),
        ).fetchone()
        if row:
            conn.execute("UPDATE staff SET topic_id = ?, personal_chat_id = ? WHERE id = ?",
                         (topics.get(name), groups.get(name), row["id"]))
            masters[name] = row["id"]

    # «наши» заказы старого бота
    own = conn.execute(
        """UPDATE repair_orders SET own_order = 1 WHERE location_id = ? AND ext_source IN (?, ?)
           AND client_id IN (SELECT id FROM clients WHERE name = ?)""",
        (location_id, SOURCE, MOVED, OUR_CLIENT_NAME),
    ).rowcount

    # открытые — в обычные ремонты CRM
    open_rows = conn.execute(
        f"SELECT id FROM repair_orders WHERE location_id = ? AND ext_source = ? AND status IN ({','.join('?' * len(OPEN))})",
        (location_id, SOURCE, *OPEN),
    ).fetchall()
    for row in open_rows:
        conn.execute("UPDATE repair_orders SET ext_source = ? WHERE id = ?", (MOVED, row["id"]))
        # the sync wrote the bot's computed pay; from now on CRM counts it
        conn.execute(
            """UPDATE repair_orders SET manual_master_pay = NULL WHERE id = ? AND own_order = 0
               AND master_id IN (SELECT id FROM staff WHERE pay_type = 'percent')""",
            (row["id"],),
        )
        conn.execute("UPDATE repair_orders SET manual_master_pay = NULL WHERE id = ? AND manual_master_pay = 0", (row["id"],))

    # карточки старого бота — к ремонтам (тот же бот их правит)
    linked = 0
    for row in conn.execute(
        "SELECT id, ext_id FROM repair_orders WHERE location_id = ? AND ext_source IN (?, ?) AND ext_id IS NOT NULL",
        (location_id, SOURCE, MOVED),
    ).fetchall():
        if not str(row["ext_id"]).isdigit():
            continue
        exists = conn.execute(
            "SELECT 1 FROM repair_order_messages WHERE order_id = ? AND chat_id = ? AND message_id = ?",
            (row["id"], str(report_chat), int(row["ext_id"])),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind, has_photo) VALUES (?, ?, ?, 'topic', 1)",
                (row["id"], str(report_chat), int(row["ext_id"])),
            )
            linked += 1

    # History stays as the old bot left it in the sheet: marking it above put
    # it in the copy's queue (core.gsheet_mirror) — only open orders go.
    conn.execute(
        "DELETE FROM gsheet_queue WHERE repair_id IN (SELECT id FROM repair_orders WHERE location_id = ? AND ext_source = ?)",
        (location_id, SOURCE),
    )
    journal = import_journal(conn, users_db, bot_id) if users_db and bot_id else 0
    return {"masters": masters, "own": own, "open": len(open_rows), "cards_linked": linked, "journal": journal}


def import_journal(conn: sqlite3.Connection, users_db: str, bot_id: int) -> int:
    """The old bot's `users` log → tg_users of the same bot (it is the same bot)."""
    source = sqlite3.connect(users_db)
    source.row_factory = sqlite3.Row
    try:
        rows = source.execute("SELECT * FROM users").fetchall()
    finally:
        source.close()
    for row in rows:
        conn.execute(
            """INSERT INTO tg_users (bot_id, user_id, username, full_name, private_messages, group_messages, callbacks,
                                     last_chat, first_seen, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id, user_id) DO UPDATE SET
                 private_messages = private_messages + excluded.private_messages,
                 group_messages = group_messages + excluded.group_messages,
                 callbacks = callbacks + excluded.callbacks,
                 first_seen = MIN(first_seen, excluded.first_seen)""",
            (bot_id, row["user_id"], row["username"], row["full_name"], row["private_messages"], row["group_messages"],
             row["callbacks"], row["last_chat"], row["first_seen"], row["last_seen"]),
        )
    return len(rows)


def open_repairs(conn: sqlite3.Connection, location_id: int) -> list[int]:
    return [r["id"] for r in conn.execute(
        f"SELECT id FROM repair_orders WHERE location_id = ? AND ext_source = ? AND status IN ({','.join('?' * len(OPEN))}) ORDER BY id",
        (location_id, MOVED, *OPEN),
    )]


def find_by_card(conn: sqlite3.Connection, card_id) -> sqlite3.Row | None:
    """A repair by the number of its card in the old bot (callback data of
    the old cards carries it)."""
    return conn.execute(
        "SELECT id FROM repair_orders WHERE ext_source IN (?, ?) AND ext_id = ? ORDER BY id LIMIT 1",
        (SOURCE, MOVED, str(card_id)),
    ).fetchone()
