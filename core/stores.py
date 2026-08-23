"""Store registry for multi-store support (Фаза A, 23.08).

Each store is a fully isolated SQLite file — see OPERATIONS.md
"Мультимагазинность". The registry lives in stores.json, a sibling of .env
(deployed by hand on the server, never committed — stores.json.example is
the template). When stores.json is absent (dev machines, or before Фаза A
is rolled out on a given server), a single synthetic store is built from the
pre-existing CRM_DB_PATH/CRM_STAFF_GROUP_CHAT_ID/CRM_REPAIR_TOPIC_ID/
CRM_MASTERS_GROUP_CHAT_ID env vars — a single-store deployment keeps working
exactly as before, with zero config changes required.

The config path is re-read from CRM_STORES_CONFIG on every call (not cached
at import time) so tests can point different scenarios at different
stores.json files within the same process.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass

from core.storage import DB_PATH as _LEGACY_DB_PATH

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")


@dataclass(frozen=True)
class StoreConfig:
    id: str
    name: str
    db_path: str
    staff_group_chat_id: int | None = None
    repair_topic_id: int | None = None
    masters_group_chat_id: int | None = None


def _optional_int(value) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _stores_config_path() -> str:
    return os.environ.get("CRM_STORES_CONFIG", os.path.join(REPO_ROOT, "stores.json"))


def _legacy_single_store() -> list[StoreConfig]:
    return [
        StoreConfig(
            id="1",
            name="Магазин",
            db_path=_LEGACY_DB_PATH,
            staff_group_chat_id=_optional_int(os.environ.get("CRM_STAFF_GROUP_CHAT_ID")),
            repair_topic_id=_optional_int(os.environ.get("CRM_REPAIR_TOPIC_ID")),
            masters_group_chat_id=_optional_int(os.environ.get("CRM_MASTERS_GROUP_CHAT_ID")),
        )
    ]


def load_stores() -> list[StoreConfig]:
    path = _stores_config_path()
    if not os.path.exists(path):
        return _legacy_single_store()
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    stores = []
    for entry in raw:
        db_path = entry["db_path"]
        if not os.path.isabs(db_path):
            db_path = os.path.join(REPO_ROOT, db_path)
        stores.append(
            StoreConfig(
                id=str(entry["id"]),
                name=entry["name"],
                db_path=db_path,
                staff_group_chat_id=_optional_int(entry.get("staff_group_chat_id")),
                repair_topic_id=_optional_int(entry.get("repair_topic_id")),
                masters_group_chat_id=_optional_int(entry.get("masters_group_chat_id")),
            )
        )
    if not stores:
        raise RuntimeError(f"{path} defines no stores")
    return stores


def get_store(store_id: str) -> StoreConfig:
    for store in load_stores():
        if store.id == str(store_id):
            return store
    raise KeyError(f"Unknown store_id: {store_id!r}")


def default_store_id() -> str:
    return load_stores()[0].id


def store_for_chat_id(chat_id: int | str) -> StoreConfig | None:
    """Which store a Telegram group belongs to (Фаза C, 23.08) — a group
    message/button press unambiguously identifies its store this way,
    unlike a DM (see core.store_access.accessible_stores/pick_default_store
    for that case, which resolves by the sender's identity instead)."""
    try:
        chat_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    for store in load_stores():
        if store.staff_group_chat_id == chat_id or store.masters_group_chat_id == chat_id:
            return store
    return None
