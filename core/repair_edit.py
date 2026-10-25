"""Правка карточки ремонта словами (25.10, Павел): «бот, смени номер на
0501234567», «бот, имя Коля», «бот, модель iPhone 13» — ответом на карточку
в группе (bot/repair_attachments.card_command) или помощнику с номером РК
(core.agent_actions.edit_repair). Каждая правка пишется в repair_edits — что
было и что стало; по этой записи работает «↩️ Вернуть».

Номер телефона — это клиент: заказ переходит к клиенту с новым номером
(есть такой — к нему, нет — заводится), а прежний клиент остаётся как был —
у него могут быть другие заказы. Имя же правится у самого клиента."""
from __future__ import annotations

import html
import json
import re
import sqlite3

from core import clients as _clients
from core import documents as _documents

FIELD_LABELS = {
    "phone": "номер", "name": "имя", "model": "модель", "defect": "неисправность",
    "password": "пароль", "comment": "комментарий", "master": "мастер", "price": "цена",
}

_VERB = (r"(?:смени|сменить|смените|поменяй|поменять|поменяйте|измени|изменить|измените|исправь|исправить|исправьте"
         r"|замени|заменить|поставь|поставить|укажи|указать|запиши|записать|зміни|змінити|поміняй|виправ|виправити|заміни|постав|вкажи)")
_CLIENT = r"(?:\s+(?:клиента|клиенту|клієнта|клієнту))?"
_FIELDS = (
    ("phone", rf"(?:номер(?:\s+телефона)?|телефон|тел\.?|мобильный){_CLIENT}"),
    ("name", rf"(?:имя|ім['’]?я|фио){_CLIENT}"),
    ("model", r"(?:модель|устройство|аппарат|пристрій|девайс)"),
    ("defect", r"(?:неисправность|несправність|поломк[уаи]|проблем[уа]|дефект)"),
    ("password", r"(?:пароль|код(?:\s+блокировки)?)"),
    ("comment", r"(?:комментарий|коментар|коммент|комент|примечание)"),
    ("master", r"(?:мастер[аи]?|майстр[аи]?|майстер|исполнител[яь])"),
    ("price", r"(?:цен[ауы]|ціну|ціна|сумм?[ау]|стоимость|вартість)"),
)
_SAID = [
    (field, re.compile(rf"^\s*(?P<verb>{_VERB}\s+)?(?:в\s+карточке\s+)?{words}\s*(?:на\s+|:\s*|—\s*|-\s*|=\s*)?(?P<value>.+?)\s*[.!]?\s*$",
                       re.IGNORECASE | re.DOTALL))
    for field, words in _FIELDS
]


class EditError(Exception):
    """Править нечего или нельзя — текст для сотрудника."""


def parse(text: str | None, addressed: bool = True) -> tuple[str, str] | None:
    """(поле, значение), если сообщение — правка карточки, иначе None.
    addressed — сказано боту («бот, …»); без обращения (просто ответ на
    карточку) нужен глагол: «смени пароль 1234», а «пароль 1234» — заметка."""
    for field, pattern in _SAID:
        match = pattern.match(text or "")
        if not match or (not addressed and not match.group("verb")):
            continue
        value = match.group("value").strip()
        if re.match(r"^на\s+", value, re.IGNORECASE):
            value = value[3:].strip()
        if not value or (not match.group("verb") and "?" in value):
            continue  # «бот, проблема в шлейфе?» — вопрос, а не правка
        if field == "price":
            digits = re.sub(r"\s", "", re.sub(r"\s*(?:грн|гривен|гривень|₴)\.?$", "", value, flags=re.IGNORECASE))
            if not digits.isdigit():
                continue
        return field, value
    return None


def _shown(value) -> str:
    return html.escape(str(value)) if value not in (None, "") else "—"


def _repair(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    from core import repairs as _repairs

    repair = _repairs.get_repair(conn, order_id)
    if not repair:
        raise EditError("Ремонт не найден.")
    return repair


def find_master(conn: sqlite3.Connection, location_id: int, name: str) -> sqlite3.Row:
    from core import auth as _auth
    from core.agent_tools import matches

    masters = _auth.location_repairers(conn, location_id)
    found = [m for m in masters if matches(name, m["name"])]
    exact = [m for m in found if m["name"].casefold() == name.strip().casefold()]
    if len(exact) == 1 or len(found) == 1:
        return (exact or found)[0]
    names = ", ".join(m["name"] for m in (found or masters))
    raise EditError(f"Какой мастер? {'Подходят' if found else 'Есть'}: {names}.")


def apply(conn: sqlite3.Connection, order_id: int, field: str, value: str, staff_id: int | None) -> dict:
    """Записать правку. Возвращает {"id", "text", "old"} — id записи в
    repair_edits (для «Вернуть»), строку отчёта и прежнее значение."""
    repair = _repair(conn, order_id)
    value = " ".join(str(value).split()) if field not in ("defect", "comment") else str(value).strip()
    if field in ("phone", "name") and repair["own_order"]:
        raise EditError("это «Наш» ремонт — клиента у него нет.")
    if field == "phone":
        phone = _clients.normalize_phone(value)
        if not phone:
            raise EditError("не похоже на номер телефона. Например: 0501234567.")
        if phone == repair["client_phone"]:
            raise EditError(f"номер и так {html.escape(phone)}.")
        existing = _clients.get_by_phone(conn, phone)
        if existing:
            client_id = existing["id"]
        else:
            name = repair["client_name"] if repair["client_name"] and repair["client_name"] != repair["client_phone"] else phone
            client_id = _clients.create_client(conn, name=name, phone=phone, source="offline")
        conn.execute("UPDATE repair_orders SET client_id = ? WHERE id = ?", (client_id, order_id))
        conn.execute("UPDATE devices SET client_id = ? WHERE id = ?", (client_id, repair["device_id"]))
        old, new, stored = repair["client_phone"], phone, json.dumps({"client_id": repair["client_id"]})
    elif field == "name":
        if value == repair["client_name"]:
            raise EditError("имя и так такое.")
        conn.execute("UPDATE clients SET name = ? WHERE id = ?", (value, repair["client_id"]))
        old, new, stored = repair["client_name"], value, json.dumps({"client_id": repair["client_id"], "name": repair["client_name"]})
    elif field == "model":
        old = " ".join(filter(None, [repair["brand"], repair["model"]]))
        conn.execute("UPDATE devices SET brand = NULL, model = ? WHERE id = ?", (value, repair["device_id"]))
        new, stored = value, json.dumps({"brand": repair["brand"], "model": repair["model"]})
    elif field == "defect":
        conn.execute("UPDATE devices SET defect_description = ? WHERE id = ?", (value, repair["device_id"]))
        old, new, stored = repair["defect_description"], value, json.dumps({"value": repair["defect_description"]})
    elif field in ("password", "comment"):
        column = "device_password" if field == "password" else "intake_comment"
        new = None if value.casefold() in ("нет", "без пароля", "-", "—", "немає", "убрать", "убери") else value
        conn.execute(f"UPDATE repair_orders SET {column} = ? WHERE id = ?", (new, order_id))
        old, stored = repair[column], json.dumps({"value": repair[column]})
    elif field == "master":
        if repair["status"] in ("issued", "cancelled"):
            raise EditError("ремонт уже закрыт — мастера не меняю.")
        master = find_master(conn, repair["location_id"], value)
        if master["id"] == repair["master_id"]:
            raise EditError(f"мастер и так {html.escape(master['name'])}.")
        conn.execute("UPDATE repair_orders SET master_id = ? WHERE id = ?", (master["id"], order_id))
        old, new, stored = repair["master_name"], master["name"], json.dumps({"master_id": repair["master_id"]})
    else:
        raise EditError("это поле так не правится.")
    # Клиент и устройство — не repair_orders: пустое обновление будит триггер
    # копии в Google-таблицу (core.gsheet_mirror), как любая правка ремонта.
    conn.execute("UPDATE repair_orders SET id = id WHERE id = ?", (order_id,))
    edit_id = conn.execute(
        "INSERT INTO repair_edits (order_id, field, old_value, new_value, staff_id) VALUES (?, ?, ?, ?, ?)",
        (order_id, field, stored, new, staff_id),
    ).lastrowid
    label = _documents.label("repair", order_id)
    text = f"✅ <b>{label}</b>: {FIELD_LABELS[field]} {_shown(old)} → <b>{_shown(new)}</b>"
    return {"id": edit_id, "text": text, "old": old}


def undo(conn: sqlite3.Connection, edit_id: int) -> str:
    """«↩️ Вернуть» под правкой: прежнее значение — обратно."""
    row = conn.execute("SELECT * FROM repair_edits WHERE id = ?", (edit_id,)).fetchone()
    if not row:
        raise EditError("Правка не найдена.")
    if row["undone_at"]:
        raise EditError("Уже возвращено.")
    repair = _repair(conn, row["order_id"])
    old = json.loads(row["old_value"])
    field = row["field"]
    if field == "phone":
        conn.execute("UPDATE repair_orders SET client_id = ? WHERE id = ?", (old["client_id"], repair["id"]))
        conn.execute("UPDATE devices SET client_id = ? WHERE id = ?", (old["client_id"], repair["device_id"]))
    elif field == "name":
        conn.execute("UPDATE clients SET name = ? WHERE id = ?", (old["name"], old["client_id"]))
    elif field == "model":
        conn.execute("UPDATE devices SET brand = ?, model = ? WHERE id = ?", (old["brand"], old["model"], repair["device_id"]))
    elif field == "defect":
        conn.execute("UPDATE devices SET defect_description = ? WHERE id = ?", (old["value"], repair["device_id"]))
    elif field in ("password", "comment"):
        column = "device_password" if field == "password" else "intake_comment"
        conn.execute(f"UPDATE repair_orders SET {column} = ? WHERE id = ?", (old["value"], repair["id"]))
    elif field == "master":
        conn.execute("UPDATE repair_orders SET master_id = ? WHERE id = ?", (old["master_id"], repair["id"]))
    conn.execute("UPDATE repair_orders SET id = id WHERE id = ?", (repair["id"],))
    conn.execute("UPDATE repair_edits SET undone_at = datetime('now') WHERE id = ?", (edit_id,))
    return f"↩️ <b>{_documents.label('repair', repair['id'])}</b>: {FIELD_LABELS[field]} — как было."
