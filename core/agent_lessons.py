"""Уроки ИИ-помощника (22.10) — Павел: «бот должен самообучаться командам».

A lesson is one rule in plain words — «"добавь в наличные N" без клиента —
это корректировка кассы», «Эдик — это мастер Эдик Gybrid». It gets here
when a staff member corrects the assistant («не так», «я имел в виду…») or
says «бот, запомни: …» (core.agent_actions.remember_lesson — the model
writes the rule from the conversation), and from then on it stands in
the assistant's instructions on every request, in every chat
(core.ai_agent). «Бот, забудь урок 3» or «Вернуть» under the «Запомнил»
receipt switches it off; nothing is deleted.
"""
from __future__ import annotations

import sqlite3

MAX_RULE = 300
# The newest ones, if there ever are more — the prompt stays bounded.
IN_PROMPT = 40


def add(conn: sqlite3.Connection, rule: str, staff_id: int | None, example: str | None = None) -> int:
    """The id of the lesson — an existing one when the same rule is
    already active (a correction repeated is not a second lesson)."""
    rule = " ".join(str(rule or "").split())[:MAX_RULE]
    if len(rule) < 5:
        raise ValueError("правило пустое")
    # SQLite lower() knows only ASCII — Cyrillic is compared here.
    for row in conn.execute("SELECT id, rule FROM agent_lessons WHERE active = 1").fetchall():
        if row[1].casefold() == rule.casefold():
            return row[0]
    example = " ".join(str(example or "").split())[:MAX_RULE] or None
    cur = conn.execute("INSERT INTO agent_lessons (rule, example, staff_id) VALUES (?, ?, ?)", (rule, example, staff_id))
    return cur.lastrowid


def active(conn: sqlite3.Connection, limit: int = IN_PROMPT) -> list[sqlite3.Row]:
    """Active lessons, oldest first (the newest `limit` of them)."""
    rows = conn.execute(
        "SELECT * FROM agent_lessons WHERE active = 1 ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return list(reversed(rows))


def forget(conn: sqlite3.Connection, lesson_id: int) -> sqlite3.Row | None:
    """Switch a lesson off. The row as it was, or None if there's no such
    active lesson."""
    row = conn.execute("SELECT * FROM agent_lessons WHERE id = ? AND active = 1", (lesson_id,)).fetchone()
    if row:
        conn.execute("UPDATE agent_lessons SET active = 0, forgotten_at = datetime('now') WHERE id = ?", (lesson_id,))
    return row


def for_prompt(conn: sqlite3.Connection) -> str:
    """The block for the assistant's instructions; empty with no lessons."""
    rows = active(conn)
    if not rows:
        return ""
    lines = "\n".join(f"• №{row['id']}: {row['rule']}" for row in rows)
    return ("Уроки от сотрудников этой мастерской — так здесь принято; они важнее общих правил выше:\n" + lines)
