"""One-off CLI to create the first staff account (owner).

Usage: python -m core.bootstrap <login> <password> <name> [--store=<id>]
Without --store, targets the default store (first entry in stores.json, or
the single legacy store if stores.json doesn't exist — see core/stores.py).
"""
from __future__ import annotations

import sys

from core.auth import create_staff
from core.storage import get_conn, init_db
from core.stores import default_store_id, get_store


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--store=")]
    store_flags = [a for a in sys.argv[1:] if a.startswith("--store=")]
    if len(args) != 3:
        print("Usage: python -m core.bootstrap <login> <password> <name> [--store=<id>]")
        raise SystemExit(1)
    login, password, name = args
    store_id = store_flags[0].split("=", 1)[1] if store_flags else default_store_id()
    db_path = get_store(store_id).db_path
    init_db(db_path)
    with get_conn(db_path) as conn:
        create_staff(conn, login=login, password=password, name=name, role="owner")
    print(f"Создан владелец: {login} (магазин {store_id})")


if __name__ == "__main__":
    main()
