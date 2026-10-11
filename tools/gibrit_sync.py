"""Подтянуть ремонты бота «Гибрид сервис» в CRM (core.gibrit_sync).

    .venv/bin/python -m tools.gibrit_sync              # обычный проход (его раз в минуту зовёт таймер)
    .venv/bin/python -m tools.gibrit_sync --dry-run    # показать, что изменится, ничего не записав
    .venv/bin/python -m tools.gibrit_sync --totals     # итоги для сверки с таблицей бота
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import gibrit_sync, storage


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default=os.environ.get("GIBRIT_DB_PATH", gibrit_sync.DEFAULT_SOURCE_DB))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--totals", action="store_true")
    args = parser.parse_args()

    storage.init_db(storage.DB_PATH)
    try:
        orders = gibrit_sync.read_source(args.source)
        with storage.get_conn(storage.DB_PATH) as conn:
            result = gibrit_sync.sync(conn, orders)
            totals = gibrit_sync.totals(conn) if args.totals or args.dry_run else None
            if args.dry_run:
                conn.rollback()
    except gibrit_sync.SyncError as exc:
        print(f"gibrit_sync: {exc}", file=sys.stderr)
        return 1
    # Тихо, когда нечего делать: таймер зовёт это раз в минуту.
    if result["created"] or result["updated"] or args.dry_run or args.totals:
        print(f"gibrit_sync{' (dry-run)' if args.dry_run else ''}: всего {result['total']}, "
              f"новых {result['created']}, изменённых {result['updated']}, без изменений {result['unchanged']}")
    if totals:
        print("итоги:", ", ".join(f"{key}={value}" for key, value in totals.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
