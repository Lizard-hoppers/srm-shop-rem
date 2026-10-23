"""Простой режим ремонтов (23.10, перенос «Гибрид сервиса» из его старого
бота). Точка с locations.simple_flow = 1 работает так, как привыкла в
старом боте:

  * статусы «новый → ✅ Готов / ❌ Возврат», «🔄 Изменить выбор» возвращает
    в «новый». «Готов» — это «Выдан»: в этот момент начисляется мастеру
    (core.repairs.settle), «Возврат» — отказ, ничего не начисляется;
  * после выбора на карточке — «💰 Выдача / 🧾 Закупка / 💸 Выплата»,
    суммы вводятся числом прямо в группе. Выдача — цена клиенту, закупка —
    запчасть одной суммой (без склада), выплата — мастеру вручную. У
    штатного мастера с процентом выплата не вводится: считается от
    (выдача − закупка) по его ставке; у «нашего» заказа — вводится всегда;
  * «📦 Наш» — заказ на устройство самой точки: фото, модель, описание,
    мастер; клиент «Наш (<точка>)», без цены;
  * копия карточки — в тему мастера в группе точки и в личную группу
    мастера (там без данных клиента), со ссылкой на главную карточку;
  * этикетка 40×25 мм картинкой на печатающий аккаунт точки.

Текст карточки — обычный CRM-шный (core.repairs.render_card_text), от
старого бота взяты кнопки.
"""
from __future__ import annotations

import html
import io
import os
import sqlite3

from core import clients as _clients
from core import documents as _documents
from core.accounts import money

# What the buttons show, as in the old bot.
BTN_DONE, BTN_RETURN, BTN_CHANGE = "✅ Готов", "❌ Возврат", "🔄 Изменить выбор"
FIELDS = {
    "price": ("💰", "Выдача", "цену для клиента"),
    "parts": ("🧾", "Закупка", "закупку (запчасть)"),
    "pay": ("💸", "Выплата", "выплату мастеру"),
}
OPEN_STATUSES = ("new", "in_progress", "ready")


def is_simple(conn: sqlite3.Connection, location_id: int | None) -> bool:
    if not location_id:
        return False
    row = conn.execute("SELECT simple_flow FROM locations WHERE id = ?", (location_id,)).fetchone()
    return bool(row and row["simple_flow"])


def money_text(value) -> str:
    return "—" if value is None else str(money(value))


def keyboard_rows(order_id: int, status: str, numbers: dict | None) -> list[list[dict]]:
    """The old bot's buttons for a card in the simple mode."""
    if status in OPEN_STATUSES:
        return [[{"text": BTN_RETURN, "callback_data": f"gb_return:{order_id}"},
                 {"text": BTN_DONE, "callback_data": f"gb_done:{order_id}"}]]
    numbers = numbers or {}
    return [
        [{"text": BTN_CHANGE, "callback_data": f"gb_change:{order_id}"}],
        [{"text": f"💰 Выдача: {money_text(numbers.get('price'))}", "callback_data": f"gb_price:{order_id}"},
         {"text": f"🧾 Закупка: {money_text(numbers.get('parts'))}", "callback_data": f"gb_parts:{order_id}"}],
        [{"text": f"💸 Выплата: {money_text(numbers.get('pay'))}", "callback_data": f"gb_pay:{order_id}"}],
    ]


def card_numbers(conn: sqlite3.Connection, order_id: int) -> dict:
    """Выдача / закупка / выплата as the card shows them. A «Возврат» shows
    zeros, as in the old bot (nothing was sold, nothing is owed)."""
    from core import repairs as _repairs

    repair = _repairs.get_repair(conn, order_id)
    if repair["status"] == "cancelled":
        return {"price": 0, "parts": 0, "pay": 0}
    numbers = _repairs.finance(conn, order_id)
    return {"price": numbers["price"], "parts": numbers["parts_cost"], "pay": numbers["master_share"]}


def pay_is_by_rate(conn: sqlite3.Connection, order_id: int) -> bool:
    """A staff master on a percent: his pay is counted, not typed in —
    except on «наш» заказ, where it is always typed in (as in the old bot)."""
    row = conn.execute(
        """SELECT r.own_order, s.pay_type FROM repair_orders r LEFT JOIN staff s ON s.id = r.master_id
           WHERE r.id = ?""",
        (order_id,),
    ).fetchone()
    return bool(row and not row["own_order"] and row["pay_type"] == "percent")


def parse_amount(text: str):
    """A typed sum — 0 is allowed here («закупки не было»). None if not a number."""
    cleaned = str(text or "").strip().replace(" ", "").replace("\xa0", "").replace(",", ".")
    for suffix in ("грн", "₴", "uah"):
        if cleaned.lower().endswith(suffix):
            cleaned = cleaned[: -len(suffix)]
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value < 0 or value > 10_000_000 or value != value:
        return None
    return money(value)


def set_amount(conn: sqlite3.Connection, order_id: int, field: str, value, staff_id: int | None) -> None:
    """Write a sum and, if the repair is already «Готов», re-settle it so
    the master's accrual follows (core.repairs.settle redoes it from scratch)."""
    from core import repairs as _repairs

    column = {"price": "price_final", "parts": "manual_parts_cost", "pay": "manual_master_pay"}[field]
    conn.execute(f"UPDATE repair_orders SET {column} = ? WHERE id = ?", (value, order_id))
    label = FIELDS[field][1].lower()
    conn.execute(
        "INSERT INTO repair_status_history (order_id, status, changed_by, comment) "
        "SELECT id, status, ?, ? FROM repair_orders WHERE id = ?",
        (staff_id, f"{label}: {money(value)} грн", order_id),
    )
    if _repairs.get_repair(conn, order_id)["status"] == "issued":
        _repairs.settle(conn, order_id)


def move(conn: sqlite3.Connection, order_id: int, action: str, staff_id: int | None, who: str) -> str:
    """✅ Готов / ❌ Возврат / 🔄 Изменить выбор. Returns the new status."""
    from core import repairs as _repairs

    target = {"done": "issued", "return": "cancelled", "change": "new"}[action]
    comment = {"done": "Готов", "return": "Возврат", "change": "Изменить выбор"}[action] + f" (кнопка, {who})"
    if target == "issued":
        repair = _repairs.get_repair(conn, order_id)
        if repair["price_final"] is None and repair["price_estimate"] is not None:
            conn.execute("UPDATE repair_orders SET price_final = price_estimate WHERE id = ?", (order_id,))
    _repairs.update_status(conn, order_id, target, staff_id, comment)
    return target


def own_client_id(conn: sqlite3.Connection, location_id: int) -> int:
    """The «Наш (<точка>)» client of the точка's own devices — the one the
    Гибрид sync already made is reused."""
    name = conn.execute("SELECT name FROM locations WHERE id = ?", (location_id,)).fetchone()["name"]
    title = f"Наш ({name})"
    row = conn.execute("SELECT id FROM clients WHERE name = ? AND phone IS NULL ORDER BY id LIMIT 1", (title,)).fetchone()
    return row["id"] if row else _clients.create_client(conn, name=title, notes="Устройства самой точки («📦 Наш»)")


def create_own_order(
    conn: sqlite3.Connection, *, location_id: int, model: str, problem: str, master_id: int | None,
    staff_id: int, photo: tuple[bytes, str] | None, key: str | None = None,
) -> tuple[int, str, dict | None, tuple[bytes, str] | None]:
    """«📦 Наш» — same return shape as core.repairs.create_repair_intake."""
    from core import repairs as _repairs

    order_id = _repairs.create_repair(
        conn, own_client_id(conn, location_id), "", None, model, None, problem, "offline", master_id, None, staff_id,
        location_id=location_id, key=key,
    )
    conn.execute("UPDATE repair_orders SET own_order = 1 WHERE id = ?", (order_id,))
    photo_for_notify = None
    if photo:
        data, ext = photo
        repair = _repairs.get_repair(conn, order_id)
        filename = _repairs.write_device_photo(repair["device_id"], data, ext)
        _repairs.set_device_photo(conn, repair["device_id"], filename)
        photo_for_notify = (data, filename)
    text, keyboard = _repairs.card(conn, order_id)
    return order_id, text, keyboard, photo_for_notify


def card_link(chat_id, message_id) -> str:
    return f"https://t.me/c/{str(chat_id).removeprefix('-100')}/{message_id}"


def master_text(repair: sqlite3.Row) -> str:
    """The card for a master's own group — outsourcers sit there, so no
    client's name or phone: what to fix and how to get in."""
    label = _documents.label("repair", repair["id"])
    model = " ".join(filter(None, [repair["brand"], repair["model"]])) or repair["device_type"] or "—"
    lines = [f"🆔 <b>{label}</b>" + (" · 📦 Наш" if repair["own_order"] else ""), "", f"📱 {html.escape(model)}"]
    if repair["defect_description"]:
        lines.append(f"🔧 {html.escape(repair['defect_description'])}")
    if repair["device_password"]:
        lines.append(f"🔑 <b>{html.escape(repair['device_password'])}</b>")
    if repair["intake_comment"]:
        lines.append(f"📝 {html.escape(repair['intake_comment'])}")
    return "\n".join(lines)


_FONTS = os.path.join(os.path.dirname(__file__), "assets", "fonts")


def label_png(repair: sqlite3.Row, link: str | None) -> bytes:
    """Этикетка 40×25 мм при 203 dpi — раскладка старого бота: номер, дата,
    пароль, неисправность в две строки, модель, QR на карточку."""
    import qrcode
    from PIL import Image, ImageDraw, ImageFont

    from core.timefmt import kyiv_datetime

    img = Image.new("1", (320, 200), 1)
    draw = ImageDraw.Draw(img)
    big = ImageFont.truetype(os.path.join(_FONTS, "DejaVuSans-Bold.ttf"), 36)
    font = ImageFont.truetype(os.path.join(_FONTS, "DejaVuSans.ttf"), 20)
    model_font = ImageFont.truetype(os.path.join(_FONTS, "DejaVuSansCondensed-Bold.ttf"), 30)
    draw.text((40, 5), str(repair["id"]), font=big, fill=0)
    draw.text((20, 45), "   " + kyiv_datetime(repair["created_at"])[:10], font=font, fill=0)
    draw.text((10, 70), f"pin: {repair['device_password'] or ''}"[:16], font=font, fill=0)
    problem = repair["defect_description"] or ""
    draw.text((10, 95), problem[:12], font=font, fill=0)
    draw.text((10, 120), problem[12:25], font=font, fill=0)
    model = " ".join(filter(None, [repair["brand"], repair["model"]])) or ""
    draw.text((10, 145), model[:15], font=model_font, fill=0)
    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=6, border=1)
    qr.add_data(link or str(repair["id"]))
    qr.make(fit=True)
    img.paste(qr.make_image(fill_color="black", back_color="white").convert("1").resize((120, 120)), (180, 5))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def after_card_posted(store, order_id: int, sent: list, photo: tuple[bytes, str] | None) -> None:
    """Simple mode, right after the main card went out: copies to the
    master's topic and his own group (link to the main card), and the label.
    Best effort — a copy that fails never undoes the repair."""
    from core import notify as _notify
    from core import repairs as _repairs
    from core.storage import get_conn

    main = next(((chat, msg) for chat, msg, *_rest in sent if str(chat) == str(store.staff_group_chat_id)), None)
    link = card_link(*main) if main else None
    with get_conn(store.db_path) as conn:
        repair = _repairs.get_repair(conn, order_id)
        master = conn.execute("SELECT * FROM staff WHERE id = ?", (repair["master_id"],)).fetchone() if repair["master_id"] else None
        full_text, _keyboard = _repairs.card(conn, order_id)
        label_chat = conn.execute("SELECT label_chat_id FROM locations WHERE id = ?", (repair["location_id"],)).fetchone()["label_chat_id"]
    link_kb = {"inline_keyboard": [[{"text": "🔗 Показать карточку", "url": link}]]} if link else None
    if master and master["topic_id"] and store.staff_group_chat_id:
        _post(_notify, store.staff_group_chat_id, full_text, photo, link_kb, thread=master["topic_id"])
    if master and master["personal_chat_id"]:
        _post(_notify, master["personal_chat_id"], master_text(repair), photo, link_kb)
    if label_chat and not repair["own_order"]:
        _notify._send_photo(label_chat, label_png(repair, link), f"label_{order_id}.png",
                            caption=f"🖨 Этикетка {_documents.label('repair', order_id)}")


def _post(notify, chat_id, text, photo, keyboard, thread=None) -> None:
    if photo:
        notify._send_photo(chat_id, photo[0], photo[1], message_thread_id=thread, caption=text, reply_markup=keyboard)
    else:
        notify._send(chat_id, text, message_thread_id=thread, reply_markup=keyboard)
