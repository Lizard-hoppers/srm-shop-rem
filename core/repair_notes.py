"""Заметки по ремонту из рабочего чата (09.10): what staff write or say
in reply to a repair's card in the group stays with the repair instead of
scrolling away in the chat.

One row per message: the text exactly as it was written (or as the voice
message was transcribed) and a short version of it made by a language
model (core.ai_notes). The original is always kept — the short text is a
convenience for reading the card, never the record itself; if the model
was unavailable there simply is no short version and the original is
shown instead.
"""
from __future__ import annotations

import sqlite3

KINDS = ("text", "voice", "photo")


def add_note(
    conn: sqlite3.Connection, order_id: int, kind: str, original_text: str, summary: str | None, *,
    staff_id: int | None = None, author_name: str | None = None, chat_id: str | None = None,
    message_id: int | None = None, stage: str | None = None, suggested_status: str | None = None,
    suggested_price=None,
) -> int:
    """`stage` — where the repair stands according to this message, if it
    says: it becomes the repair's current «Стадия». `suggested_status` —
    the status the message points to, `suggested_price` — the new price
    it states; both only recorded here, a person confirms them."""
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид заметки: {kind}")
    stage = (stage or "").strip() or None
    note_id = conn.execute(
        """INSERT INTO repair_notes
           (order_id, kind, original_text, summary, staff_id, author_name, chat_id, message_id, stage,
            suggested_status, suggested_price)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (order_id, kind, original_text.strip(), (summary or "").strip() or None, staff_id,
         (author_name or "").strip() or None, chat_id, message_id, stage, suggested_status, suggested_price),
    ).lastrowid
    if stage:
        conn.execute("UPDATE repair_orders SET stage_note = ? WHERE id = ?", (stage, order_id))
    return note_id


def list_notes(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """Oldest first — it reads as the story of the repair. `text` is what
    to show: the short version, or the original when there is none;
    `author` — the staff member's name in the CRM, or their name in
    Telegram if they aren't linked to a staff card."""
    return conn.execute(
        """SELECT repair_notes.*, COALESCE(repair_notes.summary, repair_notes.original_text) AS text,
                  COALESCE(staff.name, repair_notes.author_name, '—') AS author
           FROM repair_notes LEFT JOIN staff ON staff.id = repair_notes.staff_id
           WHERE repair_notes.order_id = ? ORDER BY repair_notes.id""",
        (order_id,),
    ).fetchall()


def card_notes(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    """The notes the chat card shows — list_notes minus the ones taken
    off it by «убери заметки»."""
    return [note for note in list_notes(conn, order_id) if not note["hidden"]]


# repair_notes.hidden: 0 — on the card; 1 — taken off by «убери заметки»
# (comes back with «верни заметки»); 2 — one note removed on its own
# («удали» in reply to it) — that one stays off.
HIDDEN_ALL, REMOVED_ONE = 1, 2


def hide_from_card(conn: sqlite3.Connection, order_id: int) -> int:
    """«Убери заметки»: every note there is now comes off the repair's
    chat card (the block disappears). They stay on the repair's page in
    the Mini App, and a note written later shows on the card again.
    Returns how many were taken off."""
    return conn.execute("UPDATE repair_notes SET hidden = ? WHERE order_id = ? AND hidden = 0", (HIDDEN_ALL, order_id)).rowcount


def show_on_card(conn: sqlite3.Connection, order_id: int) -> int:
    """«Верни заметки»: what «убери заметки» took off the card is back on
    it. Notes removed one by one stay removed. Returns how many came back."""
    return conn.execute("UPDATE repair_notes SET hidden = 0 WHERE order_id = ? AND hidden = ?", (order_id, HIDDEN_ALL)).rowcount


def remove_by_message(conn: sqlite3.Connection, chat_id: str, message_id: int) -> sqlite3.Row | None:
    """«Удали» said in reply to the message a note was made from: that
    one note comes off the card for good (it is still on the repair's
    page — nothing said about a repair is deleted). Returns the note, or
    None if that message made no note."""
    note = conn.execute(
        "SELECT * FROM repair_notes WHERE chat_id = ? AND message_id = ? ORDER BY id LIMIT 1", (chat_id, message_id)
    ).fetchone()
    if note:
        conn.execute("UPDATE repair_notes SET hidden = ? WHERE id = ?", (REMOVED_ONE, note["id"]))
    return note


def find_order_by_note_message(conn: sqlite3.Connection, chat_id: str, message_id: int) -> int | None:
    """The repair a note belongs to, given the chat message it came as —
    so a reply to someone's reply still lands in the same repair."""
    row = conn.execute(
        "SELECT order_id FROM repair_notes WHERE chat_id = ? AND message_id = ? ORDER BY id LIMIT 1",
        (chat_id, message_id),
    ).fetchone()
    return row["order_id"] if row else None


def exists(conn: sqlite3.Connection, chat_id: str, message_id: int) -> bool:
    """Already recorded — Telegram redelivering the same message must not
    make a second note."""
    return find_order_by_note_message(conn, chat_id, message_id) is not None
