"""CLI to link a staff account to a Telegram user id (entry is by id only,
there is no self-service login — an admin runs this once per new staff member).

Usage: python -m core.link_telegram <login> <telegram_id> [--store=<id>]
Ask the staff member for their id via @userinfobot in Telegram.
Without --store, targets the default store (see core/stores.py).
"""
from __future__ import annotations

import sys

from core.auth import link_staff_telegram
from core.storage import get_conn
from core.stores import default_store_id, get_store


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--store=")]
    store_flags = [a for a in sys.argv[1:] if a.startswith("--store=")]
    if len(args) != 2:
        print("Usage: python -m core.link_telegram <login> <telegram_id> [--store=<id>]")
        raise SystemExit(1)
    login, telegram_id = args[0], int(args[1])
    store_id = store_flags[0].split("=", 1)[1] if store_flags else default_store_id()
    db_path = get_store(store_id).db_path
    with get_conn(db_path) as conn:
        ok = link_staff_telegram(conn, login, telegram_id)
    if ok:
        print(f"{login} привязан к Telegram ID {telegram_id} (магазин {store_id})")
    else:
        print(f"Логин {login} не найден в магазине {store_id}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
