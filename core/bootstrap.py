"""One-off CLI to create the first staff account (owner).

Usage: python -m core.bootstrap <login> <password> <name>
"""
from __future__ import annotations

import sys

from core.auth import create_staff
from core.storage import get_conn, init_db


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python -m core.bootstrap <login> <password> <name>")
        raise SystemExit(1)
    login, password, name = sys.argv[1], sys.argv[2], sys.argv[3]
    init_db()
    with get_conn() as conn:
        create_staff(conn, login=login, password=password, name=name, role="owner")
    print(f"Создан владелец: {login}")


if __name__ == "__main__":
    main()
