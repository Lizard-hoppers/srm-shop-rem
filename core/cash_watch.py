"""Каждое движение по кассе — владельцам в личку (25.10, Павел): приход,
расход, корректировка, обмен, выплата… и их отмена. Ловит триггер на
cash_transactions (core.storage) — значит, любой путь записи: бот, Mini App,
помощник. Бот (bot/bot.py) раз в несколько секунд рассылает накопившееся.

Кому — CRM_CASH_WATCHERS: «telegram_id:точки» через «;», точки — «*» (все)
или номера через запятую: «1417059280:*;455284316:1». Пишет бот точки, где
прошло движение; не вышло (человек его не открывал) — другой бот CRM."""
from __future__ import annotations

import html
import logging
import os
import sqlite3

import httpx

from core import bots as _bots
from core import documents as _documents
from core.accounts import balance, money
from core.storage import get_conn

logger = logging.getLogger(__name__)

BATCH = 30

# Откуда движение — по ref_type: (тип документа, что это).
_REFS = {
    "sales_order": ("sale", "Продажа"), "repair_order": ("repair", "Ремонт"), "service": ("service", "Услуга"),
    "buyback_order": ("buyback", "Покупка"), "exchange": ("exchange", "Обмен"),
}
_CATEGORIES = {
    "adjustment": "Корректировка", "other": "Прочее", "salary": "Зарплата", "rent": "Аренда", "buyback": "Покупка",
    "exchange": "Обмен", "transfer": "Перемещение денег", "purchase": "Закупка", "supplier": "Поставщику",
}


def watchers(raw: str | None = None) -> list[tuple[int, set[int] | None]]:
    """[(telegram_id, точки или None = все)] из CRM_CASH_WATCHERS."""
    found = []
    for part in (raw if raw is not None else os.environ.get("CRM_CASH_WATCHERS", "")).split(";"):
        user, _, points = part.strip().partition(":")
        if not user.strip().lstrip("-").isdigit():
            continue
        points = points.strip()
        found.append((int(user), None if points in ("", "*") else {int(p) for p in points.split(",") if p.strip().isdigit()}))
    return found


def _what(conn: sqlite3.Connection, tx: sqlite3.Row) -> str:
    """Что это было: документ кассы (РКО, корректировка), документ, за который
    пришли деньги (продажа, ремонт, услуга…), или хотя бы статья."""
    doc = conn.execute(
        "SELECT * FROM documents WHERE ref_table = 'cash_transactions' AND ref_id = ? ORDER BY id LIMIT 1", (tx["id"],),
    ).fetchone()
    if doc:
        what = f"{_documents.type_name(doc['doc_type'])} {_documents.doc_label(doc)}"
    elif tx["ref_type"] in _REFS:
        doc_type, name = _REFS[tx["ref_type"]]
        what = f"{name} {_documents.label(doc_type, tx['ref_id'])}" if tx["ref_id"] else name
    else:
        what = _CATEGORIES.get(tx["category"] or "", tx["category"] or "Движение")
    if (doc or tx["ref_type"] in _REFS) and tx["category"] in ("other", "salary", "rent"):
        what += f" · {_CATEGORIES[tx['category']]}"
    return html.escape(what)


def message(conn: sqlite3.Connection, tx_id: int, event: str) -> tuple[int, str] | None:
    """(точка, текст) про движение — или None, если его уже нет."""
    tx = conn.execute(
        """SELECT cash_transactions.*, money_accounts.name AS account_name, money_accounts.currency,
                  locations.name AS point, staff.name AS staff_name
           FROM cash_transactions
           LEFT JOIN money_accounts ON money_accounts.id = cash_transactions.account_id
           LEFT JOIN locations ON locations.id = cash_transactions.location_id
           LEFT JOIN staff ON staff.id = cash_transactions.staff_id
           WHERE cash_transactions.id = ?""",
        (tx_id,),
    ).fetchone()
    if not tx:
        return None
    unit = "грн" if (tx["currency"] or "UAH") == "UAH" else tx["currency"]
    amount = f"{'+' if tx['kind'] == 'income' else '−'}{money(tx['amount'])} {unit}"
    point = html.escape(tx["point"] or "—")
    account = html.escape(tx["account_name"] or tx["method"])
    if event == "cancel":
        head = f"↩️ <b>{point}</b> · отменено <s>{amount}</s> · {account}"
    else:
        head = f"{'💵' if tx['kind'] == 'income' else '💸'} <b>{point}</b> · <b>{amount}</b> · {account}"
    what = _what(conn, tx)
    comment = tx["comment"] or ""
    if tx["ref_type"] == "service" and comment.startswith("Услуга: "):
        comment = comment[len("Услуга: "):]  # «Услуга УС-001 · Плёнка», not «· Услуга: Плёнка»
    if comment:
        what += f" · {html.escape(comment[:120])}"
    tail = [f"👤 {html.escape(tx['staff_name'])}"] if tx["staff_name"] else []
    if tx["account_id"]:
        tail.append(f"остаток {money(balance(conn, tx['account_id']))} {unit}")
    return tx["location_id"], "\n".join([head, what] + ([" · ".join(tail)] if tail else []))


def _send(token: str, chat_id: int, text: str) -> str:
    """'ok', 'later' (Telegram просит подождать / не ответил) или 'no' (этим ботом не дойдёт)."""
    try:
        resp = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          data={"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=15)
    except httpx.HTTPError:
        return "later"
    if resp.status_code == 200:
        return "ok"
    if resp.status_code == 429 or resp.status_code >= 500:
        return "later"
    logger.info("cash watch to %s refused: %s", chat_id, resp.text[:200])
    return "no"


def deliver(location_id: int | None, text: str, chat_id: int) -> str:
    """Ботом точки, а если человек его не открывал — другим ботом CRM."""
    first = _bots.token_for_location(location_id)
    result = "no"
    for token in [first] + [t for t in _bots.all_tokens() if t != first]:
        if token:
            result = _send(token, chat_id, text)
            if result != "no":
                return result
    return result


def run_once(db_path: str | None = None, send=deliver) -> int:
    """Разослать накопившееся по порядку. Возвращает, сколько движений разослано."""
    people = watchers()
    with get_conn(db_path) as conn:
        if not people:
            conn.execute("DELETE FROM cash_watch_queue")
            return 0
        queue = conn.execute("SELECT * FROM cash_watch_queue ORDER BY id LIMIT ?", (BATCH,)).fetchall()
        jobs = [(row["id"], message(conn, row["tx_id"], row["event"])) for row in queue]
    done = 0
    for queue_id, built in jobs:
        if built:
            location_id, text = built
            for user, points in people:
                if (points is None or location_id in points) and send(location_id, text, user) == "later":
                    return done  # the rest goes out on the next tick, in order
        with get_conn(db_path) as conn:
            conn.execute("DELETE FROM cash_watch_queue WHERE id = ?", (queue_id,))
        done += 1
    return done
