"""Ремонты точки «Гибрид сервис» из её собственного бота (11.10).

Магазин «Гибрид сервис» пока принимает и ведёт ремонты в своём старом
боте (@Gibrit_Repair_Bot, /opt/gibrit_bot, база orders.sqlite3). Чтобы CRM
видела эти ремонты и считала по ним выручку, прибыль и статистику
мастеров, они односторонне копируются сюда: бот — источник правды, CRM —
читатель. Каждый заказ бота = один repair_orders с ext_source='gibrit' и
ext_id = его card_id.

Чего здесь сознательно НЕТ:
  * кассы — деньги по этим ремонтам в CRM не проводятся (магазин кассу в
    CRM не ведёт), считается только выручка/прибыль документа;
  * начислений мастерам (master_accruals) — с мастерами там рассчитываются
    сами, долг «мы должны мастеру» из истории не возникает
    (core.repairs.settle пропускает ext_source);
  * карточек в Telegram — их шлёт сам бот Гибрида в свои группы.

Цифры берутся как в таблице бота: цена выдачи (E), закупка запчасти (F,
manual_parts_cost), выплата мастеру (K, manual_master_pay — введённая
руками либо (E−F)/3 для «Штат 33%»). Прибыль фирмы считает CRM по своей
формуле: цена − запчасти − выплата мастеру.

Правки такого ремонта, сделанные в самой CRM, перезапишутся при
следующем изменении заказа в боте.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from core import auth as _auth
from core import clients as _clients
from core import documents as _documents
from core import repairs as _repairs
from core.timefmt import KYIV

SOURCE = "gibrit"
LOCATION_NAME = "Гибрид сервис"
DEFAULT_SOURCE_DB = "/opt/gibrit_bot/orders.sqlite3"
# Заказы, залитые в базу бота из Google-таблицы при переносе (11.10.2026):
# когда их приняли на самом деле, бот не знает — created_at у них равен
# моменту заливки. Всё, что создано позже, несёт настоящее время.
HISTORY_IMPORTED_BEFORE = "2026-10-11 15:11:00"
OUR_CLIENT_NAME = "Наш (Гибрид сервис)"
NO_PHONE_NOTE = "Из бота «Гибрид сервис», без телефона"
STAFF_PERCENT = "Штат 33%"
OUTSOURCE = "Аутсорс"
# Меняется, когда меняется сама раскладка полей — тогда все ремонты
# пересобираются при следующем проходе.
_SIG_VERSION = "1"
_SIG_FIELDS = (
    "date", "client_name", "phone", "title", "issue_price", "purchase_price", "status", "master_pay",
    "master", "master_type", "comment", "device_model", "problem", "device_password",
)


class SyncError(Exception):
    """The sync can't run at all — message is shown as-is."""


def _col(order, name: str):
    try:
        return order[name]
    except (IndexError, KeyError):
        return None


def _num(value) -> float | None:
    text = str(value if value is not None else "").replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _money(value: float):
    value = round(value, 2)
    return int(value) if value == int(value) else value


def _utc(local: datetime) -> str:
    return local.replace(tzinfo=KYIV).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _parse_local(text: str | None) -> datetime | None:
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_day(text: str | None) -> datetime | None:
    """Колонка «Дата» бота: дд.мм.гггг — день, когда заказ закрыли."""
    try:
        return datetime.strptime((text or "").strip(), "%d.%m.%Y")
    except ValueError:
        return None


def _is_history(order) -> bool:
    return (order["created_at"] or "") < HISTORY_IMPORTED_BEFORE


def _closed_at(order) -> datetime | None:
    day = _parse_day(order["date"])
    if day is None:
        return None
    touched = _parse_local(order["updated_at"])
    # Закрыт уже при нас, в тот же день — у бота есть точное время.
    if touched and touched.date() == day.date() and (order["updated_at"] or "") >= HISTORY_IMPORTED_BEFORE:
        return touched
    return day + timedelta(hours=12)


def _estimate_created(orders: list) -> dict[int, datetime]:
    """Когда приняли заказ из истории. Бот этого не хранил, но card_id —
    номер сообщения в Telegram и растёт со временем: заказ принят не позже,
    чем закрыт любой заказ с бОльшим номером. Берём эту верхнюю границу."""
    estimate: dict[int, datetime] = {}
    bound = _parse_local(HISTORY_IMPORTED_BEFORE)
    for order in sorted(orders, key=lambda o: o["card_id"], reverse=True):
        closed = _closed_at(order)
        if closed and closed < bound:
            bound = closed
        if _is_history(order):
            estimate[order["card_id"]] = bound
    return estimate


def _signature(order) -> str:
    payload = [_SIG_VERSION] + [_col(order, field) for field in _SIG_FIELDS]
    return hashlib.sha1(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


def _status(order, has_master: bool) -> str:
    status = (order["status"] or "").strip().lower()
    if status == "готов":
        return "issued"
    if status == "возврат":
        return "cancelled"
    return "in_progress" if has_master else "new"


def _master_pay(order, price: float | None, parts: float | None) -> float:
    """Колонка K таблицы бота: введённая руками сумма, иначе формула
    =IF(M="Штат 33%"; (E−F)/3; FALSE). Не бывает отрицательной."""
    if order["master_pay"] is not None:
        return max(0.0, _num(order["master_pay"]) or 0.0)
    if (order["master_type"] or "").strip() == STAFF_PERCENT:
        return max(0.0, ((price or 0.0) - (parts or 0.0)) / 3)
    return 0.0


def _location_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM locations WHERE name = ?", (LOCATION_NAME,)).fetchone()
    if not row:
        raise SyncError(f"В CRM нет точки «{LOCATION_NAME}».")
    return row["id"]


def _client_id(conn: sqlite3.Connection, order) -> int:
    name = (order["client_name"] or "").strip() or "Без имени"
    phone = _clients.normalize_phone(order["phone"] or "")
    if phone:
        return _clients.get_or_create_by_phone(conn, name, phone)
    # Без телефона: «Наш» — собственные устройства магазина, один общий
    # клиент; остальные сводятся по имени среди таких же беcтелефонных.
    if name.lower() == "наш":
        name = OUR_CLIENT_NAME
    row = conn.execute(
        "SELECT id FROM clients WHERE name = ? AND phone IS NULL AND notes = ?", (name, NO_PHONE_NOTE)
    ).fetchone()
    return row["id"] if row else _clients.create_client(conn, name=name, notes=NO_PHONE_NOTE)


def _master_kinds(orders: list) -> dict[str, str]:
    """Штатный или аутсорс — по тому, как мастера чаще записывали в боте
    («Штат фикс» не в счёт: так помечены собственные устройства магазина,
    кто бы их ни чинил)."""
    votes: dict[str, int] = {}
    for order in orders:
        name, kind = (order["master"] or "").strip(), (order["master_type"] or "").strip()
        if name and kind in (OUTSOURCE, STAFF_PERCENT):
            votes[name] = votes.get(name, 0) + (1 if kind == OUTSOURCE else -1)
    return {name: "outsource" if score > 0 else "staff" for name, score in votes.items()}


def _master_id(conn: sqlite3.Connection, order, location_id: int, cache: dict, kinds: dict) -> int | None:
    name = (order["master"] or "").strip()
    if not name:
        return None
    if name not in cache:
        row = conn.execute(
            "SELECT id FROM staff WHERE role = 'master' AND name = ? ORDER BY (location_id = ?) DESC, id",
            (name, location_id),
        ).fetchone()
        if row:
            cache[name] = row["id"]
        else:
            kind = kinds.get(name, "staff")
            # Без ставки: выплата по каждому ремонту приходит из бота.
            cache[name] = _auth.create_master(conn, name, None, None, None, location_id=location_id, master_kind=kind)
    return cache[name]


def _target(order, created: dict[int, datetime]) -> dict:
    """Чем этот заказ бота должен быть в CRM (кроме client/master/status)."""
    price, parts = _num(order["issue_price"]), _num(order["purchase_price"])
    created_local = created.get(order["card_id"]) or _parse_local(order["created_at"]) or datetime.now(KYIV).replace(tzinfo=None)
    closed_local = _closed_at(order)
    if closed_local and created_local > closed_local:
        created_local = closed_local
    model = (_col(order, "device_model") or "").strip() or (order["title"] or "").strip() or "—"
    return {
        "price": None if price is None else int(round(price)),
        "manual_parts_cost": None if parts is None else _money(parts),
        "manual_master_pay": _money(_master_pay(order, price, parts)),
        "created_at": _utc(created_local),
        "closed_at": _utc(closed_local) if closed_local else None,
        "model": model,
        "defect": (_col(order, "problem") or "").strip() or None,
        "device_password": (_col(order, "device_password") or "").strip() or None,
        "intake_comment": (order["comment"] or "").strip() or None,
    }


def _write(conn: sqlite3.Connection, order, existing, location_id: int, created: dict, masters: dict, kinds: dict) -> None:
    target = _target(order, created)
    client_id = _client_id(conn, order)
    master_id = _master_id(conn, order, location_id, masters, kinds)
    status = _status(order, bool(master_id))
    issued = target["closed_at"] if status == "issued" else None
    # Выдан без даты закрытия в боте (так быть не должно, но таблицу правили
    # руками) — считаем выданным в момент приёма, а не «никогда».
    if status == "issued" and not issued:
        issued = target["created_at"]
    started = target["created_at"] if status != "new" else None
    values = {
        "client_id": client_id, "master_id": master_id, "status": status,
        "price_estimate": target["price"], "price_final": target["price"] if status == "issued" else None,
        "started_at": started, "completed_at": issued, "issued_at": issued,
        "no_parts": 1, "device_password": target["device_password"], "intake_comment": target["intake_comment"],
        "manual_parts_cost": target["manual_parts_cost"], "manual_master_pay": target["manual_master_pay"],
        "ext_sig": _signature(order),
    }
    if existing is None:
        device_id = conn.execute(
            "INSERT INTO devices (client_id, device_type, model, defect_description, created_at) VALUES (?, '', ?, ?, ?)",
            (client_id, target["model"], target["defect"], target["created_at"]),
        ).lastrowid
        columns = ["device_id", "channel", "location_id", "created_at", "ext_source", "ext_id", *values]
        params = [device_id, "offline", location_id, target["created_at"], SOURCE, order["card_id"], *values.values()]
        order_id = conn.execute(
            f"INSERT INTO repair_orders ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})", params,
        ).lastrowid
        conn.execute(
            "INSERT INTO repair_status_history (order_id, status, changed_by, comment, changed_at) "
            "VALUES (?, 'new', NULL, 'Принят (бот «Гибрид сервис»)', ?)",
            (order_id, target["created_at"]),
        )
        if status != "new":
            conn.execute(
                "INSERT INTO repair_status_history (order_id, status, changed_by, comment, changed_at) "
                "VALUES (?, ?, NULL, 'Из бота «Гибрид сервис»', ?)",
                (order_id, status, issued or target["created_at"]),
            )
        _documents.register(
            conn, "repair", staff_id=None, location_id=location_id, client_id=client_id,
            ref_table="repair_orders", ref_id=order_id, title=target["model"], amount=target["price"],
            created_at=target["created_at"],
        )
    else:
        order_id = existing["id"]
        conn.execute(
            f"UPDATE repair_orders SET {', '.join(f'{c} = ?' for c in values)} WHERE id = ?",
            [*values.values(), order_id],
        )
        conn.execute(
            "UPDATE devices SET client_id = ?, model = ?, defect_description = ? WHERE id = ?",
            (client_id, target["model"], target["defect"], existing["device_id"]),
        )
        conn.execute(
            "UPDATE documents SET client_id = ?, title = ?, amount = ? WHERE doc_type = 'repair' AND ref_id = ?",
            (client_id, target["model"], target["price"], order_id),
        )
        if existing["status"] != status:
            conn.execute(
                "INSERT INTO repair_status_history (order_id, status, changed_by, comment) "
                "VALUES (?, ?, NULL, 'Из бота «Гибрид сервис»')",
                (order_id, status),
            )
    # Прибыль документа — только у выданного (см. core.repairs.settle;
    # начисление мастеру для ext_source там пропускается).
    if status == "issued":
        _repairs.settle(conn, order_id)
    else:
        _repairs.unsettle(conn, order_id)


def read_source(source_db: str = DEFAULT_SOURCE_DB) -> list:
    """Все заказы бота. База открывается только на чтение."""
    try:
        src = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True, timeout=10)
        src.row_factory = sqlite3.Row
        try:
            return src.execute("SELECT * FROM orders ORDER BY card_id").fetchall()
        finally:
            src.close()
    except sqlite3.Error as exc:
        raise SyncError(f"Не читается база бота {source_db}: {exc}") from exc


def sync(conn: sqlite3.Connection, orders: list) -> dict:
    """Привести ремонты точки в соответствие с заказами бота. Идемпотентно:
    заказ, который с прошлого прохода не менялся, не трогается."""
    location_id = _location_id(conn)
    have = {
        row["ext_id"]: row for row in conn.execute(
            "SELECT id, ext_id, ext_sig, status, device_id FROM repair_orders WHERE ext_source = ?", (SOURCE,)
        )
    }
    created = _estimate_created(orders)
    kinds = _master_kinds(orders)
    masters: dict = {}
    result = {"total": len(orders), "created": 0, "updated": 0, "unchanged": 0}
    for order in orders:
        existing = have.get(order["card_id"])
        if existing is not None and existing["ext_sig"] == _signature(order):
            result["unchanged"] += 1
            continue
        _write(conn, order, existing, location_id, created, masters, kinds)
        result["updated" if existing is not None else "created"] += 1
    return result


def totals(conn: sqlite3.Connection) -> dict:
    """Итоги по перенесённым ремонтам — для сверки с таблицей бота."""
    row = conn.execute(
        """SELECT COUNT(*) AS n,
                  SUM(status = 'issued') AS issued, SUM(status = 'cancelled') AS cancelled,
                  SUM(status IN ('new', 'in_progress', 'ready')) AS open,
                  COALESCE(SUM(CASE WHEN status = 'issued' THEN price_final END), 0) AS revenue,
                  COALESCE(SUM(CASE WHEN status = 'issued' THEN manual_parts_cost END), 0) AS parts,
                  COALESCE(SUM(CASE WHEN status = 'issued' THEN manual_master_pay END), 0) AS master_pay
           FROM repair_orders WHERE ext_source = ?""",
        (SOURCE,),
    ).fetchone()
    profit = conn.execute(
        """SELECT COALESCE(SUM(documents.profit), 0) AS profit FROM repair_orders
           JOIN documents ON documents.doc_type = 'repair' AND documents.ref_id = repair_orders.id
           WHERE repair_orders.ext_source = ? AND repair_orders.status = 'issued'""",
        (SOURCE,),
    ).fetchone()["profit"]
    return {**{key: _money(row[key] or 0) for key in row.keys()}, "profit": _money(profit)}
