"""Копия ремонтов точки в Google-таблицу (23.10, этап 4 переноса «Гибрид
сервиса»). База — на сервере; таблица — фоновая копия, как было в старом
боте Гибрида с 11.10: ни один запрос пользователя не ждёт Google, а
недоступность Google только задерживает копию.

Включается ключом таблицы в «Кабинете магазина» (locations.gsheet_id).
Любое изменение ремонта такой точки ставит его в очередь (gsheet_queue —
триггеры SQLite в core.storage, так что не теряется ни один путь записи:
бот, приложение, синхронизация); бот раз в 20 секунд дописывает очередь
пачкой (bot.bot).

Строка листа — раскладка старого бота «Ремонты»:
  A Дата (день «Готов»/«Возврат»)   B Имя клиента   C Номер телефона
  D Название заказа (модель + неисправность)   E Цена для клиента
  F Цена запчасти   G card_id   H status (новый / Готов / Возврат)
  I Поставщик — не трогаем (вписывают руками)
  J Чистая прибыль — формула листа, не трогаем
  K Выплата мастеру — формула трети для «Штат 33%», иначе введённая сумма
  L Мастер   M Тип Мастера   N Комментарий   O card id_brend
G — номер РК; у заказов, пришедших из старого бота, — их прежний card_id,
чтобы найти уже существующую строку. «Возврат» показывает нули, как было.
"""
from __future__ import annotations

import logging
import os
import sqlite3

from core.accounts import money
from core.timefmt import kyiv_datetime

logger = logging.getLogger(__name__)

STATUS = {"new": "новый", "in_progress": "новый", "ready": "новый", "issued": "Готов", "cancelled": "Возврат"}
STAFF_PERCENT, OUTSOURCE, OWN = "Штат 33%", "Аутсорс", "Штат фикс"
K_FORMULA = '=IF(M{row}="Штат 33%"; J{row}/3; FALSE)'
# Written as typed into a cell by hand (numbers, the formula); the rest as plain text.
_ENTERED = ("E", "F", "K")
BATCH = 200


def row_key(repair) -> str:
    if (repair["ext_source"] or "").startswith("gibrit") and repair["ext_id"]:
        return str(repair["ext_id"])
    return str(repair["id"])


def _day(conn: sqlite3.Connection, repair) -> str:
    when = None
    if repair["status"] == "issued":
        when = repair["issued_at"]
    elif repair["status"] == "cancelled":
        row = conn.execute(
            "SELECT changed_at FROM repair_status_history WHERE order_id = ? AND status = 'cancelled' ORDER BY id DESC LIMIT 1",
            (repair["id"],),
        ).fetchone()
        when = row["changed_at"] if row else None
    return kyiv_datetime(when)[:10] if when else ""


def master_type(repair, master) -> str:
    if repair["own_order"]:
        return OWN
    if master is not None and master["pay_type"] == "percent":
        return STAFF_PERCENT
    return OUTSOURCE


def render(conn: sqlite3.Connection, repair_id: int) -> dict[str, object] | None:
    """column -> value for one repair; None if it isn't there to copy."""
    from core import repairs as _repairs

    repair = _repairs.get_repair(conn, repair_id)
    if repair is None:
        return None
    master = conn.execute("SELECT * FROM staff WHERE id = ?", (repair["master_id"],)).fetchone() if repair["master_id"] else None
    cancelled = repair["status"] == "cancelled"
    price = repair["price_final"] if repair["price_final"] is not None else repair["price_estimate"]
    title = " ".join(filter(None, [repair["brand"], repair["model"], repair["defect_description"]])).strip()
    return {
        "A": _day(conn, repair),
        "B": "Наш" if repair["own_order"] else (repair["client_name"] or ""),
        "C": "" if repair["own_order"] else (repair["client_phone"] or ""),
        "D": title,
        "E": 0 if cancelled else ("" if price is None else money(price)),
        "F": 0 if cancelled else ("" if repair["manual_parts_cost"] is None else money(repair["manual_parts_cost"])),
        "G": row_key(repair),
        "H": STATUS.get(repair["status"], "новый"),
        "K": 0 if cancelled else (K_FORMULA if repair["manual_master_pay"] is None else money(repair["manual_master_pay"])),
        "L": master["name"] if master else "",
        "M": master_type(repair, master),
        "N": repair["intake_comment"] or "",
        "O": "",
    }


def push(ws, rows: list[dict]) -> None:
    """Write rendered rows into the worksheet: an existing row is found by
    G (card_id); a new one takes the first row empty across A..I (the sheet
    keeps rows pre-filled with the J/K formulas below the data, as the old
    bot relied on)."""
    sheet = ws.get("A:I")
    by_id, empty = {}, []
    for n, cells in enumerate(sheet, start=1):
        key = str(cells[6]).strip() if len(cells) > 6 else ""
        if key:
            by_id.setdefault(key, n)
        elif n > 1 and all(str(c or "").strip() == "" for c in cells[:9]):
            empty.append(n)
    next_new = len(sheet) + 1
    empty.reverse()
    raw, entered = [], []
    for values in rows:
        r = by_id.get(values["G"])
        if r is None:
            r = empty.pop() if empty else next_new
            if r == next_new:
                next_new += 1
            by_id[values["G"]] = r
            raw.append({"range": f"I{r}", "values": [[""]]})
        for column, value in values.items():
            value = value.replace("{row}", str(r)) if isinstance(value, str) else value
            (entered if column in _ENTERED else raw).append({"range": f"{column}{r}", "values": [[value]]})
    last = max((int(item["range"].lstrip("ABCDEFGHIJKLMNO")) for item in raw + entered), default=0)
    if last > ws.row_count:
        ws.add_rows(last - ws.row_count + 100)
    if raw:
        ws.batch_update(raw, value_input_option="RAW")
    if entered:
        ws.batch_update(entered, value_input_option="USER_ENTERED")


def _open_worksheet(sheet_id: str):
    import gspread

    path = os.environ.get("GSHEET_CREDENTIALS")
    if not path:
        raise RuntimeError("GSHEET_CREDENTIALS не задан — ключ сервисного аккаунта Google")
    return gspread.service_account(filename=path).open_by_key(sheet_id).sheet1


def run_once(db_path: str | None = None, open_worksheet=None) -> int:
    """One pass over the queue, a sheet per точка. Returns how many repairs
    were copied. What fails stays queued for the next pass."""
    from core.storage import get_conn
    from core.stores import registry_db_path

    db_path = db_path or registry_db_path()
    open_worksheet = open_worksheet or _open_worksheet
    copied = 0
    with get_conn(db_path) as conn:
        points = conn.execute("SELECT id, gsheet_id FROM locations WHERE gsheet_id IS NOT NULL AND gsheet_id != ''").fetchall()
    for point in points:
        with get_conn(db_path) as conn:
            queued = conn.execute(
                """SELECT q.id, q.repair_id FROM gsheet_queue q JOIN repair_orders r ON r.id = q.repair_id
                   WHERE r.location_id = ? ORDER BY q.id LIMIT ?""",
                (point["id"], BATCH * 5),
            ).fetchall()
            if not queued:
                continue
            repair_ids = list(dict.fromkeys(row["repair_id"] for row in queued))[:BATCH]
            rows = [r for r in (render(conn, rid) for rid in repair_ids) if r]
        try:
            push(open_worksheet(point["gsheet_id"]), rows)
        except Exception:
            logger.warning("Google-таблица точки %s: копия отложена", point["id"], exc_info=True)
            continue
        done = [row["id"] for row in queued if row["repair_id"] in set(repair_ids)]
        with get_conn(db_path) as conn:
            conn.executemany("DELETE FROM gsheet_queue WHERE id = ?", [(i,) for i in done])
        copied += len(rows)
    return copied
