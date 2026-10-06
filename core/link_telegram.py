"""CLI to link a staff account to a Telegram user id (entry is by id only,
there is no self-service login — an admin runs this once per new staff member).

Usage: python -m core.link_telegram <login> <telegram_id>
Ask the staff member for their id via @userinfobot in Telegram. One base,
one staff row per person — the old --store=<id> flag is accepted and ignored.
"""
from __future__ import annotations

import sys

from core.auth import link_staff_telegram
from core.storage import get_conn
from core.stores import registry_db_path


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--store=")]
    if len(args) != 2:
        print("Usage: python -m core.link_telegram <login> <telegram_id>")
        raise SystemExit(1)
    login, telegram_id = args[0], int(args[1])
    with get_conn(registry_db_path()) as conn:
        ok = link_staff_telegram(conn, login, telegram_id)
    if ok:
        print(f"{login} привязан к Telegram ID {telegram_id}")
    else:
        print(f"Логин {login} не найден")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
