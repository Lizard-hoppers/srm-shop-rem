"""CLI to link a staff account to a Telegram user id (entry is by id only,
there is no self-service login — an admin runs this once per new staff member).

Usage: python -m core.link_telegram <login> <telegram_id>
Ask the staff member for their id via @userinfobot in Telegram.
"""
from __future__ import annotations

import sys

from core.auth import link_staff_telegram
from core.storage import get_conn


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python -m core.link_telegram <login> <telegram_id>")
        raise SystemExit(1)
    login, telegram_id = sys.argv[1], int(sys.argv[2])
    with get_conn() as conn:
        ok = link_staff_telegram(conn, login, telegram_id)
    if ok:
        print(f"{login} привязан к Telegram ID {telegram_id}")
    else:
        print(f"Логин {login} не найден")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
