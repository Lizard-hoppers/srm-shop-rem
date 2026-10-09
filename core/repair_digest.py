"""«Бот, сколько у нас ремонтов?» (09.10) — the list of repairs that are
still with us, as one chat message: device, price, status; выданные and
отменённые are left out. Each device name is a link to that repair's card
in the work chat, so one tap goes from the list to the card with its
buttons and notes.

Only builds text — who asked and where is bot/assistant_chat.py's
business.
"""
from __future__ import annotations

import html
import sqlite3

from core import documents as _documents
from core import repairs as _repairs
from core.accounts import money

# The order a reader wants: what can be handed over first, then what is
# being worked on, then what nobody has started.
OPEN_STATUSES = ("ready", "in_progress", "new")
_STATUS_ICON = {"ready": "✅", "in_progress": "🔧", "new": "🆕"}
# Telegram cuts a message at 4096 characters; leave room for the frame.
_CHUNK = 3500


def card_link(chat_id: str | int, message_id: int, topic_id: int | None = None) -> str | None:
    """A t.me link to a message in a supergroup (optionally inside a forum
    topic) — None for a chat that can't be linked to (a basic group, a
    private chat). Opens only for the group's members, which is the point."""
    chat = str(chat_id)
    if not chat.startswith("-100"):
        return None
    base = f"https://t.me/c/{chat[4:]}"
    return f"{base}/{topic_id}/{message_id}" if topic_id else f"{base}/{message_id}"


def _links(conn: sqlite3.Connection, order_ids: list[int], prefer_chat_id: str | None, topics: dict[str, int | None]) -> dict[int, str]:
    """order id -> link to its card: the one in the chat the question was
    asked in if there is one, else the staff group's topic, else any."""
    if not order_ids:
        return {}
    rows = conn.execute(
        f"""SELECT order_id, chat_id, message_id, kind FROM repair_order_messages
            WHERE order_id IN ({','.join('?' for _ in order_ids)}) ORDER BY id""",
        order_ids,
    ).fetchall()

    def rank(row) -> int:
        if prefer_chat_id is not None and row["chat_id"] == str(prefer_chat_id):
            return 0
        return 1 if row["kind"] == "topic" else 2

    best: dict[int, sqlite3.Row] = {}
    for row in rows:
        if row["order_id"] not in best or rank(row) < rank(best[row["order_id"]]):
            best[row["order_id"]] = row
    links = {}
    for order_id, row in best.items():
        topic = topics.get(row["chat_id"]) if row["kind"] == "topic" else None
        link = card_link(row["chat_id"], row["message_id"], topic)
        if link:
            links[order_id] = link
    return links


def _device(row: sqlite3.Row) -> str:
    return " ".join(filter(None, [row["brand"], row["model"]])) or row["device_type"] or "устройство"


def build(
    conn: sqlite3.Connection, location_id: int | None, *, statuses: tuple[str, ...] = OPEN_STATUSES,
    prefer_chat_id: str | int | None = None, topics: dict[str, int | None] | None = None,
) -> list[str]:
    """The answer as HTML message(s) — more than one only when the list
    doesn't fit a single Telegram message. `topics` maps a chat id to its
    «Ремонты» forum topic, for links into it."""
    statuses = tuple(s for s in OPEN_STATUSES if s in statuses) or OPEN_STATUSES
    repairs = [r for r in _repairs.list_repairs(conn, location_id=location_id) if r["status"] in statuses]
    if not repairs:
        scope = "" if statuses == OPEN_STATUSES else " с таким статусом"
        return [f"🔧 Невыданных ремонтов{scope} нет."]
    links = _links(conn, [r["id"] for r in repairs], str(prefer_chat_id) if prefer_chat_id is not None else None,
                   {str(k): v for k, v in (topics or {}).items()})
    total = money(sum(_repairs.current_price(r) or 0 for r in repairs))
    title = "Ремонтов не выдано" if statuses == OPEN_STATUSES else "Ремонтов"
    lines = [f"🔧 <b>{title}: {len(repairs)}</b> · на {total} грн"]
    for status in statuses:
        group = [r for r in repairs if r["status"] == status]
        if not group:
            continue
        lines.append("")
        lines.append(f"{_STATUS_ICON[status]} <b>{_repairs.STATUS_LABELS[status]} — {len(group)}</b>")
        for row in group:
            name = html.escape(_device(row))
            if row["id"] in links:
                name = f'<a href="{links[row["id"]]}">{name}</a>'
            price = _repairs.current_price(row)
            line = f"• {name} — {f'{price} грн' if price else 'цена не указана'} · {_documents.label('repair', row['id'])}"
            if row["stage_note"]:
                line += f" · <i>{html.escape(row['stage_note'])}</i>"
            lines.append(line)
    if links:
        lines += ["", "<i>Нажмите на название — откроется карточка ремонта в чате.</i>"]

    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > _CHUNK:
            chunks.append(current)
            current = ""
        current = f"{current}\n{line}" if current else line
    chunks.append(current)
    return chunks
