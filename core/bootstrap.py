"""One-off CLI to create the first staff account (owner).

Usage: python -m core.bootstrap <login> <password> <name>
An owner works across every точка (core.store_access), so there is no
точка to pick here; the old --store=<id> flag is still accepted and ignored.
"""
from __future__ import annotations

import sys

from core.auth import create_staff
from core.storage import get_conn, init_db
from core.stores import registry_db_path


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--store=")]
    if len(args) != 3:
        print("Usage: python -m core.bootstrap <login> <password> <name>")
        raise SystemExit(1)
    login, password, name = args
    db_path = registry_db_path()
    init_db(db_path)
    with get_conn(db_path) as conn:
        create_staff(conn, login=login, password=password, name=name, role="owner")
    print(f"Создан владелец: {login}")


if __name__ == "__main__":
    main()
