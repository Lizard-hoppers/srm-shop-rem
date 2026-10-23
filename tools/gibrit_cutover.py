"""Переключение «Гибрид сервиса» на CRM (core.gibrit_cutover).

  python -m tools.gibrit_cutover apply --env /opt/gibrit_bot/.env --users /opt/gibrit_bot/orders.sqlite3
      настройки точки, мастера, открытые заказы, карточки, журнал — после
      последней синхронизации и остановки старого бота;
  python -m tools.gibrit_cutover convert-open
      перерисовать карточки открытых заказов в «РМТ» в карточки CRM — когда
      бот CRM уже работает с токеном Гибрида (CRM_POINT_BOTS).
"""
from __future__ import annotations

import argparse
import time

from core import bots, gibrit_cutover, notify, repairs
from core.gibrit_sync import LOCATION_NAME
from core.storage import get_conn, init_db
from core.stores import registry_db_path


def _location_id(conn) -> int:
    row = conn.execute("SELECT id FROM locations WHERE name = ?", (LOCATION_NAME,)).fetchone()
    if not row:
        raise SystemExit(f"Нет точки «{LOCATION_NAME}»")
    return row["id"]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["apply", "convert-open"])
    parser.add_argument("--env", default="/opt/gibrit_bot/.env")
    parser.add_argument("--users", default="/opt/gibrit_bot/orders.sqlite3")
    args = parser.parse_args()
    db_path = registry_db_path()
    init_db(db_path)
    if args.action == "apply":
        env = gibrit_cutover.read_env(args.env)
        with get_conn(db_path) as conn:
            result = gibrit_cutover.apply(conn, env, _location_id(conn), bots.bot_id(env.get("BOT_TOKEN")), args.users)
        print("готово:", result)
        return
    with get_conn(db_path) as conn:
        location_id = _location_id(conn)
        ids = gibrit_cutover.open_repairs(conn, location_id)
    done = 0
    for order_id in ids:
        with get_conn(db_path) as conn:
            text, keyboard = repairs.card(conn, order_id)
            messages = repairs.get_order_messages(conn, order_id)
        notify.sync_repair_cards(messages, text, keyboard)
        done += 1
        time.sleep(1.2)  # Telegram: не больше ~20 правок в минуту в одной группе
    print(f"перерисовано карточек: {done}")


if __name__ == "__main__":
    main()
