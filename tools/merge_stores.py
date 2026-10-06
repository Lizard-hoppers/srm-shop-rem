"""One-off migration (06.10.2026): fold the per-store SQLite files listed in
stores.json into the single base — «Единая база» in OPERATIONS.md.

The first store's file BECOMES the base (its data stays exactly where it
is); every other store turns into a row in `locations` carrying its name,
address, sales channel and Telegram group ids. This only works because the
other stores hold no business data yet — the script refuses to go on if one
of them does (clients, repairs, stock, money…), or has a staff member the
main base doesn't know, rather than quietly dropping it.

Usage (from the project root, with the app's venv):
    python tools/merge_stores.py            # dry run: prints what it would do
    python tools/merge_stores.py --apply    # does it

Safe to run twice: a точка that already exists is left alone. Back the
files up first anyway. Afterwards stores.json and the other stores' files
are no longer read by anything — move them out of the project directory.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_BUSINESS_TABLES = (
    "clients", "devices", "repair_orders", "products", "storage_cells", "stock_movements", "suppliers",
    "goods_receipts", "sales_orders", "buyback_orders", "cash_transactions", "purchase_drafts",
)


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(ROOT, path)


def _open(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def main() -> None:
    apply = "--apply" in sys.argv[1:]
    config_path = os.environ.get("CRM_STORES_CONFIG", os.path.join(ROOT, "stores.json"))
    if not os.path.exists(config_path):
        raise SystemExit(f"{config_path} не найден — объединять нечего.")
    with open(config_path, encoding="utf-8") as f:
        entries = json.load(f)
    if not entries:
        raise SystemExit("stores.json пуст.")

    main_entry, others = entries[0], entries[1:]
    main_db = _abs(main_entry["db_path"])
    os.environ["CRM_DB_PATH"] = main_db
    print(f"Основная база: {main_db} (точка {main_entry['id']})")

    plan = []
    with _open(main_db) as conn:
        known_telegram_ids = {
            r["telegram_id"] for r in conn.execute("SELECT telegram_id FROM staff WHERE telegram_id IS NOT NULL")
        }
    for entry in others:
        path = _abs(entry["db_path"])
        if not os.path.exists(path):
            print(f"  точка {entry['id']}: файла {path} нет — заведу пустую точку «{entry['name']}»")
            plan.append((entry, None))
            continue
        with _open(path) as conn:
            tables = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            non_empty = {
                t: n for t in _BUSINESS_TABLES if t in tables
                and (n := conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            }
            if non_empty:
                raise SystemExit(f"СТОП: в {path} есть данные {non_empty} — их надо переносить отдельно.")
            strangers = [
                dict(r) for r in conn.execute("SELECT login, name, role, telegram_id FROM staff WHERE active = 1")
                if r["telegram_id"] not in known_telegram_ids
            ]
            if strangers:
                raise SystemExit(f"СТОП: в {path} есть сотрудники, которых нет в основной базе: {strangers}")
            settings = conn.execute("SELECT * FROM store_settings WHERE id = 1").fetchone() if "store_settings" in tables else None
        name = settings["name"] if settings else entry["name"]
        print(f"  точка {entry['id']}: «{name}» из {path} (данных нет, сотрудники уже есть в основной базе)")
        plan.append((entry, dict(settings) if settings else None))

    if not apply:
        print("\nЭто был пробный прогон. Запустите с --apply, чтобы выполнить.")
        return

    from core.storage import ensure_warehouses, get_conn, init_db

    init_db(main_db)  # creates locations/warehouses/documents, seeds точка 1 from the old store_settings
    with get_conn(main_db) as conn:
        first_id = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()["id"]
        if str(first_id) != str(main_entry["id"]):
            raise SystemExit(f"СТОП: первая точка в базе имеет id {first_id}, а в stores.json — {main_entry['id']}.")
        conn.execute(
            """UPDATE locations SET staff_group_chat_id = ?, repair_topic_id = ?, masters_group_chat_id = ?,
                                    sales_topic_id = ? WHERE id = ?""",
            (main_entry.get("staff_group_chat_id"), main_entry.get("repair_topic_id"),
             main_entry.get("masters_group_chat_id"), main_entry.get("sales_topic_id"), first_id),
        )
        for entry, settings in plan:
            location_id = int(entry["id"])
            if conn.execute("SELECT 1 FROM locations WHERE id = ?", (location_id,)).fetchone():
                print(f"  точка {location_id} уже есть — пропускаю")
                continue
            settings = settings or {}
            conn.execute(
                """INSERT INTO locations (id, name, address, phone, working_hours, sales_channel,
                                          staff_group_chat_id, repair_topic_id, masters_group_chat_id, sales_topic_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (location_id, settings.get("name") or entry["name"], settings.get("address"), settings.get("phone"),
                 settings.get("working_hours"), settings.get("sales_channel"),
                 entry.get("staff_group_chat_id"), entry.get("repair_topic_id"),
                 entry.get("masters_group_chat_id"), entry.get("sales_topic_id")),
            )
        ensure_warehouses(conn)
        print("\nТочки в единой базе:")
        for row in conn.execute("SELECT id, name, staff_group_chat_id, masters_group_chat_id FROM locations ORDER BY id"):
            print("  ", dict(row))
        print("Документов в журнале:", conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    print("\nГотово. Уберите stores.json и файлы остальных магазинов из папки проекта.")


if __name__ == "__main__":
    main()
