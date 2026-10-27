"""Группа стала супергруппой (27.10): Telegram даёт ей новый номер, когда
боту выдают права админа в обычной группе («Ремонты Олег»). Старый номер
везде, где он записан — личная группа мастера, группы точки, карточки, чаты
поставщиков, — меняется на новый: core.notify, получив от Telegram
migrate_to_chat_id, вызывает remap и повторяет отправку; бот делает то же
по служебному сообщению о переходе (bot/my_repairs.py)."""
from __future__ import annotations

import logging
import sqlite3

from core.storage import get_conn

logger = logging.getLogger(__name__)


def remap(old_chat_id: int | str, new_chat_id: int | str, db_path: str | None = None) -> int:
    """Every *chat_id column holding the old number gets the new one.
    Returns how many rows changed."""
    from core.stores import registry_db_path

    old, new = int(old_chat_id), int(new_chat_id)
    changed = 0
    with get_conn(db_path or registry_db_path()) as conn:
        tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for column in [row[1] for row in conn.execute(f"PRAGMA table_info({table})") if row[1].endswith("chat_id")]:
                for was, now in ((old, new), (str(old), str(new))):
                    try:
                        changed += conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (now, was)).rowcount
                    except sqlite3.IntegrityError:
                        logger.warning("chat %s -> %s: %s.%s already has the new number", old, new, table, column)
    logger.info("chat %s became %s: %d rows moved to the new number", old, new, changed)
    return changed
