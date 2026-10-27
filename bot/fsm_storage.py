"""Шаги диалогов бота — в базе (27.10, Павел): перезапуск бота (выкатка)
больше не сбрасывает недоделанный приём ремонта, продажу, кассу — человек
продолжает с того же шага. 25.10 приём Никиты потерялся именно так.

В памяти, как прежде (aiogram MemoryStorage), и параллельно — в отдельном
файле SQLite рядом с базой CRM; после перезапуска диалог поднимается
оттуда. Данные — pickle: в шагах лежат фото устройства (байты) и кнопки
экрана (InlineKeyboardMarkup), JSON их не держит. То, что не сохраняется
(чужой объект с живым Bot внутри — грабли рассылки taki_vmeste), просто
остаётся в памяти, как до этого хранилища: диалог не падает. Брошенные
диалоги старше двух суток подчищаются при старте."""
from __future__ import annotations

import logging
import os
import pickle
import sqlite3
from typing import Any, Mapping

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

logger = logging.getLogger(__name__)

KEEP_DAYS = 2


def default_path() -> str:
    from core.stores import registry_db_path

    return os.environ.get("CRM_FSM_DB") or os.path.join(os.path.dirname(os.path.abspath(registry_db_path())), "fsm_state.sqlite3")


class PersistentStorage(BaseStorage):
    def __init__(self, path: str | None = None) -> None:
        self._mem: dict[str, tuple[str | None, dict]] = {}
        self._db = sqlite3.connect(path or default_path(), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS fsm (key TEXT PRIMARY KEY, state TEXT, data BLOB, updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        self._db.execute("DELETE FROM fsm WHERE updated_at < datetime('now', ?)", (f"-{KEEP_DAYS} days",))

    @staticmethod
    def _key(key: StorageKey) -> str:
        return ":".join(str(part) for part in (
            key.bot_id, key.chat_id, key.user_id, key.thread_id or "", key.business_connection_id or "", key.destiny,
        ))

    def _load(self, k: str) -> tuple[str | None, dict]:
        if k not in self._mem:
            row = self._db.execute("SELECT state, data FROM fsm WHERE key = ?", (k,)).fetchone()
            state, data = (row[0], row[1]) if row else (None, None)
            try:
                data = pickle.loads(data) if data else {}
            except Exception:
                logger.warning("fsm: saved dialog %s could not be read — starting it afresh", k, exc_info=True)
                state, data = None, {}
            self._mem[k] = (state, data)
        return self._mem[k]

    def _save(self, k: str) -> None:
        state, data = self._mem[k]
        if state is None and not data:
            self._mem.pop(k, None)
            self._db.execute("DELETE FROM fsm WHERE key = ?", (k,))
            return
        try:
            blob = pickle.dumps(data)
        except Exception:
            # Kept in memory only, as before this storage — a restart drops it.
            logger.warning("fsm: dialog %s not saved (data won't pickle)", k, exc_info=True)
            self._db.execute("DELETE FROM fsm WHERE key = ?", (k,))
            return
        self._db.execute(
            """INSERT INTO fsm (key, state, data, updated_at) VALUES (?, ?, ?, datetime('now'))
               ON CONFLICT(key) DO UPDATE SET state = excluded.state, data = excluded.data, updated_at = excluded.updated_at""",
            (k, state, blob),
        )

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        k = self._key(key)
        _old, data = self._load(k)
        self._mem[k] = (state.state if isinstance(state, State) else state, data)
        self._save(k)

    async def get_state(self, key: StorageKey) -> str | None:
        return self._load(self._key(key))[0]

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        k = self._key(key)
        state, _old = self._load(k)
        self._mem[k] = (state, dict(data))
        self._save(k)

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        return dict(self._load(self._key(key))[1])

    async def close(self) -> None:
        self._db.close()
