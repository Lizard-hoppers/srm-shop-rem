"""Scenario tests (manifest p.9). Run against a throwaway SQLite file, never
against a live crm.sqlite3. Usage: python simulate_tests.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("CRM_SECRET_KEY", "test-secret-not-for-production")
# webapp.main's startup hook calls core.storage.init_db() with no override,
# so it always targets whatever CRM_DB_PATH resolved to at process start —
# point that at a throwaway file too, before core.storage is ever imported.
_WEBAPP_TEST_DB = os.path.join(tempfile.gettempdir(), "crm_simulate_tests_webapp.sqlite3")
os.environ.setdefault("CRM_DB_PATH", _WEBAPP_TEST_DB)

import hashlib
import hmac
import json
import time
import urllib.parse

import jinja2

from core import auth, clients, device_catalog, inventory, purchases, qr, repairs, sales, timefmt
from core.session_token import make_token, read_token
from core.storage import get_conn, init_db
from core.telegram_auth import validate_init_data
from fastapi.testclient import TestClient

PASS = 0
FAIL = 0


def check(label: str, condition: bool) -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label}")


def scenario_auth(db_path: str) -> None:
    print("scenario: staff auth")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "owner", "s3cr3t-pass", "Владелец", "owner")
        row = auth.get_staff_by_login(conn, "owner")
        check("staff created", row is not None and row["id"] == staff_id)
        check("correct password verifies", auth.verify_password("s3cr3t-pass", row["password_hash"]))
        check("wrong password rejected", not auth.verify_password("wrong", row["password_hash"]))

        linked = auth.link_staff_telegram(conn, "owner", 1417059280)
        check("telegram link succeeds for known login", linked)
        by_tg = auth.get_staff_by_telegram_id(conn, 1417059280)
        check("staff found by telegram_id", by_tg is not None and by_tg["id"] == staff_id)
        check("unknown telegram_id finds nobody", auth.get_staff_by_telegram_id(conn, 111) is None)
        check("linking unknown login fails", not auth.link_staff_telegram(conn, "nope", 42))


def scenario_client_and_repair(db_path: str) -> None:
    print("scenario: client -> device -> repair order")
    with get_conn(db_path) as conn:
        client_id = clients.create_client(conn, "Иван Иванов", phone="+380500000000", source="offline")
        client = clients.get_client(conn, client_id)
        check("client created", client is not None and client["name"] == "Иван Иванов")

        device_id = conn.execute(
            "INSERT INTO devices (client_id, device_type, brand, model, defect_description) "
            "VALUES (?, 'смартфон', 'Samsung', 'A54', 'не включается')",
            (client_id,),
        ).lastrowid
        order_id = conn.execute(
            "INSERT INTO repair_orders (device_id, client_id, status, channel) VALUES (?, ?, 'new', 'offline')",
            (device_id, client_id),
        ).lastrowid
        conn.execute(
            "INSERT INTO repair_status_history (order_id, status, comment) VALUES (?, 'new', 'принят на диагностику')",
            (order_id,),
        )
        devices = clients.get_client_devices(conn, client_id)
        check("device linked to client", len(devices) == 1 and devices[0]["id"] == device_id)

        history = conn.execute(
            "SELECT * FROM repair_status_history WHERE order_id = ?", (order_id,)
        ).fetchall()
        check("status history recorded", len(history) == 1)


def scenario_inventory(db_path: str) -> None:
    print("scenario: products, cells, stock movements")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "storekeeper1", "pass", "Кладовщик", "storekeeper")
        product_id = inventory.create_product(
            conn, "Дисплей Samsung A54", "SKU-A54-DSP", "Дисплеи", "шт", True, False, min_qty=2, price=None
        )
        cell_a = inventory.create_cell(conn, "A1-01", "Стеллаж A", None)
        cell_b = inventory.create_cell(conn, "A1-02", "Стеллаж A", None)

        inventory.receive_stock(conn, product_id, cell_a, 5, staff_id, comment="приход от поставщика")
        check("stock after receipt", inventory.product_total_qty(conn, product_id) == 5)

        inventory.transfer_stock(conn, product_id, cell_a, cell_b, 2, staff_id)
        by_cell = {r["cell_id"]: r["qty"] for r in inventory.product_stock_by_cell(conn, product_id)}
        check("transfer moved qty between cells", by_cell.get(cell_a) == 3 and by_cell.get(cell_b) == 2)

        inventory.write_off_stock(conn, product_id, cell_b, 1, staff_id, comment="брак")
        check("write-off reduces stock", inventory.product_total_qty(conn, product_id) == 4)

        low = inventory.low_stock_report(conn)
        check("low stock not triggered yet (4 > min 2)", all(r["id"] != product_id for r in low))

        raised = False
        try:
            inventory.write_off_stock(conn, product_id, cell_b, 999, staff_id, comment="перебор")
        except inventory.InsufficientStockError:
            raised = True
        check("overdraw raises InsufficientStockError", raised)

        movements = inventory.list_movements(conn)
        check("movements logged", len(movements) == 3)


def scenario_repairs_pipeline(db_path: str) -> None:
    print("scenario: repairs pipeline (intake -> assign -> parts -> status -> issued)")
    with get_conn(db_path) as conn:
        master_id = auth.create_staff(conn, "master1", "pass", "Мастер Олег", "master")
        staff_id = auth.create_staff(conn, "admin1", "pass", "Админ", "admin")
        client_id = clients.create_client(conn, "Пётр Петров", phone="+380671112233", source="offline")
        product_id = inventory.create_product(conn, "Батарея A54", "SKU-BAT", "Батареи", "шт", True, False, min_qty=1, price=None)
        cell_id = inventory.create_cell(conn, "B1-01", None, None)
        inventory.receive_stock(conn, product_id, cell_id, 3, staff_id)

        order_id = repairs.create_repair(
            conn, client_id, "смартфон", "Samsung", "A54", "IMEI123", "не держит заряд",
            "offline", None, 1500, staff_id,
        )
        repair = repairs.get_repair(conn, order_id)
        check("repair created with status new", repair["status"] == "new")

        repairs.assign_master(conn, order_id, master_id)
        repair = repairs.get_repair(conn, order_id)
        check("master assigned", repair["master_id"] == master_id)

        repairs.update_status(conn, order_id, "in_progress", staff_id)
        repair = repairs.get_repair(conn, order_id)
        check("status moved to in_progress and started_at stamped", repair["status"] == "in_progress" and repair["started_at"] is not None)

        inventory.record_movement(conn, product_id, 1, "repair_use", staff_id, from_cell_id=cell_id, ref_type="repair_order", ref_id=order_id)
        check("part usage deducted stock", inventory.product_total_qty(conn, product_id) == 2)
        parts = repairs.get_used_parts(conn, order_id)
        check("part usage recorded against repair", len(parts) == 1 and parts[0]["qty"] == 1)

        repairs.update_status(conn, order_id, "ready", staff_id)
        repairs.set_price(conn, order_id, price_estimate=1500, price_final=1400)
        repairs.update_status(conn, order_id, "issued", staff_id, comment="выдан клиенту")
        repair = repairs.get_repair(conn, order_id)
        check("repair issued with timestamp and final price", repair["status"] == "issued" and repair["issued_at"] is not None and repair["price_final"] == 1400)

        history = repairs.get_status_history(conn, order_id)
        check("status history has 4 entries", len(history) == 4)


def scenario_client_history(db_path: str) -> None:
    print("scenario: client card shows every device brought in, across separate visits")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "admin2", "pass", "Админ 2", "admin")
        product_id = inventory.create_product(conn, "Чехол", "SKU-CASE", "Аксессуары", "шт", False, True, min_qty=0, price=300)
        cell_id = inventory.create_cell(conn, "E1-01", None, None)
        inventory.receive_stock(conn, product_id, cell_id, 5, staff_id)

        # Same client (matched by phone), two different phones brought in on separate visits.
        client_id_1 = clients.get_or_create_by_phone(conn, "Ирина Коваль", "+380631112200", source="offline")
        order_1 = repairs.create_repair(
            conn, client_id_1, "смартфон", "Apple", "iPhone 12", None, "разбит экран",
            "offline", None, 2000, staff_id,
        )
        client_id_2 = clients.get_or_create_by_phone(conn, "Ирина Коваль", "+380631112200", source="offline")
        order_2 = repairs.create_repair(
            conn, client_id_2, "планшет", "Apple", "iPad", None, "не заряжается",
            "offline", None, 1200, staff_id,
        )
        check("second visit reused the same client (matched by phone)", client_id_1 == client_id_2)

        sale_id = sales.create_sale(conn, client_id_1, "offline", staff_id, [(product_id, 1, 300)])

        history = repairs.list_repairs_by_client(conn, client_id_1)
        check("client history has both repair visits", len(history) == 2)
        check("history includes the iPhone visit", any(h["id"] == order_1 for h in history))
        check("history includes the iPad visit", any(h["id"] == order_2 for h in history))

        sale_history = sales.list_sales_by_client(conn, client_id_1)
        check("client purchase history recorded", len(sale_history) == 1 and sale_history[0]["id"] == sale_id)


def scenario_client_qr(db_path: str) -> None:
    print("scenario: client loyalty QR codes (self-registration by phone, scan-to-find)")
    with get_conn(db_path) as conn:
        client_id = clients.create_client(conn, "Оксана", phone="+380675554433", source="offline")

        code = qr.client_code(client_id)
        check("code has the expected prefix", code == f"CRMCID:{client_id}")
        check("code round-trips back to the client id", qr.parse_client_code(code) == client_id)
        check("garbage text does not parse as a code", qr.parse_client_code("not a qr code") is None)
        check("bare digits without the prefix are rejected", qr.parse_client_code("42") is None)

        png = qr.generate_png(code)
        check("QR PNG has a real PNG header", png[:8] == b"\x89PNG\r\n\x1a\n")
        check("QR PNG is a plausible image size", len(png) > 200)

        # A bot contact-share sends the phone without a leading '+'; a
        # staff-typed "+380675554433" and a bot-shared "380675554433" must
        # resolve to the same client, not create a duplicate.
        same_client_id = clients.get_or_create_by_phone(conn, "Оксана", "380675554433", source="online")
        check("phone without leading + still matches the same client", same_client_id == client_id)

        clients.link_telegram(conn, client_id, 555000111)
        found = clients.get_by_telegram_id(conn, 555000111)
        check("client resolvable by telegram_id after bot registration", found is not None and found["id"] == client_id)
        check("unknown telegram_id finds no client", clients.get_by_telegram_id(conn, 999) is None)


def scenario_device_catalog(db_path: str) -> None:
    print("scenario: device catalog autocomplete (seeded + learns new entries)")
    with get_conn(db_path) as conn:
        types = device_catalog.list_device_types(conn)
        check("seed device types present", "Смартфон" in types and "Ноутбук" in types)
        brands = device_catalog.list_brands(conn)
        check("seed brands present", "Apple" in brands and "Samsung" in brands)
        all_rows = device_catalog.list_all(conn)
        check("seed has a substantial number of entries", len(all_rows) > 50)
        check("known combo present in seed", any(r["brand"] == "Apple" and r["model"] == "iPhone 13" for r in all_rows))

        before = len(device_catalog.list_all(conn))
        device_catalog.remember(conn, "Смартфон", "Nokia", "3310 Rebuild Edition")
        after = device_catalog.list_all(conn)
        check("remembering a new combo grows the catalog", len(after) == before + 1)
        check("the new combo is actually queryable", any(r["model"] == "3310 Rebuild Edition" for r in after))

        before2 = len(device_catalog.list_all(conn))
        device_catalog.remember(conn, "Смартфон", "Nokia", "3310 Rebuild Edition")
        check("remembering the same combo twice does not duplicate it", len(device_catalog.list_all(conn)) == before2)

        device_catalog.remember(conn, "", "Brand", "Model")
        device_catalog.remember(conn, "Тип", "", "Model")
        device_catalog.remember(conn, "Тип", "Brand", "")
        check("incomplete combos (blank type/brand/model) are silently ignored", len(device_catalog.list_all(conn)) == before2)


def scenario_purchases(db_path: str) -> None:
    print("scenario: goods receipt (приход)")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "storekeeper2", "pass", "Кладовщик 2", "storekeeper")
        supplier_id = purchases.create_supplier(conn, "ООО Компонент", "+380440000000")
        product_id = inventory.create_product(conn, "Шлейф зарядки", "SKU-CHG", "Шлейфы", "шт", True, False, min_qty=1, price=None)
        cell_id = inventory.create_cell(conn, "C1-01", None, None)

        receipt_id = purchases.create_receipt(
            conn, supplier_id, "INV-001", staff_id, [(product_id, cell_id, 10, 250)]
        )
        check("stock increased by received qty", inventory.product_total_qty(conn, product_id) == 10)
        items = purchases.get_receipt_items(conn, receipt_id)
        check("receipt item recorded with cost", len(items) == 1 and items[0]["unit_cost"] == 250)
        movements = [m for m in inventory.list_movements(conn) if m["ref_type"] == "goods_receipt"]
        check("receipt linked to a stock movement", len(movements) == 1 and movements[0]["ref_id"] == receipt_id)


def scenario_sales(db_path: str) -> None:
    print("scenario: offline sale deducts stock from a cell with enough qty")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "cashier1", "pass", "Кассир", "admin")
        product_id = inventory.create_product(conn, "Наушники", "SKU-EAR", "Аксессуары", "шт", False, True, min_qty=0, price=500)
        cell_a = inventory.create_cell(conn, "D1-01", None, None)
        cell_b = inventory.create_cell(conn, "D1-02", None, None)
        inventory.receive_stock(conn, product_id, cell_a, 2, staff_id)
        inventory.receive_stock(conn, product_id, cell_b, 5, staff_id)

        order_id = sales.create_sale(conn, None, "offline", staff_id, [(product_id, 4, 500)])
        check("sale deducted 4 units total", inventory.product_total_qty(conn, product_id) == 3)
        items = sales.get_sale_items(conn, order_id)
        check("sale item recorded", len(items) == 1 and items[0]["qty"] == 4)

        raised = False
        try:
            sales.create_sale(conn, None, "offline", staff_id, [(product_id, 999, 500)])
        except inventory.InsufficientStockError:
            raised = True
        check("sale beyond stock raises InsufficientStockError", raised)

        warranty_order_id = sales.create_sale(conn, None, "offline", staff_id, [(product_id, 1, 500)], warranty_until="2027-08-17")
        sale = sales.get_sale(conn, warranty_order_id)
        check("warranty_until is stored on the sale", sale["warranty_until"] == "2027-08-17")

        no_warranty_id = sales.create_sale(conn, None, "offline", staff_id, [(product_id, 1, 500)])
        sale2 = sales.get_sale(conn, no_warranty_id)
        check("warranty_until defaults to null when not given", sale2["warranty_until"] is None)


def _build_init_data(bot_token: str, user: dict, auth_date: int) -> str:
    data = {"auth_date": str(auth_date), "user": json.dumps(user, separators=(",", ":"))}
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    data["hash"] = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(data)


def scenario_timefmt() -> None:
    print("scenario: timestamps display as дд.мм.гггг in Kyiv time, not raw UTC")
    # August: Kyiv is UTC+3 (EEST/summer time) — 10:00 UTC is 13:00 Kyiv.
    check("UTC->Kyiv conversion during summer time (+3)", timefmt.kyiv_datetime("2026-08-17 10:00:00") == "17.08.2026 13:00")
    # January: Kyiv is UTC+2 (EET/winter time) — 22:30 UTC on the 31st rolls to 00:30 Kyiv on the 1st.
    check("UTC->Kyiv conversion during winter time (+2), crossing midnight", timefmt.kyiv_datetime("2026-01-31 22:30:00") == "01.02.2026 00:30")
    check("kyiv_datetime handles missing value", timefmt.kyiv_datetime(None) == "—")
    check("kyiv_datetime handles empty string", timefmt.kyiv_datetime("") == "—")
    check("kyiv_datetime passes through unparseable garbage instead of crashing", timefmt.kyiv_datetime("not-a-date") == "not-a-date")

    check("ru_date reformats a plain date (no timezone shift)", timefmt.ru_date("2026-12-31") == "31.12.2026")
    check("ru_date handles missing value", timefmt.ru_date(None) == "—")
    check("ru_date passes through unparseable garbage instead of crashing", timefmt.ru_date("garbage") == "garbage")


def scenario_telegram_auth() -> None:
    print("scenario: telegram mini app initData validation")
    bot_token = "123456:FAKE-TOKEN-FOR-TESTS"
    user = {"id": 999888777, "first_name": "Тест"}

    valid = _build_init_data(bot_token, user, int(time.time()))
    parsed = validate_init_data(valid, bot_token)
    check("valid initData accepted", parsed is not None and parsed["id"] == user["id"])

    wrong_secret = validate_init_data(valid, "other:token")
    check("wrong bot token rejected", wrong_secret is None)

    tampered = valid.replace("999888777", "999888778")
    check("tampered payload rejected", validate_init_data(tampered, bot_token) is None)

    stale = _build_init_data(bot_token, user, int(time.time()) - 999999)
    check("stale auth_date rejected", validate_init_data(stale, bot_token) is None)


def scenario_session_token() -> None:
    print("scenario: url-carried session token")
    token = make_token(42)
    check("token round-trips to the same staff_id", read_token(token) == 42)
    check("garbage token rejected", read_token("not-a-real-token") is None)
    check("empty token rejected", read_token("") is None)
    # Flip a character in the middle of the signature, not the last one: the
    # last base64 char can encode spare bits that decoding discards, so two
    # different last characters can occasionally decode to the same bytes.
    mid = len(token) // 2
    flipped = "a" if token[mid] != "a" else "b"
    tampered = token[:mid] + flipped + token[mid + 1:]
    check("tampered token rejected", read_token(tampered) is None)


def scenario_miniapp_boot_template() -> None:
    """Regression guard: an unrecognized/failed Telegram login used to
    re-render the same boot page whose script always auto-submits, so a
    stranger's browser hammered /miniapp/auto in an infinite loop (found in
    production 17.08.2026). The auto-submit form must only appear on a
    clean load, never alongside an error."""
    print("scenario: miniapp boot page never auto-resubmits on error")
    templates_dir = os.path.join(os.path.dirname(__file__), "webapp", "templates")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(templates_dir))
    template = env.get_template("miniapp.html")

    clean = template.render(error=None)
    check("clean load includes the auto-submit form", "autoForm" in clean)

    with_error = template.render(error="Этот Telegram-аккаунт не привязан к CRM.")
    check("error response has no auto-submit form (no resubmit loop)", "autoForm" not in with_error)
    check("error response shows the error message", "не привязан" in with_error)


def scenario_webapp_forms(db_path: str) -> None:
    """Real HTTP requests against the FastAPI app, not just core functions.
    Regression guard for 17.08.2026: several forms declared required fields
    as typed `int | None = Form(...)`/`str = Form(...)`; a browser that
    submits one of them blank or omitted made FastAPI raise a raw 422 JSON
    error instead of the app's normal Russian-language error page. Every
    "required" field on a user-facing form must degrade to a friendly
    re-rendered page, never a bare framework error."""
    print("scenario: web forms never leak a raw 422 to the user")
    if os.path.exists(db_path):
        os.remove(db_path)
    init_db(db_path)
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "webtest", "pass", "Веб Тест", "owner")
    token = make_token(staff_id)

    import webapp.main  # noqa: F401 -- import after CRM_DB_PATH is set for this test

    with TestClient(webapp.main.app) as client:
        # The exact bug: client_phone missing entirely from the POST body.
        resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Тест", "device_type": "Смартфон", "channel": "offline",
        })
        check("missing client_phone: no raw 422", resp.status_code != 422)
        check("missing client_phone: friendly error shown", "Заполните имя" in resp.text)

        # master_id/price_estimate submitted empty (exactly what an unset <select>/<input> sends).
        resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Реальный Клиент", "client_phone": "+380501234567",
            "device_type": "Ноутбук", "channel": "offline", "master_id": "", "price_estimate": "",
        })
        check("empty master_id/price_estimate: repair created (303)", resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))

        resp = client.post(f"/clients?t={token}", data={"name": "", "phone": "+380501111111"})
        check("missing client name: no raw 422", resp.status_code != 422)
        check("missing client name: friendly error shown", "Введите имя клиента" in resp.text)

        resp = client.post(f"/inventory/products?t={token}", data={"name": "", "price": ""})
        check("missing product name: no raw 422", resp.status_code != 422)
        check("missing product name: friendly error shown", "Введите название товара" in resp.text)

        resp = client.post(f"/inventory/products?t={token}", data={"name": "Тест товар", "price": ""})
        check("product with empty optional price: created (303)", resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))

        resp = client.post(f"/inventory/cells?t={token}", data={"code": ""})
        check("missing cell code: no raw 422", resp.status_code != 422)
        check("missing cell code: friendly error shown", "Введите код ячейки" in resp.text)

        resp = client.post(f"/inventory/movements/receive?t={token}", data={"product_id": "", "cell_id": "", "qty": ""})
        check("missing movement fields: no raw 422", resp.status_code != 422)

        resp = client.post(f"/purchases/suppliers?t={token}", data={"name": ""})
        check("missing supplier name: no raw 422", resp.status_code != 422)
        check("missing supplier name: friendly error shown", "Введите название поставщика" in resp.text)

        # Client loyalty QR: image endpoint + scan-to-find lookup.
        client_resp = client.post(f"/clients?t={token}", data={"name": "QR Клиент", "phone": "+380990001122"})
        # TestClient follows the 303 by default, so the final URL (not headers) has the new id.
        client_id = int(str(client_resp.url).split("/clients/")[1].split("?")[0])

        resp = client.get(f"/clients/{client_id}/qr.png?t={token}")
        check("qr.png returns 200", resp.status_code == 200)
        check("qr.png has image content-type", resp.headers.get("content-type", "").startswith("image/"))
        check("qr.png body is a real PNG", resp.content[:8] == b"\x89PNG\r\n\x1a\n")

        resp = client.get(f"/clients/find?t={token}&code=CRMCID:{client_id}")
        check("scanning a valid client code redirects to that client", f"/clients/{client_id}" in str(resp.url))

        resp = client.get(f"/clients/find?t={token}&code=garbage-not-a-code")
        check("scanning an unknown code: no raw error, friendly message", resp.status_code != 422 and "не распознан" in resp.text)

        # Device catalog autocomplete: seeded suggestions render, and a
        # brand-new device typed on intake gets remembered for next time.
        resp = client.get(f"/repairs?t={token}")
        check("repairs page renders the device type datalist", 'id="deviceTypeList"' in resp.text)
        check("repairs page embeds a known seeded brand", '"Apple"' in resp.text)

        client.post(f"/repairs?t={token}", data={
            "client_name": "Каталог Тест", "client_phone": "+380990002233",
            "device_type": "Экзотика", "brand": "НовыйБренд", "model": "СуперМодель X",
            "channel": "offline",
        })
        with get_conn(db_path) as conn:
            learned = device_catalog.list_all(conn)
        check("a brand-new device typed on intake is remembered in the catalog",
              any(r["brand"] == "НовыйБренд" and r["model"] == "СуперМодель X" for r in learned))

        # Sale warranty: staff picks the date themselves at checkout.
        client.post(f"/inventory/products?t={token}", data={"name": "Гарантийный товар", "sku": "WARR-1", "unit": "шт", "min_qty": "0"})
        client.post(f"/inventory/cells?t={token}", data={"code": "WARR-CELL"})
        with get_conn(db_path) as conn:
            wproduct_id = conn.execute("SELECT id FROM products WHERE sku = 'WARR-1'").fetchone()["id"]
            wcell_id = conn.execute("SELECT id FROM storage_cells WHERE code = 'WARR-CELL'").fetchone()["id"]
        client.post(f"/inventory/movements/receive?t={token}", data={"product_id": str(wproduct_id), "cell_id": str(wcell_id), "qty": "5"})

        sale_resp = client.post(f"/sales?t={token}", data={
            "channel": "offline", "warranty_until": "2027-08-17",
            "product_id_0": str(wproduct_id), "qty_0": "1", "price_0": "1000",
        })
        check("sale with warranty page shows the chosen date in дд.мм.гггг format", "17.08.2027" in sale_resp.text)


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.sqlite3")
        init_db(db_path)
        scenario_auth(db_path)
        scenario_client_and_repair(db_path)
        scenario_inventory(db_path)
        scenario_repairs_pipeline(db_path)
        scenario_client_history(db_path)
        scenario_client_qr(db_path)
        scenario_device_catalog(db_path)
        scenario_purchases(db_path)
        scenario_sales(db_path)

    scenario_timefmt()
    scenario_telegram_auth()
    scenario_session_token()
    scenario_miniapp_boot_template()
    scenario_webapp_forms(_WEBAPP_TEST_DB)
    if os.path.exists(_WEBAPP_TEST_DB):
        os.remove(_WEBAPP_TEST_DB)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
