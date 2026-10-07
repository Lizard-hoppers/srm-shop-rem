"""Помощник (Заход 7): reminds, never decides. Once a day — after
DIGEST_HOUR by Kyiv time, and only if there is something to fix — it posts
the точка's «Проблемы» (core.overview.problems: ремонты без указанной
запчасти, непринятые перемещения, истёкшие резервы, отчёты мастеров без
приёмки…) into that точка's staff group. It changes nothing in the base
but the date it last wrote.

Off by default; switched on per точка in «Кабинет магазина»
(locations.assistant_digest). The list itself is rule-based on purpose —
a reminder must say exactly what the base says. The language model's part
of the помощник is reading photos (накладные → черновик прихода, see
core.vision_ocr), where a human confirms every line before anything is
written.
"""
from __future__ import annotations

import html
import sqlite3
from datetime import datetime

from core import notify as _notify
from core import overview as _overview
from core.storage import get_conn
from core.timefmt import KYIV

DIGEST_HOUR = 10


def digest_text(conn: sqlite3.Connection, location: sqlite3.Row) -> str | None:
    """The message for one точка, or None when nothing is waiting."""
    lines = _overview.problems_text(conn, location["id"])
    if not lines:
        return None
    return "\n".join([
        f"🤖 <b>Помощник · {html.escape(location['name'])}</b>",
        "Ждёт вашего внимания:",
        *lines,
        "",
        "<i>Подробности — на Главной в приложении, блок «Проблемы».</i>",
    ])


def run_once(db_path: str | None = None, now: datetime | None = None) -> int:
    """Send today's digest wherever it is due and hasn't gone out yet.
    Returns how many were sent. A точка with nothing to report isn't
    marked — if a problem appears later the same day, it is told then."""
    now = now or datetime.now(KYIV)
    if now.hour < DIGEST_HOUR:
        return 0
    today = now.strftime("%Y-%m-%d")
    sent = 0
    with get_conn(db_path) as conn:
        due = conn.execute(
            """SELECT * FROM locations
               WHERE active = 1 AND assistant_digest = 1 AND staff_group_chat_id IS NOT NULL
                 AND (assistant_last_digest IS NULL OR assistant_last_digest != ?)""",
            (today,),
        ).fetchall()
        messages = [(location, digest_text(conn, location)) for location in due]
    for location, text in messages:
        if not text:
            continue
        if _notify.notify_staff_group(text, staff_group_chat_id=location["staff_group_chat_id"]) is None:
            continue  # Telegram didn't take it — try again on the next tick
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET assistant_last_digest = ? WHERE id = ?", (today, location["id"]))
        sent += 1
    return sent
