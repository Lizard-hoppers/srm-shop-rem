"""Scenario tests (manifest p.9). Run against a throwaway SQLite file, never
against a live crm.sqlite3. Usage: python simulate_tests.py
"""
from __future__ import annotations

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))
os.environ.setdefault("CRM_SECRET_KEY", "test-secret-not-for-production")
# webapp.routers.miniapp reads CRM_BOT_TOKEN once at import time too — a
# fallback here lets scenario_multi_store_login_and_switch_http actually
# drive the real /miniapp/auto endpoint end-to-end (previously untested:
# no scenario ever exercised a successful Telegram login over HTTP, only
# core.telegram_auth.validate_init_data directly). setdefault(), not an
# unconditional overwrite: validate_init_data never calls the network (pure
# local HMAC check), so even if a sourced real .env leaves the genuine bot
# token in place there's no incident-19.08-style risk of a real Telegram
# call — the test just signs initData with whatever CRM_BOT_TOKEN actually
# resolved to (read back from webapp.routers.miniapp.BOT_TOKEN itself,
# never assumed) so it matches either way.
os.environ.setdefault("CRM_BOT_TOKEN", "123456:test-bot-token-not-real")
# webapp.main's startup hook calls core.storage.init_db() with no override,
# so it always targets whatever CRM_DB_PATH resolved to at process start —
# point that at a throwaway file too, before core.storage is ever imported.
#
# This MUST be an unconditional overwrite, not setdefault(): incident
# 19.08 — running this suite with the real .env sourced (to exercise
# PRINT_AGENT_TOKEN for real) left CRM_DB_PATH already set to the live
# crm.sqlite3 in the shell environment, setdefault() silently kept that
# value, and the whole scenario_webapp_forms HTTP suite ran straight
# against production — 6 fake repairs/devices/2 fake clients written for
# real, plus real Telegram cards sent to Работа/Мастера 007 (both
# cleaned up by hand afterward). The module docstring above says "never
# against a live crm.sqlite3" — setdefault() didn't actually guarantee
# that; this does.
_WEBAPP_TEST_DB = os.path.join(tempfile.gettempdir(), "crm_simulate_tests_webapp.sqlite3")
os.environ["CRM_DB_PATH"] = _WEBAPP_TEST_DB
# The точки registry is the `locations` table of whatever base CRM_DB_PATH
# names (core.stores.registry_db_path, re-read on every call) — so the
# overwrite above already keeps core.stores away from any real base too.
# Scenarios that need their own base with several точки switch CRM_DB_PATH
# for their duration through _separate_base() below and restore it after.

import hashlib
import hmac
import json
import re
import time
import urllib.parse

import httpx
import jinja2
from PIL import Image

from core import accounts, auth, barcode_label, buyback, cash, clients, device_catalog, doc_cancel, documents, inventory, locations, masters, money_ops, shifts, purchases, qr, repairs, sales, store_access, store_prefs, store_settings, stores, timefmt
from core import session_token as _session_token
from core import storage
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


from contextlib import contextmanager


@contextmanager
def _separate_base(tmp: str, filename: str, location_names: tuple[str, ...] = ("Магазин",)):
    """A fresh base of its own for one scenario, with the given точки
    (ids 1, 2, … in order) — the whole app (core.stores registry, the web
    middleware, session tokens) is pointed at it through CRM_DB_PATH until
    the block exits."""
    path = os.path.join(tmp, filename)
    prev = os.environ["CRM_DB_PATH"]
    os.environ["CRM_DB_PATH"] = path
    try:
        init_db(path)
        with get_conn(path) as conn:
            store_settings.update_settings(conn, location_names[0], None, None, None)
            for name in location_names[1:]:
                locations.create_location(conn, name)
        yield path
    finally:
        os.environ["CRM_DB_PATH"] = prev


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

        inventory.update_product(
            conn, product_id, "Дисплей Samsung A54 OLED", "SKU-A54-DSP", "Дисплеи", "шт", True, False, 3, 1500,
        )
        updated = inventory.get_product(conn, product_id)
        check("update_product changes the stored fields",
              updated["name"] == "Дисплей Samsung A54 OLED" and updated["min_qty"] == 3 and updated["price"] == 1500)

        check("a fresh product has no photo", inventory.get_product(conn, product_id)["photo_path"] is None)
        inventory.set_product_photo(conn, product_id, "42_abc123.jpg")
        check("set_product_photo stores the filename", inventory.get_product(conn, product_id)["photo_path"] == "42_abc123.jpg")
        inventory.set_product_photo(conn, product_id, None)
        check("set_product_photo(None) clears it", inventory.get_product(conn, product_id)["photo_path"] is None)

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

        check("a fresh device has no photo", repair["device_photo_path"] is None)
        repairs.set_device_photo(conn, repair["device_id"], "1_abc123.jpg")
        check("set_device_photo stores the filename", repairs.get_repair(conn, order_id)["device_photo_path"] == "1_abc123.jpg")
        repairs.set_device_photo(conn, repair["device_id"], None)
        check("set_device_photo(None) clears it", repairs.get_repair(conn, order_id)["device_photo_path"] is None)

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

        # Product barcodes (19.08) — a real Code128 of the product's own
        # SKU, not an app-invented id (Павел wanted scanning to match a
        # part's existing barcode digits), so lookup is by exact SKU via
        # core.inventory.get_product_by_sku(), not a parsed prefix.
        product_id = inventory.create_product(conn, "Экран iPhone 12", "SKU-SCR12", "Экраны", "шт", True, False, min_qty=1, price=None)
        found = inventory.get_product_by_sku(conn, "SKU-SCR12")
        check("get_product_by_sku finds the product by its exact SKU", found is not None and found["id"] == product_id)
        check("get_product_by_sku is exact, not a substring match", inventory.get_product_by_sku(conn, "SKU-SCR") is None)
        check("get_product_by_sku returns None for an unknown SKU", inventory.get_product_by_sku(conn, "no-such-sku") is None)
        check("get_product_by_sku returns None for an empty string", inventory.get_product_by_sku(conn, "") is None)

        label_png = barcode_label.generate_label_png("2716140063024", "Дисплей Xiaomi Redmi 9A/9AT/9C", 350)
        check("barcode label PNG has a real PNG header", label_png[:8] == b"\x89PNG\r\n\x1a\n")
        check("barcode label PNG is a plausible image size", len(label_png) > 2000)

        no_price_label_png = barcode_label.generate_label_png("SKU-SCR12", "Экран iPhone 12", None)
        check("barcode label renders fine with no price set", no_price_label_png[:8] == b"\x89PNG\r\n\x1a\n")

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


def scenario_supplier_returns(db_path: str) -> None:
    print("scenario: same part from multiple suppliers + return a defective batch")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "storekeeper3", "pass", "Кладовщик 3", "storekeeper")
        supplier_a = purchases.create_supplier(conn, "Поставщик А", None)
        supplier_b = purchases.create_supplier(conn, "Поставщик Б", None)
        product_id = inventory.create_product(conn, "Экран Xiaomi Redmi 9A", "SKU-SCR-9A", "Экраны", "шт", True, False, min_qty=1, price=None)
        cell_id = inventory.create_cell(conn, "C1-02", None, None)

        # Same product, same cell, two different suppliers — the mixed-in-
        # one-cell reality Павел confirmed (21.08).
        receipt_a = purchases.create_receipt(conn, supplier_a, "A-100", staff_id, [(product_id, cell_id, 5, 400)])
        receipt_b = purchases.create_receipt(conn, supplier_b, "B-200", staff_id, [(product_id, cell_id, 5, 420)])
        check("stock from both suppliers lands in the same cell, summed", inventory.product_total_qty(conn, product_id) == 10)

        history = purchases.list_receipts_for_product(conn, product_id)
        check("purchase history shows both suppliers' deliveries, newest first",
              len(history) == 2 and history[0]["receipt_id"] == receipt_b and history[1]["receipt_id"] == receipt_a)

        # A defect is found among what turned out to be supplier B's batch.
        return_id = purchases.create_supplier_return(
            conn, product_id, supplier_b, receipt_b, cell_id, 2, "Треснул экран прямо из коробки", staff_id,
        )
        check("stock dropped by exactly the returned qty", inventory.product_total_qty(conn, product_id) == 8)

        returns = purchases.list_supplier_returns(conn, product_id=product_id)
        check("the return is logged against supplier B specifically, not supplier A",
              len(returns) == 1 and returns[0]["supplier_name"] == "Поставщик Б" and returns[0]["qty"] == 2)

        movement = [m for m in inventory.list_movements(conn) if m["ref_type"] == "supplier_return"][0]
        check("the return's stock movement is linked back to the supplier_returns row",
              movement["ref_id"] == return_id and movement["reason"] == "adjustment")
        check("the movement comment carries the defect reason",
              "Треснул экран" in movement["comment"])

        raised = False
        try:
            purchases.create_supplier_return(conn, product_id, supplier_a, receipt_a, cell_id, 999, "тест", staff_id)
        except inventory.InsufficientStockError:
            raised = True
        check("returning more than is on the shelf raises InsufficientStockError, not a silent overdraw", raised)

        # A return with no specific receipt remembered — supplier only.
        purchases.create_supplier_return(conn, product_id, supplier_a, None, cell_id, 1, None, staff_id)
        check("a return can be logged with no receipt_id (supplier known, delivery not)",
              any(r["receipt_id"] is None for r in purchases.list_supplier_returns(conn, product_id=product_id)))


def scenario_purchase_import(db_path: str) -> None:
    print("scenario: parse pasted invoice text into draft receipt rows")
    from core import purchase_import

    with get_conn(db_path) as conn:
        # "Шлейф зарядки" / SKU-CHG already exists, created by scenario_purchases.
        text = "SKU-CHG\t5\t300\nШлейф зарядки;3;280\nНеизвестный товар   2   40"
        rows = purchase_import.parse_invoice_text(conn, text)
        blank_lines_skipped = purchase_import.parse_invoice_text(conn, "\n\nSKU-CHG\t1\t100\n\n")

    check("parse_invoice_text returns one row per non-empty line", len(rows) == 3)
    check("tab-separated line matches by exact SKU",
          rows[0]["product_id"] is not None and rows[0]["qty"] == 5 and rows[0]["unit_cost"] == 300)
    check("semicolon-separated line matches the same product by exact name",
          rows[1]["product_id"] == rows[0]["product_id"] and rows[1]["qty"] == 3 and rows[1]["unit_cost"] == 280)
    check("multi-space-separated line with no catalog match comes back product_id=None",
          rows[2]["product_id"] is None and rows[2]["name_guess"] == "Неизвестный товар" and rows[2]["qty"] == 2)
    check("blank lines in the pasted text are skipped", len(blank_lines_skipped) == 1)


def scenario_purchase_drafts_and_vision(db_path: str) -> None:
    print("scenario: photo-of-invoice drafts + vision OCR error handling")
    from core import purchase_import, vision_ocr

    with get_conn(db_path) as conn:
        matched = purchase_import.match_items(conn, [
            {"name": "SKU-CHG", "qty": 4, "unit_cost": 260},
            {"name": "Совсем незнакомый товар", "qty": 1, "unit_cost": None},
        ])
        check("match_items resolves a known SKU from a structured item", matched[0]["product_id"] is not None)
        check("match_items leaves an unknown item unresolved", matched[1]["product_id"] is None)

        draft_id = purchases.create_draft(conn, 1, matched)
        draft = purchases.get_draft(conn, draft_id)
        check("create_draft stores a pending draft", draft is not None and draft["status"] == "pending")
        check("get_draft_items round-trips the matched items", purchases.get_draft_items(conn, draft_id) == matched)

        purchases.mark_draft_applied(conn, draft_id)
        check("mark_draft_applied flips status to applied", purchases.get_draft(conn, draft_id)["status"] == "applied")

    # vision_ocr must never silently return an empty/garbage result — every
    # failure mode raises VisionOcrError for the bot handler to catch and
    # tell staff to retry/enter manually, rather than acting on nothing.
    raised_no_key = False
    try:
        vision_ocr.extract_invoice_items(b"fake-bytes")
    except vision_ocr.VisionOcrError:
        raised_no_key = True
    check("extract_invoice_items raises without an API key configured", raised_no_key)

    class _OkResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"choices": [{"message": {"content":
                '{"items": [{"name": "Кабель", "qty": 2, "unit_cost": 90}, {"name": "  "}]}'
            }}]}

    class _BadShapeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"choices": [{"message": {"content": "не json"}}]}

    class _ErrorResponse:
        status_code = 500
        text = "server error"

    orig_post = httpx.post
    orig_key = vision_ocr._API_KEY
    vision_ocr._API_KEY = "test-key"
    try:
        httpx.post = lambda url, headers, json, timeout: _OkResponse()
        items = vision_ocr.extract_invoice_items(b"fake-bytes")
        check("extract_invoice_items parses a well-formed OpenAI response and drops blank names",
              items == [{"name": "Кабель", "qty": 2, "unit_cost": 90}])

        httpx.post = lambda url, headers, json, timeout: _BadShapeResponse()
        raised_bad_shape = False
        try:
            vision_ocr.extract_invoice_items(b"fake-bytes")
        except vision_ocr.VisionOcrError:
            raised_bad_shape = True
        check("extract_invoice_items raises on unparseable model output", raised_bad_shape)

        httpx.post = lambda url, headers, json, timeout: _ErrorResponse()
        raised_http_error = False
        try:
            vision_ocr.extract_invoice_items(b"fake-bytes")
        except vision_ocr.VisionOcrError:
            raised_http_error = True
        check("extract_invoice_items raises on a non-200 response", raised_http_error)

        # Product barcode/label scan (scan-to-fill SKU button) — same
        # photo->JSON pipeline, different prompt/shape, and must never
        # surface a price even if the model included one.
        class _LabelResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {"choices": [{"message": {"content":
                    '{"name": "Экран Redmi 9A", "sku": "  RM9A-DSP-042  ", "price": 999}'
                }}]}

        httpx.post = lambda url, headers, json, timeout: _LabelResponse()
        label = vision_ocr.extract_product_label(b"fake-bytes")
        check("extract_product_label parses name and (trimmed) sku",
              label == {"name": "Экран Redmi 9A", "sku": "RM9A-DSP-042"})
        check("extract_product_label never surfaces a price field", "price" not in label)

        class _EmptyLabelResponse:
            status_code = 200
            text = "ok"

            def json(self):
                return {"choices": [{"message": {"content": '{"name": null, "sku": null}'}}]}

        httpx.post = lambda url, headers, json, timeout: _EmptyLabelResponse()
        empty_label = vision_ocr.extract_product_label(b"fake-bytes")
        check("extract_product_label returns None for both fields when nothing was legible",
              empty_label == {"name": None, "sku": None})
    finally:
        httpx.post = orig_post
        vision_ocr._API_KEY = orig_key


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

        inventory.receive_stock(conn, product_id, cell_a, 2, staff_id)
        cash_sale_id = sales.create_sale(conn, None, "offline", staff_id, [(product_id, 1, 500)], payment_method="cash")
        check("payment_method defaults to cash and is stored on the order",
              sales.get_sale(conn, cash_sale_id)["payment_method"] == "cash")
        card_sale_id = sales.create_sale(conn, None, "offline", staff_id, [(product_id, 1, 500)], payment_method="card")
        check("a card sale is stored with method='card'", sales.get_sale(conn, card_sale_id)["payment_method"] == "card")

        cash_income = [t for t in cash.list_transactions(conn) if t["ref_type"] == "sales_order" and t["ref_id"] == cash_sale_id]
        card_income = [t for t in cash.list_transactions(conn) if t["ref_type"] == "sales_order" and t["ref_id"] == card_sale_id]
        check("a cash sale posts a cash-method income row for its full total",
              len(cash_income) == 1 and cash_income[0]["method"] == "cash" and cash_income[0]["amount"] == 500)
        check("a card sale posts a card-method income row, not cash",
              len(card_income) == 1 and card_income[0]["method"] == "card")


def scenario_masters(db_path: str) -> None:
    print("scenario: мастера — CRUD, pay rate/percentage, profit-based stats")
    with get_conn(db_path) as conn:
        admin_id = auth.create_staff(conn, "masteradmin", "pass", "Админ Мастеров", "admin")

        master_id = auth.create_master(conn, "Мастер Иван", None, "percent", 40)
        master = auth.get_master(conn, master_id)
        check("create_master sets role=master", master["role"] == "master")
        check("create_master auto-generates a login (never surfaced to Павел)",
              bool(master["login"]) and master["login"] != "")
        check("telegram_id is optional and defaults to null", master["telegram_id"] is None)
        check("pay_type/pay_value stored as given", master["pay_type"] == "percent" and master["pay_value"] == 40)

        second_id = auth.create_master(conn, "Мастер Иван", 555111222, "fixed", 300)
        second = auth.get_master(conn, second_id)
        check("two masters with the same name get distinct auto-generated logins",
              second["login"] != master["login"])
        check("telegram_id is stored when given", second["telegram_id"] == 555111222)

        check("list_masters (active only) returns both fresh masters",
              {m["id"] for m in auth.list_masters(conn)} >= {master_id, second_id})

        auth.update_master(conn, master_id, "Мастер Иван Петров", 999888777, "percent", 50)
        updated = auth.get_master(conn, master_id)
        check("update_master changes name/telegram_id/pay_value",
              updated["name"] == "Мастер Иван Петров" and updated["telegram_id"] == 999888777 and updated["pay_value"] == 50)

        auth.set_master_active(conn, second_id, False)
        check("a deactivated master drops out of the active-only list",
              second_id not in {m["id"] for m in auth.list_masters(conn)})
        check("but include_inactive=True still finds them (for reactivation)",
              second_id in {m["id"] for m in auth.list_masters(conn, include_inactive=True)})
        check("get_master still finds a deactivated master (unlike get_staff_by_id)",
              auth.get_master(conn, second_id) is not None)
        auth.set_master_active(conn, second_id, True)

        # Profit-based stats: a real repair, with a part whose cost basis
        # comes from an actual goods receipt (unit_cost snapshotted onto
        # the repair_use movement at write-off time).
        product_id = inventory.create_product(conn, "Экран для профита", "SKU-PROFIT", "Экраны", "шт", True, False, min_qty=0, price=None)
        cell_id = inventory.create_cell(conn, "M1-01", None, None)
        purchases.create_receipt(conn, None, "PROFIT-INV", admin_id, [(product_id, cell_id, 5, 200)])

        client_id = clients.get_or_create_by_phone(conn, "Клиент Мастера", "+380671119988", source="offline")
        order_id = repairs.create_repair(
            conn, client_id, "Смартфон", "Xiaomi", "Redmi 9", None, "экран разбит", "offline", master_id, 1000, admin_id,
        )
        repairs.assign_master(conn, order_id, master_id)
        inventory.record_movement(conn, product_id, 1, "repair_use", admin_id, from_cell_id=cell_id, ref_type="repair_order", ref_id=order_id)

        movement = [m for m in inventory.list_movements(conn) if m["ref_type"] == "repair_order" and m["ref_id"] == order_id][0]
        check("repair_use snapshots the product's current unit_cost onto the movement", movement["unit_cost"] == 200)

        # Not issued yet — must not count toward stats (mirrors касса: only
        # "Выдан" repairs count).
        pre_issue_stats = masters.period_stats(conn, master_id)
        check("an unissued repair doesn't count toward stats yet", pre_issue_stats["repairs_count"] == 0)

        repairs.set_price(conn, order_id, price_estimate=1000, price_final=1000)
        repairs.update_status(conn, order_id, "issued", admin_id)

        stats = masters.period_stats(conn, master_id)
        check("issued repair counts toward all-time stats", stats["repairs_count"] == 1)
        check("revenue is the repair's price_final", stats["revenue"] == 1000)
        check("parts cost is qty * snapshotted unit_cost (1 * 200)", stats["parts_cost"] == 200)
        check("profit is revenue minus parts cost (1000 - 200 = 800)", stats["profit"] == 800)

        check("payout() at 50% of an 800 profit is 400", masters.payout(800, 1, "percent", 50) == 400)
        check("payout() for a fixed rate is repairs_count * pay_value, not profit-based",
              masters.payout(800, 3, "fixed", 300) == 900)
        check("payout() with no pay_type configured is 0, not a crash", masters.payout(800, 1, None, None) == 0)
        check("payout() never goes negative even if parts cost exceeded price",
              masters.payout(-500, 1, "percent", 50) == 0)

        summary = masters.master_summary(conn, auth.get_master(conn, master_id))
        check("master_summary's all_time picks up the issued repair", summary["all_time"]["repairs_count"] == 1)
        check("master_summary computes a payout per period using the master's own pay_type/value",
              summary["all_time"]["payout"] == round(800 * 50 / 100))
        check("master_summary's today bucket also has the repair (issued just now)",
              summary["today"]["repairs_count"] == 1)


def scenario_cash(db_path: str) -> None:
    print("scenario: касса — cash-on-hand balance, expenses, adjustments, period summary")
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "cashier2", "pass", "Кассир 2", "owner")
        balance_before = cash.cash_balance(conn)

        cash.record_income(conn, "cash", 1000, "manual", None, staff_id, "тестовый приход нал")
        cash.record_income(conn, "card", 2000, "manual", None, staff_id, "тестовый приход карта")
        check("cash balance only moves on cash-method income, not card",
              cash.cash_balance(conn) == balance_before + 1000)

        cash.record_expense(conn, "cash", 300, "rent", "аренда за день", staff_id)
        check("a cash expense reduces the cash balance", cash.cash_balance(conn) == balance_before + 1000 - 300)

        cash.record_expense(conn, "card", 150, "supplies", "оплата картой поставщику", staff_id)
        check("a card expense does not touch the cash balance", cash.cash_balance(conn) == balance_before + 1000 - 300)

        cash.record_adjustment(conn, 500, "довнесли на размен", staff_id)
        check("a positive adjustment (внести) adds to the cash balance",
              cash.cash_balance(conn) == balance_before + 1000 - 300 + 500)
        cash.record_adjustment(conn, -200, "забрали в сейф", staff_id)
        check("a negative adjustment (изъять) subtracts from the cash balance",
              cash.cash_balance(conn) == balance_before + 1000 - 300 + 500 - 200)

        raised = False
        try:
            cash.record_adjustment(conn, 0, "нулевая сумма", staff_id)
        except ValueError:
            raised = True
        check("a zero-amount adjustment is rejected, not silently a no-op", raised)

        raised = False
        try:
            cash.record_expense(conn, "cash", -50, "other", "отрицательная сумма", staff_id)
        except ValueError:
            raised = True
        check("a negative expense amount is rejected", raised)

        today = timefmt.kyiv_today()
        utc_start, utc_end = timefmt.kyiv_date_range_utc(today, today)
        summary = cash.period_summary(conn, utc_start, utc_end)
        check("today's period summary picks up the income/expense just recorded",
              summary["income_cash"] >= 1500 and summary["income_card"] >= 2000 and summary["expense_cash"] >= 300)
        check("net is income minus expense for the period",
              summary["net"] == summary["income_total"] - summary["expense_total"])

        far_future_start, far_future_end = timefmt.kyiv_date_range_utc("2099-01-01", "2099-01-01")
        empty_summary = cash.period_summary(conn, far_future_start, far_future_end)
        check("a period with no transactions summarizes to all zeros",
              empty_summary == {"income_cash": 0, "income_card": 0, "income_total": 0,
                                 "expense_cash": 0, "expense_card": 0, "expense_total": 0, "net": 0})

        recent = cash.list_transactions(conn, limit=3)
        check("list_transactions respects the limit and is newest-first",
              len(recent) == 3 and recent[0]["created_at"] >= recent[1]["created_at"])


class _FakeTelegramResponse:
    status_code = 200
    text = "ok"

    def json(self):
        return {"result": {"message_id": 555}}


def scenario_repair_card_notify(db_path: str) -> None:
    print("scenario: repair staff-group card + notify")
    from core import notify

    with get_conn(db_path) as conn:
        client_id = clients.get_or_create_by_phone(conn, "Карточка <script>", "+380990004455", source="offline")
        order_id = repairs.create_repair(
            conn, client_id, "Смартфон", "Apple", "iPhone 12", "SN123",
            "Не <включается>", "offline", None, 500, 1,
        )
        repair = repairs.get_repair(conn, order_id)

    text = repairs.render_card_text(repair)
    check("repair card escapes HTML in client name", "&lt;script&gt;" in text and "<script>" not in text)
    check("repair card escapes HTML in defect description", "&lt;включается&gt;" in text)
    check("repair card shows the device line", "Смартфон Apple iPhone 12" in text)
    check("repair card shows 'не назначен' when no master is assigned", "не назначен" in text)
    check("repair card shows the order id", f"№{order_id}" in text)
    check("repair card header uses the status label", "Новый" in text)

    kb_new = repairs.render_keyboard(order_id, "new")
    check("keyboard for 'new' offers to take the job",
          kb_new["inline_keyboard"][0][0]["callback_data"] == f"repair_take:{order_id}")
    check("keyboard for 'new' also offers Открыть в CRM",
          kb_new["inline_keyboard"][-1][0]["callback_data"] == f"open_crm:repair:{order_id}")
    kb_in_progress = repairs.render_keyboard(order_id, "in_progress")
    check("keyboard for 'in_progress' offers done + release",
          {b["callback_data"] for b in kb_in_progress["inline_keyboard"][0]}
          == {f"repair_done:{order_id}", f"repair_release:{order_id}"})
    kb_ready = repairs.render_keyboard(order_id, "ready")
    check("keyboard for 'ready' has nothing left to press but Открыть в CRM",
          kb_ready["inline_keyboard"] == [[{"text": "🔗 Открыть в CRM", "callback_data": f"open_crm:repair:{order_id}"}]])

    # No CRM_STAFF_GROUP_CHAT_ID in the test env — must no-op, never raise.
    raised = False
    try:
        notify.notify_repair_card("test")
    except Exception:
        raised = True
    check("notify_repair_card is a silent no-op when unconfigured", not raised)

    # With both destinations configured, a new repair must fan out to both:
    # the "Ремонт техники" topic in the main group, and the separate
    # masters group — two independent sendMessage calls, both carrying the
    # initial keyboard.
    calls = []

    def _fake_post(url, json, timeout):
        calls.append({"url": url, **json})
        return _FakeTelegramResponse()

    orig_post = httpx.post
    orig_env = (notify._BOT_TOKEN, notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID)
    notify._BOT_TOKEN = "test-token"
    notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = "-100main", "5", "-100masters"
    edit_calls = []

    def _fake_post_edit(url, json, timeout):
        edit_calls.append({"url": url, **json})
        return _FakeTelegramResponse()

    try:
        httpx.post = _fake_post
        sent = notify.notify_repair_card("карточка", reply_markup=kb_new)

        # A status change — whether from a button or the web app — must
        # edit every stored message for the order, not post a new one.
        # Keep _BOT_TOKEN/chat-id overrides active through this part too,
        # since edit_message()/sync_repair_cards() short-circuit without them.
        httpx.post = _fake_post_edit
        ok = notify.edit_message(sent[0][0], sent[0][1], "обновлённый текст", reply_markup=kb_in_progress)
        notify.sync_repair_cards([(c, m, hp) for c, m, _k, hp in sent], "синхронизировано")
    finally:
        httpx.post = orig_post
        notify._BOT_TOKEN, notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = orig_env

    check(
        "notify_repair_card fans out to both the repair topic and the masters group",
        len(sent) == 2 and {c for c, m, k, hp in sent} == {"-100main", "-100masters"}
        and all(c["reply_markup"] == kb_new for c in calls),
    )
    check("the topic destination carries message_thread_id", calls[0]["message_thread_id"] == "5")
    check("a text-only card (no photo) is tracked with has_photo=False",
          all(hp is False for c, m, k, hp in sent))
    check("edit_message reports success against the (faked) Telegram API", ok)
    check("sync_repair_cards edits every stored message for the order",
          sum(1 for c in edit_calls if c["url"].endswith("editMessageText")) == 1 + len(sent))
    check("sync_repair_cards clears the keyboard when reply_markup is omitted",
          any(c.get("reply_markup") == {"inline_keyboard": []} for c in edit_calls))

    # A repair with a device photo must go out as ONE message — the photo
    # itself carrying the card text as its caption and the status keyboard
    # — not a bare photo followed by a separate text card.
    photo_calls = []

    def _fake_post_photo(url, data, files, timeout):
        photo_calls.append({"url": url, "data": data, "files": files})
        return _FakeTelegramResponse()

    caption_edit_calls = []

    def _fake_post_caption_edit(url, json, timeout):
        caption_edit_calls.append({"url": url, **json})
        return _FakeTelegramResponse()

    notify._BOT_TOKEN = "test-token"
    notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = "-100main", "5", "-100masters"
    try:
        httpx.post = _fake_post_photo
        sent_photo = notify.notify_repair_card(
            "карточка с фото", reply_markup=kb_new, photo=(b"fake-jpeg-bytes", "device.jpg")
        )

        httpx.post = _fake_post_caption_edit
        notify.sync_repair_cards([(c, m, hp) for c, m, _k, hp in sent_photo], "готово", None)
    finally:
        httpx.post = orig_post
        notify._BOT_TOKEN, notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = orig_env

    check("a repair with a photo sends exactly one message per destination (no separate bare photo)",
          len(photo_calls) == 2)
    check("both photo+caption cards are tracked with has_photo=True",
          len(sent_photo) == 2 and all(hp is True for c, m, k, hp in sent_photo))
    check("the photo call carries the card text as its caption, not a follow-up message",
          photo_calls[0]["data"]["caption"] == "карточка с фото")
    check("the photo call carries the status keyboard as a JSON-encoded field (multipart has no nested objects)",
          json.loads(photo_calls[0]["data"]["reply_markup"]) == kb_new)
    check("the topic photo carries message_thread_id", photo_calls[0]["data"]["message_thread_id"] == "5")
    check("a status change on a photo card edits via editMessageCaption, not editMessageText",
          len(caption_edit_calls) == 2 and all(c["url"].endswith("editMessageCaption") for c in caption_edit_calls))

    check("an overlong caption is truncated to Telegram's 1024-char cap",
          len(notify._as_caption("x" * 2000)) == 1024)

    # Фаза C (23.08): explicit per-store group ids must win over whatever
    # the module-level (legacy env) constants happen to be — this is the
    # whole point of notify_repair_card/notify_staff_group taking them as
    # keyword args now, since webapp.routers.repairs.create_view passes the
    # CURRENT request's store, which may differ from the "default" store
    # the process started with.
    store_a_calls = []

    def _fake_post_store_a(url, json, timeout):
        store_a_calls.append({"url": url, **json})
        return _FakeTelegramResponse()

    notify._BOT_TOKEN = "test-token"
    # Module constants deliberately point at a DIFFERENT ("wrong") group —
    # a stale set of legacy env vars a real deployment might still have —
    # to prove the explicit args, not these, decide where the card goes.
    notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = "-999wrong", "1", "-999wrong-masters"
    try:
        httpx.post = _fake_post_store_a
        sent_store_a = notify.notify_repair_card(
            "карточка магазина A", reply_markup=kb_new,
            staff_group_chat_id="-100storeA", repair_topic_id="7", masters_group_chat_id="-100storeA-masters",
        )
    finally:
        httpx.post = orig_post
        notify._BOT_TOKEN, notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = orig_env

    check("explicit staff_group_chat_id/masters_group_chat_id override the (wrong) module constants",
          {c for c, m, k, hp in sent_store_a} == {"-100storeA", "-100storeA-masters"})
    check("explicit repair_topic_id is used, not the module constant",
          any(c.get("message_thread_id") == "7" for c in store_a_calls))
    check("nothing was sent to the module-constant (wrong) group",
          all(c["chat_id"] not in ("-999wrong", "-999wrong-masters") for c in store_a_calls))

    # notify_staff_group gets the same treatment, same reasoning.
    notify._BOT_TOKEN = "test-token"
    notify._STAFF_GROUP_CHAT_ID = "-999wrong"
    try:
        httpx.post = _fake_post_store_a
        store_a_calls.clear()
        notify.notify_staff_group("привет магазину A", staff_group_chat_id="-100storeA")
    finally:
        httpx.post = orig_post
        notify._BOT_TOKEN, notify._STAFF_GROUP_CHAT_ID, notify._REPAIR_TOPIC_ID, notify._MASTERS_GROUP_CHAT_ID = orig_env
    check("notify_staff_group's explicit staff_group_chat_id also overrides the module constant",
          len(store_a_calls) == 1 and store_a_calls[0]["chat_id"] == "-100storeA")


def scenario_repair_actions(db_path: str) -> None:
    print("scenario: claim / complete / cancel a repair (button actions)")
    with get_conn(db_path) as conn:
        master_a = auth.create_staff(conn, "master_a", "pass", "Мастер A", "master")
        master_b = auth.create_staff(conn, "master_b", "pass", "Мастер B", "master")
        client_id = clients.get_or_create_by_phone(conn, "Кнопки Тест", "+380990005566", source="offline")

        order_id = repairs.create_repair(
            conn, client_id, "Ноутбук", "Dell", "XPS", None, "не включается", "offline", None, None, 1,
        )

        check("claim by master A succeeds", repairs.claim_repair(conn, order_id, master_a))
        check("second claim by master B fails — already taken", not repairs.claim_repair(conn, order_id, master_b))
        repair = repairs.get_repair(conn, order_id)
        check("repair is now in_progress with master A assigned",
              repair["status"] == "in_progress" and repair["master_id"] == master_a)

        check("complete by the wrong master fails without override", not repairs.complete_repair(conn, order_id, master_b))
        check("complete by the assigned master succeeds", repairs.complete_repair(conn, order_id, master_a))
        repair = repairs.get_repair(conn, order_id)
        check("repair is now ready", repair["status"] == "ready")

        check("cancel after already-ready fails — not in_progress anymore", not repairs.cancel_repair(conn, order_id, master_a))

        # cancel_repair (21.08) is a terminal outcome — "не удалось
        # починить" — NOT a release back to the queue for someone else to
        # try (that was this button's old behavior; Павел wants a real
        # failure state instead).
        order_id_2 = repairs.create_repair(
            conn, client_id, "Планшет", "Samsung", "Tab", None, "треснул экран", "offline", None, None, 1,
        )
        check("claim order 2", repairs.claim_repair(conn, order_id_2, master_a))
        check("cancel by the wrong master fails without override", not repairs.cancel_repair(conn, order_id_2, master_b))
        check("cancel by the claiming master succeeds", repairs.cancel_repair(conn, order_id_2, master_a))
        repair2 = repairs.get_repair(conn, order_id_2)
        check("order 2 is 'cancelled', not back to 'new' — a terminal outcome, not a re-queue",
              repair2["status"] == "cancelled")
        check("master_id stays on the row after cancelling — history shows who attempted it",
              repair2["master_id"] == master_a)

        order_id_3 = repairs.create_repair(
            conn, client_id, "Телефон", "Apple", "SE", None, "не включается", "offline", None, None, 1,
        )
        check("claim order 3", repairs.claim_repair(conn, order_id_3, master_b))
        check("owner can cancel order 3 on master B's behalf via override",
              repairs.cancel_repair(conn, order_id_3, 1, override=True))
        check("order 3 is cancelled via override", repairs.get_repair(conn, order_id_3)["status"] == "cancelled")

        order_id_4 = repairs.create_repair(
            conn, client_id, "Часы", "Apple", "Watch", None, "не заряжается", "offline", None, None, 1,
        )
        check("claim order 4", repairs.claim_repair(conn, order_id_4, master_b))
        check("owner can complete order 4 on master B's behalf via override",
              repairs.complete_repair(conn, order_id_4, 1, override=True))


def scenario_quick_cash_chat(db_path: str) -> None:
    """bot/quick_actions.py's «💵 Касса» — расход / внести / снять, driven
    through a fake private chat, same technique as
    scenario_quick_intake_chat. Unlike that one, this drives all the way
    through to the DB-writing confirm callbacks (cash_expense_confirm /
    cash_adjustment_confirm) against the real shared test db — what's
    actually worth checking here is that a chat-driven expense/внесение/
    снятие lands the exact same core.cash rows the direct-call scenario
    above already trusts, not just that the screens look right."""
    print("scenario: quick Касса dialog (расход / внести / снять) via chat")
    import asyncio
    from types import SimpleNamespace

    os.environ.setdefault("CRM_MINIAPP_URL", "https://example.invalid/miniapp")

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa
    from core.stores import StoreConfig

    CHAT_ID = 777002
    owner_alerts: list[tuple] = []
    STAFF_TG_ID = 1417059281
    fake_store = StoreConfig(id="1", name="Тестовый магазин", db_path=db_path)

    with get_conn(db_path) as conn:
        # admin, not storekeeper: «внести/снять» is a balance correction,
        # owner/admin only since Заход 2 — and this scenario drives all three.
        auth.create_staff(conn, "cash_bot_tester", "pass", "Кассир Тест", "admin")
        auth.link_staff_telegram(conn, "cash_bot_tester", STAFF_TG_ID)
        tester_row = dict(auth.get_staff_by_telegram_id(conn, STAFF_TG_ID))
        balance_before = cash.cash_balance(conn)

    class _Chat:
        def __init__(self) -> None:
            self.log: list[dict] = []
            self._next_id = 9000

        def add(self, author: str, text: str, markup=None) -> int:
            self._next_id += 1
            self.log.append({"id": self._next_id, "author": author, "text": text, "markup": markup})
            return self._next_id

        def delete(self, message_id: int) -> None:
            self.log = [m for m in self.log if m["id"] != message_id]

        def edit(self, message_id: int, text: str, markup) -> None:
            for m in self.log:
                if m["id"] == message_id:
                    m["text"], m["markup"] = text, markup

        @property
        def bot_messages(self) -> list[dict]:
            return [m for m in self.log if m["author"] == "bot"]

        def last(self) -> dict | None:
            return self.log[-1] if self.log else None

    class _Bot:
        def __init__(self, chat: "_Chat") -> None:
            self.chat = chat

        async def delete_message(self, chat_id, message_id):
            self.chat.delete(message_id)

        async def delete_messages(self, chat_id, message_ids):
            for mid in message_ids:
                self.chat.delete(mid)

        async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            self.chat.edit(message_id, text, reply_markup)

        async def send_message(self, chat_id, text, **kwargs):
            # DMs to OTHER people (the owner alert on a shift discrepancy)
            # — not part of this chat's screen, just recorded.
            owner_alerts.append((chat_id, text))

    class _Msg:
        def __init__(self, chat: "_Chat", bot: "_Bot", text: str) -> None:
            self._chat_log = chat
            self.bot = bot
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.from_user = SimpleNamespace(id=STAFF_TG_ID, full_name="Кассир")
            self.text = text
            self.message_id = chat.add("staff", text)

        async def answer(self, text, reply_markup=None):
            return SimpleNamespace(message_id=self._chat_log.add("bot", text, reply_markup))

    class _CbMessage:
        def __init__(self, chat: "_Chat", message_id: int) -> None:
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.message_id = message_id
            self._chat_log = chat

        async def edit_text(self, text, reply_markup=None):
            self._chat_log.edit(self.message_id, text, reply_markup)

    class _Cb:
        def __init__(self, chat: "_Chat", bot: "_Bot", data: str, message_id: int) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=STAFF_TG_ID)
            self.message = _CbMessage(chat, message_id)
            self.bot = bot
            self.answered: list[tuple] = []

        async def answer(self, text=None, show_alert=False):
            self.answered.append((text, show_alert))

    original_resolve = qa._resolve_staff_for_dm
    original_get_store = qa.get_store
    qa._resolve_staff_for_dm = lambda telegram_id: (fake_store, tester_row)
    qa.get_store = lambda store_id: fake_store

    async def run() -> None:
        chat = _Chat()
        bot = _Bot(chat)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=STAFF_TG_ID))

        def say(text: str) -> _Msg:
            return _Msg(chat, bot, text)

        def tap(data: str) -> _Cb:
            return _Cb(chat, bot, data, chat.last()["id"])

        def screen() -> dict:
            return chat.bot_messages[-1]

        # --- Смена: nothing that writes works before today's shift is open ---
        await qa.cash_start(say(qa.BTN_CASH), state)
        check("with no shift open, Касса shows «сверить остатки» (the точка's accounts) instead of its menu",
              "Сверьте остатки" in screen()["text"] and "Наличные:" in screen()["text"]
              and "В товаре по всему бизнесу" in screen()["text"] and "Баланс:" not in screen()["text"])
        await qa.shift_open_mismatch(tap("shift_open_mismatch"), state)
        check("«есть расхождение» asks what exactly doesn't match", "не сходится" in screen()["text"])
        await qa.shift_got_note(say("наличных на 200 грн меньше"), state)
        check("the shift opens anyway, the discrepancy is acknowledged",
              "Смена открыта" in screen()["text"] and "наличных на 200 грн меньше" in screen()["text"])
        with get_conn(db_path) as conn:
            opened = shifts.current_shift(conn, tester_row["id"], 1)
            check("the shift is on record with its note and a snapshot of the balances",
                  opened is not None and opened["discrepancy_note"] == "наличных на 200 грн меньше"
                  and "accounts" in json.loads(opened["snapshot_json"]))
            shift_doc = documents.get_for(conn, "shift", opened["id"])
            check("every OTHER owner/admin with a Telegram id is told about the discrepancy, the author isn't",
                  owner_alerts and all("Расхождение при открытии смены" in text for _cid, text in owner_alerts)
                  and STAFF_TG_ID not in [cid for cid, _text in owner_alerts])
            check("and it is in the journal as a document (СМ) carrying the discrepancy",
                  shift_doc is not None and "Расхождение" in shift_doc["title"])

        # --- Расход ---
        await qa.cash_start(say(qa.BTN_CASH), state)
        check("cash menu shows the current balance", "Баланс:" in screen()["text"])
        check("and offers to close the shift",
              any(b.callback_data == "shift_close" for row in screen()["markup"].inline_keyboard for b in row))

        await qa.cash_menu_expense(tap("cash_menu_expense"), state)
        check("expense flow asks for the amount", "Сумма расхода" in screen()["text"])

        await qa.cash_expense_got_amount(say("250"), state)
        check("expense flow then asks for a category", "Категория расхода" in screen()["text"])

        await qa.cash_expense_got_category(tap("cash_cat_supplies"), state)
        check("expense flow then asks for a comment", "Комментарий" in screen()["text"])

        await qa.cash_expense_got_comment(say("Тест"), state)
        card = screen()
        check("expense confirm card recaps amount + category + comment",
              "250 грн" in card["text"] and "Закупка" in card["text"] and "Тест" in card["text"])
        check("expense confirm card carries confirm/cancel buttons", card["markup"] is not None)

        await qa.cash_expense_confirm(tap("cash_expense_confirm"), state)
        check("expense confirm clears the FSM state", (await state.get_state()) is None)
        with get_conn(db_path) as conn:
            after_expense = cash.cash_balance(conn)
        check("a cash expense actually reduces the drawer balance by exactly its amount",
              after_expense == balance_before - 250)

        # --- Внести ---
        await qa.cash_start(say(qa.BTN_CASH), state)
        await qa.cash_menu_in(tap("cash_menu_in"), state)
        check("«Внести» flow asks for the amount to bring in", "внесения" in screen()["text"])
        await qa.cash_adjustment_got_amount(say("500"), state)
        check("«Внести» flow then asks for an optional comment", "Комментарий" in screen()["text"])
        await qa.cash_adjustment_skip_comment(tap("cash_comment_skip"), state)
        check("«Внести» confirm card recaps the amount without a comment line",
              "500 грн" in screen()["text"] and "Комментарий:" not in screen()["text"])
        await qa.cash_adjustment_confirm(tap("cash_adjustment_confirm"), state)
        with get_conn(db_path) as conn:
            after_in = cash.cash_balance(conn)
        check("«Внести» actually increases the drawer balance by exactly its amount",
              after_in == after_expense + 500)

        # --- Снять ---
        await qa.cash_start(say(qa.BTN_CASH), state)
        await qa.cash_menu_out(tap("cash_menu_out"), state)
        check("«Снять» flow asks for the amount to take out", "снятия" in screen()["text"])
        await qa.cash_adjustment_got_amount(say("100"), state)
        await qa.cash_adjustment_got_comment(say("В сейф"), state)
        check("«Снять» confirm card recaps a typed comment too", "В сейф" in screen()["text"])
        await qa.cash_adjustment_confirm(tap("cash_adjustment_confirm"), state)
        with get_conn(db_path) as conn:
            after_out = cash.cash_balance(conn)
        check("«Снять» actually decreases the drawer balance by exactly its amount",
              after_out == after_in - 100)

        # --- Отмена посреди диалога ---
        # Three finished confirmations (Расход/Внести/Снять above) are
        # legitimately still sitting in the chat at this point — a
        # confirmed card is a lasting record, never auto-deleted (same as
        # repair_confirm/buyback_confirm). Cancel must remove only the
        # screen ITS OWN abandoned attempt put up, not those.
        messages_before = len(chat.bot_messages)
        await qa.cash_start(say(qa.BTN_CASH), state)
        await qa.cash_menu_expense(tap("cash_menu_expense"), state)
        await qa.cash_expense_got_amount(say("999"), state)
        await qa.cancel_flow(say(qa.BTN_CANCEL), state)
        check("❌ Отмена mid-flow wipes its own screen, leaving earlier confirmations untouched",
              len(chat.bot_messages) == messages_before)
        with get_conn(db_path) as conn:
            after_cancel = cash.cash_balance(conn)
        check("a cancelled expense never touches the balance", after_cancel == after_out)

    try:
        asyncio.run(run())
    finally:
        qa._resolve_staff_for_dm = original_resolve
        qa.get_store = original_get_store


def scenario_quick_client_search_chat(db_path: str) -> None:
    """bot/quick_actions.py's «🔍 Клиент» — search by phone or name, driven
    through a fake private chat, same technique as scenario_quick_cash_chat
    (real shared test db, monkeypatched _resolve_staff_for_dm/get_store)."""
    print("scenario: quick Клиент search dialog (телефон / имя) via chat")
    import asyncio
    from types import SimpleNamespace

    os.environ.setdefault("CRM_MINIAPP_URL", "https://example.invalid/miniapp")

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa
    from core.stores import StoreConfig

    CHAT_ID = 777003
    STAFF_TG_ID = 1417059282
    fake_store = StoreConfig(id="1", name="Тестовый магазин", db_path=db_path)

    with get_conn(db_path) as conn:
        auth.create_staff(conn, "client_search_bot_tester", "pass", "Продавец Тест", "admin")
        auth.link_staff_telegram(conn, "client_search_bot_tester", STAFF_TG_ID)
        # ЖЖЖТЕСТКЛИЕНТ prefix: the shared test db already carries clients
        # from every scenario that ran before this one — a plain "Иван
        # Петров" could collide with something another scenario seeded,
        # throwing off the exact match-count checks below.
        petrov_id = clients.get_or_create_by_phone(conn, "ЖЖЖТЕСТКЛИЕНТ Иван Петров", "0561111111", source="offline")
        clients.get_or_create_by_phone(conn, "ЖЖЖТЕСТКЛИЕНТ Иван Сидоров", "0562222222", source="offline")
        clients.get_or_create_by_phone(conn, "ЖЖЖТЕСТКЛИЕНТ Мария Иванова", "0563333333", source="offline")

    class _Chat:
        def __init__(self) -> None:
            self.log: list[dict] = []
            self._next_id = 12000

        def add(self, author: str, text: str, markup=None) -> int:
            self._next_id += 1
            self.log.append({"id": self._next_id, "author": author, "text": text, "markup": markup})
            return self._next_id

        def delete(self, message_id: int) -> None:
            self.log = [m for m in self.log if m["id"] != message_id]

        def edit(self, message_id: int, text: str, markup) -> None:
            for m in self.log:
                if m["id"] == message_id:
                    m["text"], m["markup"] = text, markup

        @property
        def bot_messages(self) -> list[dict]:
            return [m for m in self.log if m["author"] == "bot"]

        def last(self) -> dict | None:
            return self.log[-1] if self.log else None

    class _Bot:
        def __init__(self, chat: "_Chat") -> None:
            self.chat = chat

        async def delete_message(self, chat_id, message_id):
            self.chat.delete(message_id)

        async def delete_messages(self, chat_id, message_ids):
            for mid in message_ids:
                self.chat.delete(mid)

        async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            self.chat.edit(message_id, text, reply_markup)

    class _Msg:
        def __init__(self, chat: "_Chat", bot: "_Bot", text: str) -> None:
            self._chat_log = chat
            self.bot = bot
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.from_user = SimpleNamespace(id=STAFF_TG_ID, full_name="Продавец")
            self.text = text
            self.message_id = chat.add("staff", text)

        async def answer(self, text, reply_markup=None):
            return SimpleNamespace(message_id=self._chat_log.add("bot", text, reply_markup))

    class _CbMessage:
        def __init__(self, chat: "_Chat", message_id: int) -> None:
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.message_id = message_id
            self._chat_log = chat

        async def edit_text(self, text, reply_markup=None):
            self._chat_log.edit(self.message_id, text, reply_markup)

    class _Cb:
        def __init__(self, chat: "_Chat", bot: "_Bot", data: str, message_id: int) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=STAFF_TG_ID)
            self.message = _CbMessage(chat, message_id)
            self.bot = bot
            self.answered: list[tuple] = []

        async def answer(self, text=None, show_alert=False):
            self.answered.append((text, show_alert))

    original_resolve = qa._resolve_staff_for_dm
    original_get_store = qa.get_store
    qa._resolve_staff_for_dm = lambda telegram_id: (fake_store, {"role": "admin", "id": 999})
    qa.get_store = lambda store_id: fake_store

    async def run() -> None:
        chat = _Chat()
        bot = _Bot(chat)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=STAFF_TG_ID))

        def say(text: str) -> _Msg:
            return _Msg(chat, bot, text)

        def tap(data: str) -> _Cb:
            return _Cb(chat, bot, data, chat.last()["id"])

        def screen() -> dict:
            return chat.bot_messages[-1]

        # --- Точное совпадение по телефону, в местном формате ---
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_search(tap("client_menu_search"), state)
        check("search prompt asks for phone or name", "Телефон или имя" in screen()["text"])

        await qa.client_search_got_query(say("0561111111"), state)
        check("a local-format phone finds the exact client by its canonical +380 phone",
              "ЖЖЖТЕСТКЛИЕНТ Иван Петров" in screen()["text"])
        check("the client card shows the phone in canonical form", "+380561111111" in screen()["text"])
        check("the client card carries an Открыть в CRM button", screen()["markup"] is not None)
        check("a single-match search clears the FSM state", (await state.get_state()) is None)

        # --- Точное совпадение по фамилии ---
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_search(tap("client_menu_search"), state)
        await qa.client_search_got_query(say("ЖЖЖТЕСТКЛИЕНТ Иван Сидоров"), state)
        check("a unique name substring finds the exact client", "ЖЖЖТЕСТКЛИЕНТ Иван Сидоров" in screen()["text"])

        # --- Несколько совпадений -> список кнопок -> выбор ---
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_search(tap("client_menu_search"), state)
        await qa.client_search_got_query(say("ЖЖЖТЕСТКЛИЕНТ Иван"), state)
        picker = screen()
        check("a substring matching several clients shows a picker, not a card",
              "Нашлось 2" in picker["text"])
        check("the picker lists both matches as buttons",
              len(picker["markup"].inline_keyboard) == 2)
        check("picker buttons carry the right callback_data",
              any(row[0].callback_data == f"client_pick:{petrov_id}" for row in picker["markup"].inline_keyboard))

        pick_button = next(
            row[0] for row in picker["markup"].inline_keyboard if row[0].callback_data == f"client_pick:{petrov_id}"
        )
        await qa.client_search_pick(tap(pick_button.callback_data), state)
        check("picking from the list shows that exact client's card",
              "ЖЖЖТЕСТКЛИЕНТ Иван Петров" in screen()["text"])
        check("picking from the list clears the FSM state", (await state.get_state()) is None)

        # --- Ничего не найдено ---
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_search(tap("client_menu_search"), state)
        await qa.client_search_got_query(say("ЖЖЖНЕТКЛИЕНТАТАКОГО"), state)
        check("no matches warns and keeps the search open for another attempt",
              "Ничего не найдено" in screen()["text"] and (await state.get_state()) is not None)
        await qa.client_search_got_query(say("ЖЖЖТЕСТКЛИЕНТ Иван Петров"), state)
        check("a retry after a miss still finds the client", "ЖЖЖТЕСТКЛИЕНТ Иван Петров" in screen()["text"])

        # --- Отмена посреди поиска ---
        messages_before = len(chat.bot_messages)
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_search(tap("client_menu_search"), state)
        await qa.cancel_flow(say(qa.BTN_CANCEL), state)
        check("❌ Отмена mid-search wipes its own screen, leaving earlier results untouched",
              len(chat.bot_messages) == messages_before)

    try:
        asyncio.run(run())
    finally:
        qa._resolve_staff_for_dm = original_resolve
        qa.get_store = original_get_store


def scenario_repair_attachments(db_path: str) -> None:
    print("scenario: photo replies attach to a repair via its posted card")
    with get_conn(db_path) as conn:
        client_id = clients.get_or_create_by_phone(conn, "Вложения Тест", "+380990007788", source="offline")
        order_id = repairs.create_repair(
            conn, client_id, "Телефон", "Apple", "iPhone 12", None, "не держит заряд", "offline", None, None, 1,
        )

        check("no order found for an untracked (chat_id, message_id) pair",
              repairs.find_order_by_message(conn, "-100masters", 999) is None)

        repairs.save_order_messages(conn, order_id, [
            ("-100topic", 42, "topic", True), ("-100masters", 43, "masters_group", True),
        ])
        check("find_order_by_message resolves the topic card back to the repair",
              repairs.find_order_by_message(conn, "-100topic", 42) == order_id)
        check("find_order_by_message resolves the masters-group card too",
              repairs.find_order_by_message(conn, "-100masters", 43) == order_id)

        check("no attachments yet", repairs.get_attachments(conn, order_id) == [])
        attachment_id = repairs.add_attachment(conn, order_id, "42_abc123.jpg", "старая батарея", 1)
        check("add_attachment returns a real row id", attachment_id > 0)

        attachments = repairs.get_attachments(conn, order_id)
        check("get_attachments returns the saved photo with its caption and who added it",
              len(attachments) == 1 and attachments[0]["photo_path"] == "42_abc123.jpg"
              and attachments[0]["caption"] == "старая батарея" and attachments[0]["staff_name"] == "Владелец")


def scenario_phone_normalization(db_path: str) -> None:
    """core.clients.normalize_phone canonicalizes a number AND decides
    whether the thing it was handed is a phone number at all. Until
    04.09.2026 it only did the first job — every caller reads "" as «no
    phone», and free text was returned unchanged, so «не помню» passed
    every check in the codebase and landed in the clients table."""
    print("scenario: phone numbers are canonicalized, and free text is not a phone")
    check("an already-canonical number passes through", clients.normalize_phone("+380501234567") == "+380501234567")
    check("Telegram's no-plus form gets its +", clients.normalize_phone("380501234567") == "+380501234567")
    check("the local 0-prefixed form becomes international", clients.normalize_phone("0501234567") == "+380501234567")
    check("spaces, brackets and dashes are stripped", clients.normalize_phone("+38 (050) 123-45-67") == "+380501234567")
    check("a foreign number is still accepted as-is", clients.normalize_phone("+12125550123") == "+12125550123")

    check("a blank field means no phone", clients.normalize_phone("   ") == "")
    check("the untouched «+380» template means no phone", clients.normalize_phone("+380") == "")
    check("free text is not a phone number", clients.normalize_phone("не помню") == "")
    check("a number with a letter in it is not a phone number", clients.normalize_phone("050123456O") == "")
    check("too few digits is not a phone number", clients.normalize_phone("12345") == "")
    check("more digits than E.164 allows is not a phone number", clients.normalize_phone("+1234567890123456") == "")

    check("a blank field doesn't look entered", not clients.phone_looks_entered("  "))
    check("the «+380» template doesn't look entered", not clients.phone_looks_entered(" +380 "))
    check("free text DOES look entered — a typo to report back, not an empty field",
          clients.phone_looks_entered("не помню"))

    with get_conn(db_path) as conn:
        first = clients.get_or_create_by_phone(conn, "Телефонный Тест", "0996667788", source="offline")
        again = clients.get_or_create_by_phone(conn, "Телефонный Тест", "+38 (099) 666-77-88", source="offline")
        check("the same number typed two ways resolves to one client record", first == again)
        nobody_a = clients.get_or_create_by_phone(conn, "Безномерный А", "", source="offline")
        nobody_b = clients.get_or_create_by_phone(conn, "Безномерный Б", "", source="offline")
        check("two phoneless clients are never merged into each other", nobody_a != nobody_b)
        check("a phoneless client stores NULL, not an empty string",
              clients.get_client(conn, nobody_a)["phone"] is None)


def scenario_bot_html_safety() -> None:
    """Манифест §7 для сообщений бота: он поднят с parse_mode=HTML, поэтому
    любой текст не от нас — имя из чужой карточки контакта, название
    позиции, прочитанное с бумажной накладной через OpenAI vision —
    обязан экранироваться. Иначе Telegram роняет отправку на разборе HTML,
    и сотрудник остаётся, например, навсегда на «📷 Распознаю накладную…»."""
    print("scenario: bot messages escape text that isn't ours (parse_mode=HTML)")
    import asyncio
    from types import SimpleNamespace

    os.environ.setdefault("CRM_MINIAPP_URL", "https://example.invalid/miniapp")
    from bot import handlers as bot_handlers
    from bot import purchase_photo

    preview = purchase_photo._draft_preview_text([
        {"name_guess": "Кабель USB <-> Lightning AT&T", "qty": 2, "unit_cost": 150, "product_id": None},
    ])
    check("a line-item name read off a photo is escaped", "&lt;-&gt;" in preview and "&amp;T" in preview)
    check("...while the preview's own markup survives", "<b>Распознано с фото:</b>" in preview)
    check("...and the quantity and cost still render", "2 шт × 150" in preview)

    sent: list[str] = []

    class _ContactMessage:
        def __init__(self, contact) -> None:
            self.contact = contact

        async def reply(self, text, reply_markup=None):
            sent.append(text)

    asyncio.run(bot_handlers.offer_add_client(_ContactMessage(
        SimpleNamespace(first_name="Вася <дома>", last_name="&Co", phone_number="+380501234567")
    )))
    check("a name off someone's contact card is escaped before it goes out",
          "&lt;дома&gt;" in sent[0] and "&amp;Co" in sent[0])

def scenario_quick_intake_chat() -> None:
    """bot/quick_actions.py's step-by-step intake, driven through a fake
    private chat. The invariant under test: the flow leaves exactly ONE bot
    message and it is always the LAST message in the chat. Before 03.09 the
    flow edited a single message in place — an edit leaves a message where
    it already is, so the next question ended up ABOVE the staff member's
    reply, off screen and without a notification ("отвечаю, а бот ничего не
    присылает"). No aiogram Bot is ever built here: the handlers only ever
    touch message.bot/message.answer, so the fakes below are enough and
    nothing can reach the network."""
    print("scenario: quick intake dialog keeps one screen at the bottom of the chat")
    import asyncio
    from types import SimpleNamespace

    # bot.config raises at import time without this one. Nothing here calls
    # Telegram (see the docstring), so a placeholder suffices — setdefault
    # rather than an overwrite because a real value would be equally inert.
    os.environ.setdefault("CRM_MINIAPP_URL", "https://example.invalid/miniapp")

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa

    CHAT_ID = 777001
    STAFF_TG_ID = 1417059280

    class _Chat:
        """A private chat as Telegram would keep it: messages in order, with
        deletions actually removing them."""

        def __init__(self) -> None:
            self.log: list[dict] = []
            self._next_id = 1000

        def add(self, author: str, text: str, markup=None) -> int:
            self._next_id += 1
            self.log.append({"id": self._next_id, "author": author, "text": text, "markup": markup})
            return self._next_id

        def delete(self, message_id: int) -> None:
            self.log = [m for m in self.log if m["id"] != message_id]

        def edit(self, message_id: int, text: str, markup) -> None:
            for m in self.log:
                if m["id"] == message_id:
                    m["text"], m["markup"] = text, markup

        @property
        def bot_messages(self) -> list[dict]:
            return [m for m in self.log if m["author"] == "bot"]

        @property
        def staff_messages(self) -> list[dict]:
            return [m for m in self.log if m["author"] == "staff"]

        def last(self) -> dict | None:
            return self.log[-1] if self.log else None

    class _Bot:
        def __init__(self, chat: _Chat) -> None:
            self.chat = chat

        async def delete_message(self, chat_id, message_id):
            self.chat.delete(message_id)

        async def delete_messages(self, chat_id, message_ids):
            for mid in message_ids:
                self.chat.delete(mid)

        async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
            self.chat.edit(message_id, text, reply_markup)

        async def get_file(self, file_id):
            return SimpleNamespace(file_path=f"photos/{file_id}.jpg")

        async def download_file(self, file_path):
            return io.BytesIO(b"\xff\xd8fake-jpeg")

    class _Msg:
        def __init__(self, chat: _Chat, bot: _Bot, text=None, photo=None) -> None:
            self._chat_log = chat
            self.bot = bot
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.from_user = SimpleNamespace(id=STAFF_TG_ID, full_name="Мастер")
            self.text = text
            self.photo = photo
            self.message_id = chat.add("staff", text if text is not None else "[фото]")

        async def answer(self, text, reply_markup=None):
            return SimpleNamespace(message_id=self._chat_log.add("bot", text, reply_markup))

        async def answer_photo(self, photo, caption=None, reply_markup=None):
            # The fake chat log doesn't distinguish a photo's caption from
            # a plain message's text — every check below only cares about
            # that string and the attached keyboard, not the transport.
            return SimpleNamespace(message_id=self._chat_log.add("bot", caption, reply_markup))

    class _CbMessage:
        def __init__(self, chat: _Chat, message_id: int) -> None:
            self.chat = SimpleNamespace(id=CHAT_ID, type="private")
            self.message_id = message_id
            self._chat_log = chat

        async def edit_text(self, text, reply_markup=None):
            self._chat_log.edit(self.message_id, text, reply_markup)

        async def edit_reply_markup(self, reply_markup=None):
            for m in self._chat_log.log:
                if m["id"] == self.message_id:
                    m["markup"] = reply_markup

        async def answer(self, text, reply_markup=None):
            return SimpleNamespace(message_id=self._chat_log.add("bot", text, reply_markup))

    class _Cb:
        def __init__(self, chat: _Chat, bot: _Bot, data: str, message_id: int) -> None:
            self.data = data
            self.from_user = SimpleNamespace(id=STAFF_TG_ID)
            self.message = _CbMessage(chat, message_id)
            self.bot = bot
            self.answered: list[tuple] = []

        async def answer(self, text=None, show_alert=False):
            self.answered.append((text, show_alert))

    original_resolve = qa._resolve_staff_for_dm
    # The entry handlers resolve a real store + staff row out of the DB;
    # everything under test starts after that, so stub it and drive the
    # genuine handlers from the tap onward.
    _fake_store = SimpleNamespace(id="1", location_id=1, db_path=":memory:")
    qa._resolve_staff_for_dm = lambda telegram_id: (_fake_store, {"role": "master"})
    # No real base behind this scenario — the shift check has nothing to
    # read; the shift flow itself is covered in scenario_quick_cash_chat.
    original_shift_is_open = qa._shift_is_open
    qa._shift_is_open = lambda store, staff: True
    # client_menu_add re-resolves the store/staff itself (get_store by id,
    # then a real DB lookup by telegram_id) rather than reusing
    # _resolve_staff_for_dm — ":memory:" has no schema/rows and "test-store"
    # isn't a real configured store, so patch both rather than stand up a
    # real db just for the one "start another flow" check below.
    original_get_store = qa.get_store
    original_get_staff = auth.get_staff_by_telegram_id
    qa.get_store = lambda store_id: _fake_store
    auth.get_staff_by_telegram_id = lambda conn, telegram_id: {"id": 1, "role": "master"}

    async def run() -> None:
        chat = _Chat()
        bot = _Bot(chat)
        state = FSMContext(
            storage=MemoryStorage(),
            key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=STAFF_TG_ID),
        )

        def say(text=None, photo=None) -> _Msg:
            return _Msg(chat, bot, text=text, photo=photo)

        def tap(data: str) -> _Cb:
            return _Cb(chat, bot, data, chat.last()["id"])

        def screen() -> dict:
            return chat.bot_messages[-1]

        await qa.repair_start(say(qa.BTN_REPAIR), state)
        check("tap on 🔧 Ремонт leaves exactly one bot message", len(chat.bot_messages) == 1)
        check("the tap itself is deleted right away", chat.staff_messages == [])
        check("first screen is step 1 of 6 and asks for the photo",
              "шаг 1 из 6" in screen()["text"] and "фото" in screen()["text"].lower())

        first_screen_id = screen()["id"]
        await qa.repair_got_photo(say(photo=[SimpleNamespace(file_id="photo-1")]), state)
        check("answering REPOSTS a new message instead of editing the old one in place",
              screen()["id"] != first_screen_id)
        check("still exactly one bot message after the repost", len(chat.bot_messages) == 1)
        check("the screen is the last message in the chat", chat.last()["author"] == "bot")
        check("the sent photo is not left lying in the chat", chat.staff_messages == [])
        check("step 2 asks for the defect", "шаг 2 из 6" in screen()["text"] and "неисправность" in screen()["text"].lower())
        check("the photo is already acknowledged in the recap", "Фото: приложено ✅" in screen()["text"])

        await qa.repair_got_defect(say("Не включается <после падения>"), state)
        check("step 3 asks for the model", "шаг 3 из 6" in screen()["text"] and "Модель устройства" in screen()["text"])
        check("staff-typed text is HTML-escaped in the recap (parse_mode=HTML)",
              "&lt;после падения&gt;" in screen()["text"] and "<после падения>" not in screen()["text"])

        await qa.repair_got_model(say("iPhone 12"), state)
        check("device type is gone — the model alone is the device line", "Устройство: iPhone 12" in screen()["text"])
        check("step 4 asks for the price estimate, with a skip button",
              "шаг 4 из 6" in screen()["text"] and "Оценочная стоимость" in screen()["text"]
              and screen()["markup"] is not None)

        await qa.repair_got_price(say("не число"), state)
        check("a bad price answer warns WITHOUT losing the question",
              screen()["text"].startswith("⚠️") and "Оценочная стоимость" in screen()["text"])

        skip_button = screen()["markup"].inline_keyboard[0][0]
        await qa.repair_skip_price(tap(skip_button.callback_data), state)
        check("skipping the price moves straight to the phone",
              "шаг 5 из 6" in screen()["text"] and "Телефон клиента:" in screen()["text"])
        check("a skipped price recaps as 'пока не известна', not a stale warning",
              "Оценка:" not in screen()["text"])

        # "+380" — ровно то, что остаётся, если отправить телефон, не
        # дописав его после кода страны; normalize_phone трактует это как
        # «ничего не ввели» (см. её docstring).
        await qa.repair_got_phone(say("+380"), state)
        check("a bad phone answer warns WITHOUT losing the question",
              screen()["text"].startswith("⚠️") and "Телефон клиента:" in screen()["text"])

        await qa.repair_got_phone(say("0501234567"), state)
        check("the phone is normalized and step 6 asks for the name",
              "+380501234567" in screen()["text"] and "шаг 6 из 6" in screen()["text"] and "Имя клиента:" in screen()["text"])

        await qa.repair_got_name(say("Вася <дома>"), state)
        card = screen()
        check("the confirm card carries its accept/cancel buttons", card["markup"] is not None)
        check("the confirm card is a PHOTO message (Павел: должна быть с фото) with the full recap",
              all(part in card["text"] for part in ("iPhone 12", "Не включается", "+380501234567"))
              and "пока не известна" in card["text"])
        check("the confirm card escapes the staff-typed name too", "&lt;дома&gt;" in card["text"])
        check("the reply that produced the card is gone from the chat", chat.staff_messages == [])
        check("the confirm card is the last message in the chat", chat.last()["id"] == card["id"])

        await qa.repair_confirm_fallback(say("а что дальше?"), state)
        check("a stray message on the confirm card does NOT replace it — the card is untouched",
              any(m["id"] == card["id"] and m["markup"] is not None for m in chat.log))
        check("...a separate reminder is posted instead, pointing back at the card",
              chat.last()["id"] != card["id"] and "Нажмите" in chat.last()["text"])

        # Бросить диалог на середине и начать другой: и брошенный экран, И
        # тот самый reminder (потому и трекается через user_message_ids,
        # не остаётся сиротой) не должны остаться висеть в чате.
        await qa.client_start(say(qa.BTN_CLIENT), state)
        await qa.client_menu_add(tap("client_menu_add"), state)
        check("starting another flow wipes both the abandoned card and its reminder",
              len(chat.bot_messages) == 1)
        check("the new flow starts at its own step 1 of 2", "шаг 1 из 2" in screen()["text"])

    try:
        asyncio.run(run())
    finally:
        qa._resolve_staff_for_dm = original_resolve
        qa._shift_is_open = original_shift_is_open
        qa.get_store = original_get_store
        auth.get_staff_by_telegram_id = original_get_staff

    # Скупка ветвится: «на запчасти» вообще не спрашивает цену продажи,
    # поэтому этот шаг не должен попадать в знаменатель.
    parts = qa._flow_screen("BuybackIntake:photo", {"purpose": "parts"}, "📷 Пришлите фото устройства:")
    check("buyback 'на запчасти' doesn't count the resale-price step it will never ask", "шаг 8 из 8" in parts)
    resale = qa._flow_screen("BuybackIntake:photo", {"purpose": "resale"}, "📷 Пришлите фото устройства:")
    check("buyback 'на продажу' counts all nine steps", "шаг 9 из 9" in resale)
    confirm = qa._flow_screen("RepairIntake:confirm", {"client_name": "Вася"}, "📋 Проверьте данные:")
    check("a confirm card gets no step header prepended — it renders its own recap",
          confirm == "📋 Проверьте данные:")

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
    token = make_token(42, "7")
    data = read_token(token)
    check("token round-trips to the same staff_id", data is not None and data["staff_id"] == 42)
    check("token carries the store_id it was made with", data is not None and data["store_id"] == "7")
    check("garbage token rejected", read_token("not-a-real-token") is None)
    check("empty token rejected", read_token("") is None)
    # Flip a character in the middle of the signature, not the last one: the
    # last base64 char can encode spare bits that decoding discards, so two
    # different last characters can occasionally decode to the same bytes.
    mid = len(token) // 2
    flipped = "a" if token[mid] != "a" else "b"
    tampered = token[:mid] + flipped + token[mid + 1:]
    check("tampered token rejected", read_token(tampered) is None)

    # Фаза A backward compat: a token minted before store_id existed (no key
    # in the payload at all) must still read back, with a default store_id
    # filled in — nobody already logged in should get kicked out by the upgrade.
    legacy_token = _session_token._serializer.dumps({"staff_id": 99})
    legacy_data = read_token(legacy_token)
    check(
        "a pre-Фаза-A token (no store_id in payload) still authenticates, defaulted to the default store",
        legacy_data is not None and legacy_data["staff_id"] == 99 and legacy_data["store_id"] == stores.default_store_id(),
    )


def scenario_stores_config() -> None:
    print("scenario: точки registry (core/stores.py over the locations table)")
    with tempfile.TemporaryDirectory() as tmp:
        prev = os.environ["CRM_DB_PATH"]
        try:
            os.environ["CRM_DB_PATH"] = os.path.join(tmp, "never-created.sqlite3")
            bare = stores.load_stores()
            check("a base that doesn't exist yet still yields exactly one точка (so default_store_id() always answers)",
                  len(bare) == 1 and bare[0].db_path == os.environ["CRM_DB_PATH"])
        finally:
            os.environ["CRM_DB_PATH"] = prev

        with _separate_base(tmp, "registry.sqlite3", ("Первый", "Второй")) as db_path:
            with get_conn(db_path) as conn:
                conn.execute(
                    "UPDATE locations SET staff_group_chat_id = -100, repair_topic_id = 5, masters_group_chat_id = -200 WHERE id = 1"
                )
            parsed = stores.load_stores()
            check("two точки in the base -> two stores", len(parsed) == 2)
            check("every store points at the one shared base", {s.db_path for s in parsed} == {db_path})
            check("store ids are the location ids, as strings (what tokens and callbacks carry)",
                  [s.id for s in parsed] == ["1", "2"] and parsed[1].location_id == 2)
            check("optional group-chat fields are None when not set", parsed[1].staff_group_chat_id is None)
            check("get_store finds a known id", stores.get_store("2").name == "Второй")
            check("default_store_id() is the first точка", stores.default_store_id() == "1")
            try:
                stores.get_store("nope")
                check("get_store raises on an unknown id", False)
            except KeyError:
                check("get_store raises on an unknown id", True)

            check("store_for_chat_id resolves the staff group to its store", stores.store_for_chat_id(-100).id == "1")
            check("store_for_chat_id resolves the masters group to its store too", stores.store_for_chat_id(-200).id == "1")
            check("a topic id is not a chat_id — resolves to nothing", stores.store_for_chat_id(5) is None)
            check("an unrecognized chat_id resolves to no store", stores.store_for_chat_id(-999) is None)
            check("store_for_chat_id accepts a string chat_id too (Telegram hands either)",
                  stores.store_for_chat_id("-100").id == "1")
            check("a non-numeric chat_id resolves to no store, doesn't raise", stores.store_for_chat_id("not-a-number") is None)

            with get_conn(db_path) as conn:
                warehouses = conn.execute("SELECT kind, location_id FROM warehouses ORDER BY id").fetchall()
            check("every точка has its own 'point' склад and there is exactly one «В пути»",
                  sorted((w["kind"], w["location_id"]) for w in warehouses if w["kind"] == "point") == [("point", 1), ("point", 2)]
                  and sum(1 for w in warehouses if w["kind"] == "transit") == 1)


def scenario_storage_context() -> None:
    print("scenario: per-request db path via contextvar (core.storage)")
    with tempfile.TemporaryDirectory() as tmp:
        path_a = os.path.join(tmp, "a.sqlite3")
        path_b = os.path.join(tmp, "b.sqlite3")
        init_db(path_a)
        init_db(path_b)

        with get_conn(path_a) as conn:
            settings_row = conn.execute("SELECT * FROM store_settings WHERE id = 1").fetchone()
        check(
            "init_db seeds the store_settings singleton row (Кабинет магазина schema, no UI yet)",
            settings_row is not None and settings_row["name"] == "Магазин",
        )

        token = storage.set_current_db_path(path_a)
        try:
            with storage.get_conn() as conn:
                auth.create_staff(conn, "a-owner", "pass", "A", "owner")
        finally:
            storage.reset_current_db_path(token)

        token2 = storage.set_current_db_path(path_b)
        try:
            with storage.get_conn() as conn:
                check(
                    "switching the contextvar to db b sees an empty staff table (isolation from db a)",
                    auth.get_staff_by_login(conn, "a-owner") is None,
                )
        finally:
            storage.reset_current_db_path(token2)

        with storage.get_conn(path_a) as conn:
            check(
                "the staff row written while the contextvar pointed at db a is actually there",
                auth.get_staff_by_login(conn, "a-owner") is not None,
            )
        check(
            "an explicit db_path argument always wins over whatever the contextvar holds",
            storage._current_db_path.get() != path_a,
        )


def scenario_store_prefs() -> None:
    print("scenario: last-used-store preference (core.store_prefs)")
    with tempfile.TemporaryDirectory() as tmp:
        prefs_db = os.path.join(tmp, "prefs.sqlite3")
        store_prefs.init_db(prefs_db)

        check("an unknown telegram_id has no preference yet", store_prefs.get_last_store(999, prefs_db) is None)

        store_prefs.set_last_store(111, "2", prefs_db)
        check("the preference just set round-trips", store_prefs.get_last_store(111, prefs_db) == "2")

        store_prefs.set_last_store(111, "3", prefs_db)
        check("setting again overwrites, doesn't duplicate", store_prefs.get_last_store(111, prefs_db) == "3")
        with get_conn(prefs_db) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM store_prefs WHERE telegram_id = 111").fetchone()["n"]
        check("still exactly one row for this telegram_id after overwriting", count == 1)


def scenario_store_access() -> None:
    print("scenario: which точки a Telegram user may work in (core.store_access)")
    prev_prefs = os.environ.get("CRM_STORE_PREFS_PATH")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "access.sqlite3", ("Один", "Два", "Три")) as db_path:
        os.environ["CRM_STORE_PREFS_PATH"] = os.path.join(tmp, "prefs.sqlite3")
        store_prefs.init_db()
        try:
            with get_conn(db_path) as conn:
                owner_id = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
                auth.link_staff_telegram(conn, "owner", 777)
                master_id = auth.create_staff(conn, "somemaster", "pass", "Мастер Два", "master", location_id=2)
                auth.link_staff_telegram(conn, "somemaster", 888)
                homeless_id = auth.create_staff(conn, "newbie", "pass", "Без точки", "storekeeper")
                auth.link_staff_telegram(conn, "newbie", 999)

            found = store_access.accessible_stores(777)
            check("an owner can work in every точка", {s.id for s, _ in found} == {"1", "2", "3"})
            check("and is the SAME staff row in each — one base, one identity",
                  {st["id"] for _, st in found} == {owner_id})
            master_found = store_access.accessible_stores(888)
            check("a master is tied to their own точка only",
                  [(s.id, st["id"]) for s, st in master_found] == [("2", master_id)])
            check("an employee with no точка set lands on the first one",
                  [(s.id, st["id"]) for s, st in store_access.accessible_stores(999)] == [("1", homeless_id)])
            check("a telegram_id that isn't staff finds nothing", store_access.accessible_stores(4242) == [])

            check("pick_default_store with a single match just returns it",
                  store_access.pick_default_store(888, master_found).id == "2")
            check("pick_default_store with no preference recorded yet falls back to the first точка",
                  store_access.pick_default_store(777, found).id == "1")
            store_prefs.set_last_store(777, "3")
            check("pick_default_store honors a recorded preference among the accessible точки",
                  store_access.pick_default_store(777, found).id == "3")
            store_prefs.set_last_store(888, "3")
            check("a stale preference pointing at a точка this person can't work in is ignored",
                  store_access.pick_default_store(888, master_found).id == "2")
        finally:
            if prev_prefs is None:
                os.environ.pop("CRM_STORE_PREFS_PATH", None)
            else:
                os.environ["CRM_STORE_PREFS_PATH"] = prev_prefs


def scenario_store_settings(db_path: str) -> None:
    print("scenario: Кабинет магазина (core.store_settings)")
    with get_conn(db_path) as conn:
        seeded = store_settings.get_settings(conn)
        check("a freshly init'd db has the default seeded name", seeded["name"] == "Магазин")
        check("optional fields start out empty", seeded["address"] is None and seeded["phone"] is None)

        store_settings.update_settings(conn, "  Ремонт-Плюс  ", "  ул. Ленина, 1  ", "+380501112233", "9:00–19:00")
        updated = store_settings.get_settings(conn)
        check("update_settings trims whitespace off the name", updated["name"] == "Ремонт-Плюс")
        check("address/phone/hours are all stored", updated["address"] == "ул. Ленина, 1" and updated["phone"] == "+380501112233" and updated["working_hours"] == "9:00–19:00")

        store_settings.update_settings(conn, "Снова Магазин", "", "", "")
        cleared = store_settings.get_settings(conn)
        check("blanking optional fields stores NULL, not an empty string", cleared["address"] is None and cleared["phone"] is None and cleared["working_hours"] is None)


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
            "client_name": "Тест", "device_type_0": "Смартфон", "channel": "offline",
        })
        check("missing client_phone: no raw 422", resp.status_code != 422)
        check("missing client_phone: friendly error shown", "Заполните имя и телефон клиента" in resp.text)

        # master_id/price_estimate submitted empty (exactly what an unset <select>/<input> sends).
        resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Реальный Клиент", "client_phone": "+380501234567",
            "device_type_0": "Ноутбук", "model_0": "ThinkPad X1", "defect_description_0": "Не включается",
            "channel": "offline", "master_id": "", "price_estimate_0": "",
        }, files={"photo_0": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")})
        check("empty master_id/price_estimate: repair created (303)", resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))

        # Multi-device intake: one client, two devices in the same visit
        # ("+" Добавить ещё устройство) — each becomes its own repair order.
        multi_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Два Устройства", "client_phone": "+380501234599", "channel": "offline",
            "device_count": "2",
            "device_type_0": "Смартфон", "brand_0": "Apple", "model_0": "iPhone 11", "defect_description_0": "Треснул экран",
            "device_type_1": "Ноутбук", "brand_1": "Dell", "model_1": "XPS 13", "defect_description_1": "Не держит батарея",
        }, files={
            "photo_0": ("device0.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg"),
            "photo_1": ("device1.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg"),
        })
        check("multi-device intake redirects to a repair (303)",
              multi_resp.status_code == 303 or (multi_resp.history and multi_resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            two_device_client_id = conn.execute(
                "SELECT id FROM clients WHERE phone = '+380501234599'"
            ).fetchone()["id"]
            two_device_repairs = repairs.list_repairs_by_client(conn, two_device_client_id)
        check("both devices from one intake became separate repair orders", len(two_device_repairs) == 2)
        check("both device types were recorded", {r["device_type"] for r in two_device_repairs} == {"Смартфон", "Ноутбук"})

        # A device row with something filled in but no device_type is
        # rejected rather than silently dropped or crashing.
        incomplete_row_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Неполная Строка", "client_phone": "+380501234588", "channel": "offline",
            "device_count": "1", "brand_0": "Apple",
        })
        check("a device row with no device_type is rejected with a friendly error",
              "Укажите тип устройства" in incomplete_row_resp.text)

        # Regression guard: model, defect description and a device photo
        # are now required on intake, not just device_type.
        no_model_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Без Модели", "client_phone": "+380501234511", "channel": "offline",
            "device_count": "1", "device_type_0": "Смартфон", "defect_description_0": "Не включается",
        }, files={"photo_0": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")})
        check("a device row with no model is rejected with a friendly error",
              "Укажите модель устройства" in no_model_resp.text)

        no_defect_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Без Описания", "client_phone": "+380501234522", "channel": "offline",
            "device_count": "1", "device_type_0": "Смартфон", "model_0": "Galaxy S21",
        }, files={"photo_0": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")})
        check("a device row with no defect description is rejected with a friendly error",
              "Опишите неисправность" in no_defect_resp.text)

        no_photo_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Без Фото", "client_phone": "+380501234533", "channel": "offline",
            "device_count": "1", "device_type_0": "Смартфон", "model_0": "Galaxy S21",
            "defect_description_0": "Не включается",
        })
        check("a device row with no photo is rejected with a friendly error",
              "Загрузите фото устройства" in no_photo_resp.text)

        # Submitting with every device row left blank is rejected too,
        # not a silent no-op.
        no_devices_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Без Устройств", "client_phone": "+380501234577", "channel": "offline",
            "device_count": "1",
        })
        check("submitting with no devices at all shows a friendly error",
              "Добавьте хотя бы одно устройство" in no_devices_resp.text)

        resp = client.post(f"/clients?t={token}", data={"name": "", "phone": "+380501111111"})
        check("missing client name: no raw 422", resp.status_code != 422)
        check("missing client name: friendly error shown", "Введите имя клиента" in resp.text)

        # Телефон здесь необязателен — но «введён и не разобран» должно
        # отличаться от «не введён», иначе опечатка тихо сохраняет клиента
        # вообще без номера (04.09).
        resp = client.post(f"/clients?t={token}", data={"name": "Опечатка Тест", "phone": "не помню"})
        check("a typed-but-unparseable phone is reported, not silently dropped",
              resp.status_code != 422 and "Проверьте номер телефона" in resp.text)
        with get_conn(db_path) as conn:
            check("...and no client was created from that rejected submission",
                  conn.execute("SELECT COUNT(*) c FROM clients WHERE name = 'Опечатка Тест'").fetchone()["c"] == 0)

        resp = client.post(f"/clients?t={token}", data={"name": "Без Телефона", "phone": ""})
        check("a blank optional phone still creates the client (303)",
              resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))
        resp = client.post(f"/clients?t={token}", data={"name": "Со Скобками", "phone": "+38 (050) 765-43-21"})
        check("a number typed with spaces/brackets/dashes is accepted (303)",
              resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            stored = conn.execute("SELECT phone FROM clients WHERE name = 'Со Скобками'").fetchone()
            check("...and stored in canonical form", stored["phone"] == "+380507654321")
            blank = conn.execute("SELECT phone FROM clients WHERE name = 'Без Телефона'").fetchone()
            check("a client with no phone stores NULL, not an empty string", blank["phone"] is None)

        with get_conn(db_path) as conn:
            edit_target = conn.execute("SELECT id FROM clients WHERE name = 'Без Телефона'").fetchone()["id"]
        bad_edit = client.post(
            f"/clients/{edit_target}/edit?t={token}", data={"name": "Опечатка Правка", "phone": "звонить маме"}
        )
        check("editing a client with an unparseable phone is reported too",
              bad_edit.status_code != 422 and "Проверьте номер телефона" in bad_edit.text)

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

        # Supplier-return form, end to end over real HTTP: product page ->
        # receive stock -> return part of it to a supplier -> product page
        # reflects the drop and lists the return in its history.
        with get_conn(db_path) as conn:
            return_test_supplier = purchases.create_supplier(conn, "HTTP Тест Поставщик", None)
            return_test_product = inventory.create_product(conn, "HTTP Тест Деталь", None, None, "шт", True, False, min_qty=0, price=None)
            return_test_cell = inventory.create_cell(conn, "HTTP-C1", None, None)
            inventory.receive_stock(conn, return_test_product, return_test_cell, 5, staff_id)

        detail_resp = client.get(f"/inventory/products/{return_test_product}?t={token}")
        check("product card renders the supplier purchase-history section", "Поставщики этого товара" in detail_resp.text)

        resp = client.post(f"/inventory/products/{return_test_product}/supplier-return?t={token}", data={
            "supplier_id": "", "cell_id": str(return_test_cell), "qty": "1",
        })
        check("missing supplier on return: no raw 422", resp.status_code != 422)
        check("missing supplier on return: friendly error shown", "Выберите поставщика" in resp.text)

        resp = client.post(f"/inventory/products/{return_test_product}/supplier-return?t={token}", data={
            "supplier_id": str(return_test_supplier), "cell_id": str(return_test_cell), "qty": "999", "reason": "Брак",
        })
        check("returning more than in stock: friendly error, not a raw exception",
              resp.status_code != 500 and "Недостаточно товара" in resp.text)

        resp = client.post(f"/inventory/products/{return_test_product}/supplier-return?t={token}", data={
            "supplier_id": str(return_test_supplier), "cell_id": str(return_test_cell), "qty": "2", "reason": "Брак",
        })
        check("a valid return redirects back to the product card (303)",
              resp.status_code == 303 or (resp.history and resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            check("stock actually dropped by the returned qty", inventory.product_total_qty(conn, return_test_product) == 3)
        check("the product card now shows the return in its history", "HTTP Тест Поставщик" in resp.text)

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

        # Cross-entity scanner, now on Склад (18.08 on Ещё, barcodes for
        # products + moved to Склад 19.08) — /warehouse/find tries every
        # known code kind and jumps straight to wherever that thing
        # lives, unlike /clients/find which only ever recognizes a
        # client QR code.
        barcode_product_resp = client.post(f"/inventory/products?t={token}", data={"name": "Штрихкод Товар", "sku": "BARCODE-PROD-1", "unit": "шт", "min_qty": "0", "price": "500"})
        barcode_product_id = int(str(barcode_product_resp.url).split("/inventory/products/")[1].split("?")[0])

        product_barcode_resp = client.get(f"/inventory/products/{barcode_product_id}/barcode.png?t={token}")
        check("product barcode.png returns 200", product_barcode_resp.status_code == 200)
        check("product barcode.png has image content-type", product_barcode_resp.headers.get("content-type", "").startswith("image/"))
        check("product barcode.png body is a real PNG", product_barcode_resp.content[:8] == b"\x89PNG\r\n\x1a\n")

        product_card_resp = client.get(f"/inventory/products/{barcode_product_id}?t={token}")
        check("the product card shows its own barcode", f"/inventory/products/{barcode_product_id}/barcode.png" in product_card_resp.text)

        # Tap-to-flip print view (19.08, Xprinter XP-420B 30x20mm labels).
        check("the product card renders the flip-to-print barcode card",
              'id="barcodeFlip"' in product_card_resp.text and 'id="printAgentBtn"' in product_card_resp.text)
        check("the product card embeds a compact (big-price) barcode for the print-only area",
              f"/inventory/products/{barcode_product_id}/barcode.png?compact=1" in product_card_resp.text)

        compact_barcode_resp = client.get(f"/inventory/products/{barcode_product_id}/barcode.png?t={token}&compact=1")
        check("compact barcode.png returns 200", compact_barcode_resp.status_code == 200)
        check("compact barcode.png body is a real PNG", compact_barcode_resp.content[:8] == b"\x89PNG\r\n\x1a\n")

        # Print queue (19.08) — the CRM server can't reach a printer
        # behind Павел's router directly, so "Отправить на печать"
        # enqueues a job that print_agent.py (running on his LAN) polls
        # for, fetches the label from, and acks.
        enqueue_resp = client.post(f"/inventory/products/{barcode_product_id}/print-label?t={token}")
        check("enqueuing a print job succeeds and returns a job id",
              enqueue_resp.status_code == 200 and enqueue_resp.json()["ok"] is True)
        print_job_id = enqueue_resp.json()["job_id"]

        status_resp = client.get(f"/inventory/print-jobs/{print_job_id}/status?t={token}")
        check("a fresh job starts out pending", status_resp.json() == {"ok": True, "status": "pending"})

        import os as _os  # noqa: E402 -- PRINT_AGENT_TOKEN is read at import time in webapp.routers.print_agent
        agent_token = _os.environ.get("PRINT_AGENT_TOKEN")

        no_token_resp = client.get(f"/print-agent/jobs")
        check("the print agent endpoint rejects a request with no token", no_token_resp.status_code == 403)

        wrong_token_resp = client.get(f"/print-agent/jobs?token=not-the-real-token")
        check("the print agent endpoint rejects a request with the wrong token", wrong_token_resp.status_code == 403)

        if agent_token:
            jobs_resp = client.get(f"/print-agent/jobs?token={agent_token}")
            check("the agent can list pending jobs with the right token",
                  jobs_resp.status_code == 200 and any(j["id"] == print_job_id for j in jobs_resp.json()["jobs"]))

            label_resp = client.get(f"/print-agent/jobs/{print_job_id}/label.png?token={agent_token}")
            check("the agent can fetch the compact label PNG for its job",
                  label_resp.status_code == 200 and label_resp.content[:8] == b"\x89PNG\r\n\x1a\n")

            ack_resp = client.post(f"/print-agent/jobs/{print_job_id}/ack?token={agent_token}&ok=true")
            check("the agent can ack a job as printed", ack_resp.status_code == 200 and ack_resp.json()["ok"] is True)

            after_ack_status = client.get(f"/inventory/print-jobs/{print_job_id}/status?t={token}")
            check("staff polling sees the job flip to printed after the agent acks it",
                  after_ack_status.json() == {"ok": True, "status": "printed"})

            no_longer_pending_resp = client.get(f"/print-agent/jobs?token={agent_token}")
            check("an acked job no longer shows up as pending for the agent",
                  print_job_id not in [j["id"] for j in no_longer_pending_resp.json()["jobs"]])
        else:
            print("  (skipped agent-token checks — PRINT_AGENT_TOKEN not set in this shell)")

        no_sku_resp = client.post(f"/inventory/products?t={token}", data={"name": "Товар Без SKU", "unit": "шт", "min_qty": "0"})
        no_sku_product_id = int(str(no_sku_resp.url).split("/inventory/products/")[1].split("?")[0])
        no_sku_card_resp = client.get(f"/inventory/products/{no_sku_product_id}?t={token}")
        check("a product with no SKU shows a hint instead of a broken barcode image",
              "нет SKU" in no_sku_card_resp.text and f"/inventory/products/{no_sku_product_id}/barcode.png" not in no_sku_card_resp.text)
        no_sku_barcode_resp = client.get(f"/inventory/products/{no_sku_product_id}/barcode.png?t={token}")
        check("requesting a barcode for a product with no SKU 404s instead of crashing", no_sku_barcode_resp.status_code == 404)

        no_sku_enqueue_resp = client.post(f"/inventory/products/{no_sku_product_id}/print-label?t={token}")
        check("enqueuing a print job for a product with no SKU is rejected, not silently queued",
              no_sku_enqueue_resp.status_code == 400 and no_sku_enqueue_resp.json()["ok"] is False)

        find_product_resp = client.get(f"/warehouse/find?t={token}&code=BARCODE-PROD-1")
        check("scanning a product's barcode on Склад jumps straight to that product's card",
              f"/inventory/products/{barcode_product_id}" in str(find_product_resp.url))

        find_client_resp = client.get(f"/warehouse/find?t={token}&code=CRMCID:{client_id}")
        check("the same Склад scanner also recognizes a client QR code",
              f"/clients/{client_id}" in str(find_client_resp.url))

        find_garbage_resp = client.get(f"/warehouse/find?t={token}&code=not-a-real-code")
        check("an unrecognized code on the Склад scanner: no raw error, friendly message",
              find_garbage_resp.status_code != 422 and "не распознан" in find_garbage_resp.text)

        warehouse_resp = client.get(f"/warehouse?t={token}")
        check("Склад renders the photo scanner button", 'scanPhotoBtn' in warehouse_resp.text and '/warehouse/find' in warehouse_resp.text)
        check("Ещё no longer carries the scanner (moved to Склад)",
              'scanPhotoBtn' not in client.get(f"/more?t={token}").text)

        # Settings — язык интерфейса (19.08, full app coverage 21.08).
        more_for_settings_resp = client.get(f"/more?t={token}")
        check("Ещё shows a Настройки card linking to /settings",
              "/settings" in more_for_settings_resp.text)

        settings_resp = client.get(f"/settings?t={token}")
        check("settings page offers both language options",
              "Русский" in settings_resp.text and "Українська" in settings_resp.text)

        dash_before_lang = client.get(f"/?t={token}")
        check("dashboard defaults to Russian nav labels", "Ремонты" in dash_before_lang.text)

        set_lang_resp = client.post(f"/settings/language?t={token}", data={"language": "uk"})
        check("switching language redirects back to settings",
              set_lang_resp.status_code == 303 or (set_lang_resp.history and set_lang_resp.history[0].status_code == 303))

        dash_after_lang = client.get(f"/?t={token}")
        check("after switching to uk, the tabbar shows Ukrainian labels instead",
              "Ремонти" in dash_after_lang.text and "Ремонты" not in dash_after_lang.text)

        bad_lang_resp = client.post(f"/settings/language?t={token}", data={"language": "en"})
        check("an unknown language code is ignored, not stored",
              bad_lang_resp.status_code == 303 or (bad_lang_resp.history and bad_lang_resp.history[0].status_code == 303))
        check("the previous (uk) choice is still in effect after the rejected value",
              "Ремонти" in client.get(f"/?t={token}").text)

        # 21.08 — the rest of the app (Продажи, Склад, Приход, Клиенты,
        # Отчёты, Касса) got its i18n pass too; spot-check one distinctive
        # uk string per page (not just the shared tabbar) with the ru
        # equivalent absent, same shape as the tabbar check above.
        uk_sales = client.get(f"/sales?t={token}")
        check("Продажи: uk page shows a translated heading, not the ru one",
              "Новий продаж" in uk_sales.text and "Новая продажа" not in uk_sales.text)

        uk_warehouse = client.get(f"/warehouse?t={token}")
        check("Склад hub: uk nav card titles, not ru",
              "Товари" in uk_warehouse.text and "Товары" not in uk_warehouse.text)

        uk_products = client.get(f"/inventory/products?t={token}")
        check("Товары: uk add-product heading, not ru",
              "Додати товар" in uk_products.text and "Добавить товар" not in uk_products.text)

        uk_cells = client.get(f"/inventory/cells?t={token}")
        check("Ячейки: uk page title, not ru", "Комірки" in uk_cells.text and "Ячейки" not in uk_cells.text)

        uk_purchases = client.get(f"/purchases?t={token}")
        check("Приход: uk heading, not ru",
              "Новий прихід" in uk_purchases.text and "Новый приход" not in uk_purchases.text)

        uk_clients = client.get(f"/clients?t={token}")
        check("Клиенты: uk add-client heading, not ru",
              "Додати клієнта" in uk_clients.text and "Добавить клиента" not in uk_clients.text)

        uk_reports = client.get(f"/reports?t={token}")
        check("Отчёты: uk section heading, not ru",
              "Ремонти за статусами" in uk_reports.text and "Ремонты по статусам" not in uk_reports.text)

        uk_cash = client.get(f"/cash?t={token}")
        check("Касса: uk balance label, not ru",
              "готівка в касі зараз" in uk_cash.text and "наличка в кассе сейчас" not in uk_cash.text)

        # reset — later checks in this same scenario assume Russian text
        client.post(f"/settings/language?t={token}", data={"language": "ru"})

        # Photo-based scan (19.08, replaced live getUserMedia+ZXing camera
        # streaming, which was crashing/hanging Telegram Desktop's
        # sandboxed WebKitGTK renderer) — snap a photo of a barcode,
        # OpenAI vision reads the SKU off it, same contract as the other
        # scan-to-X endpoints.
        scan_photo_resp = client.post(
            f"/warehouse/scan-photo?t={token}",
            files={"photo": ("barcode.jpg", b"fake-bytes", "image/jpeg")},
        )
        check("POST /warehouse/scan-photo without an API key returns a structured error, not a 500",
              scan_photo_resp.status_code == 502 and scan_photo_resp.json()["ok"] is False)

        # Fast local decode (zbar, core.barcode_scan): a real barcode —
        # one of our own printed labels — decodes without ever touching
        # OpenAI, so this must succeed even with no API key configured.
        real_barcode_png = barcode_label.generate_label_png(sku="4600000000012", name="Тестовая Деталь", price=100)
        real_scan_resp = client.post(
            f"/warehouse/scan-photo?t={token}",
            files={"photo": ("barcode.png", real_barcode_png, "image/png")},
        )
        check("a real barcode photo decodes instantly via zbar, no OpenAI key needed",
              real_scan_resp.status_code == 200
              and real_scan_resp.json() == {"ok": True, "sku": "4600000000012"})

        oversized_scan_photo_resp = client.post(
            f"/warehouse/scan-photo?t={token}",
            files={"photo": ("huge.jpg", b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("POST /warehouse/scan-photo rejects an oversized photo before ever calling OpenAI",
              oversized_scan_photo_resp.status_code == 413 and oversized_scan_photo_resp.json()["ok"] is False)

        # Physical USB/Bluetooth scanner support (19.08) — a keyboard-
        # wedge scanner just "types" into whatever field has focus, so a
        # plain GET form hitting the same /warehouse/find the camera
        # scanner uses is all that's needed; no JS, no new backend logic.
        check("Склад renders a plain text field for a physical scanner, not just the camera button",
              'name="code"' in warehouse_resp.text and 'action="/warehouse/find"' in warehouse_resp.text)

        hw_scan_resp = client.get(f"/warehouse/find?t={token}&code=BARCODE-PROD-1")
        check("a physical scanner's input (a plain GET with code=) resolves exactly like a camera scan",
              f"/inventory/products/{barcode_product_id}" in str(hw_scan_resp.url))

        clients_list_resp = client.get(f"/clients?t={token}")
        check("the clients list is now a card grid, not a table", 'class="cards"' in clients_list_resp.text)
        check("a client's card links to their detail page", f"/clients/{client_id}" in clients_list_resp.text)

        # Онлайн/офлайн filter buttons (all clients so far in this scenario
        # are offline; add one online client to prove the filter actually
        # excludes rows, not just renders the buttons).
        with get_conn(db_path) as conn:
            online_client_id = clients.create_client(conn, "Онлайн Клиент", phone="+380990003344", source="online")

        offline_filter_resp = client.get(f"/clients?t={token}&source=offline")
        check("«Офлайн» filter excludes the online client", f"/clients/{online_client_id}" not in offline_filter_resp.text)
        check("«Офлайн» filter still shows an offline client", f"/clients/{client_id}" in offline_filter_resp.text)

        online_filter_resp = client.get(f"/clients?t={token}&source=online")
        check("«Онлайн» filter shows only the online client", f"/clients/{online_client_id}" in online_filter_resp.text
              and f"/clients/{client_id}" not in online_filter_resp.text)

        # Device catalog autocomplete: seeded suggestions render, and a
        # brand-new device typed on intake gets remembered for next time.
        resp = client.get(f"/repairs?t={token}")
        check("repairs page renders the device type datalist", 'id="deviceTypeList"' in resp.text)
        check("repairs page embeds a known seeded brand", '"Apple"' in resp.text)
        check("repairs intake form has the dynamic add-device button, not a fixed single device",
              'id="addDeviceRowBtn"' in resp.text and 'repair-device-rows.js' in resp.text)
        check("repairs intake form accepts a file upload (multipart, not urlencoded)",
              'enctype="multipart/form-data"' in resp.text)

        # App-wide double-submit guard (18.08 — a laggy save + a second
        # tap duplicated a repair order): loaded on every page via
        # base.html, not just the repairs intake form.
        check("every page loads the double-submit guard, not just repairs intake",
              'double-submit-guard.js' in resp.text)

        # tel: links don't work in Telegram's own in-app WebView (a
        # documented Telegram bug, both platforms) — this app-wide script
        # (19.08) reroutes them through window.open() as the workaround,
        # loaded on every page the same way as the double-submit guard.
        check("every page loads the tel: link fix", 'tel-link-fix.js' in resp.text)

        catalog_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Каталог Тест", "client_phone": "+380990002233",
            "device_type_0": "Экзотика", "brand_0": "НовыйБренд", "model_0": "СуперМодель X",
            "defect_description_0": "Не заряжается", "channel": "offline",
        }, files={"photo_0": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")})
        with get_conn(db_path) as conn:
            learned = device_catalog.list_all(conn)
        check("a brand-new device typed on intake is remembered in the catalog",
              any(r["brand"] == "НовыйБренд" and r["model"] == "СуперМодель X" for r in learned))

        # Photo attached at intake time (a real <input type=file> on
        # repairs_list.html, not the separate post-hoc /photo endpoint) —
        # regression guard for 18.08: the photo used to only be addable
        # AFTER the card had already gone out to the groups; it must now
        # be on record from the moment the repair (and its card) exists.
        intake_photo_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "С Фото На Приёме", "client_phone": "+380990002244",
            "device_count": "1", "device_type_0": "Смартфон", "model_0": "Galaxy S21",
            "defect_description_0": "Не включается", "channel": "offline",
        }, files={"photo_0": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")})
        intake_photo_order_id = int(str(intake_photo_resp.url).split("/repairs/")[1].split("?")[0])
        with get_conn(db_path) as conn:
            intake_photo_path = repairs.get_repair(conn, intake_photo_order_id)["device_photo_path"]
        check("a photo attached on the intake form is on record immediately, not after a follow-up trip",
              bool(intake_photo_path) and intake_photo_path.endswith(".jpg"))
        intake_photo_file = os.path.join("webapp", "static", "device_photos", intake_photo_path or "")
        check("the intake photo file was actually written to disk", os.path.exists(intake_photo_file))
        if os.path.exists(intake_photo_file):
            os.remove(intake_photo_file)

        bad_intake_photo_resp = client.post(f"/repairs?t={token}", data={
            "client_name": "Плохое Фото", "client_phone": "+380990002255",
            "device_count": "1", "device_type_0": "Смартфон", "model_0": "Galaxy S21",
            "defect_description_0": "Не включается", "channel": "offline",
        }, files={"photo_0": ("note.txt", b"not an image", "text/plain")})
        check("an invalid photo on the intake form is rejected with a friendly error, no repair created",
              "Фото устройства должно быть" in bad_intake_photo_resp.text)
        with get_conn(db_path) as conn:
            bad_photo_client = conn.execute("SELECT id FROM clients WHERE phone = '+380990002255'").fetchone()
        check("rejecting the intake photo didn't leave a half-created client behind", bad_photo_client is None)

        # Device photo upload mirrors the product-photo endpoint exactly
        # (see 18.08 fix) — same size/type validation, same JSON shape, so
        # the shared photo-upload.js works unchanged for repairs.
        catalog_order_id = int(str(catalog_resp.url).split("/repairs/")[1].split("?")[0])

        bad_device_photo_resp = client.post(
            f"/repairs/{catalog_order_id}/photo?t={token}",
            files={"photo": ("note.txt", b"not an image", "text/plain")},
        )
        check("uploading a non-image device photo is rejected with a friendly JSON error",
              bad_device_photo_resp.status_code == 400 and bad_device_photo_resp.json()["ok"] is False)

        oversized_device_resp = client.post(
            f"/repairs/{catalog_order_id}/photo?t={token}",
            files={"photo": ("huge.jpg", b"\xff\xd8\xff" + b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("an oversized device photo is rejected with a friendly JSON error",
              oversized_device_resp.status_code == 413 and oversized_device_resp.json()["ok"] is False)

        good_device_photo_resp = client.post(
            f"/repairs/{catalog_order_id}/photo?t={token}",
            files={"photo": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")},
        )
        good_device_photo_json = good_device_photo_resp.json()
        with get_conn(db_path) as conn:
            device_photo_path = repairs.get_repair(conn, catalog_order_id)["device_photo_path"]
        check("uploading a valid device photo stores a photo_path",
              good_device_photo_json["ok"] is True and bool(device_photo_path) and device_photo_path.endswith(".jpg"))
        check("the JSON response's photo_url matches the stored device photo path",
              device_photo_path in good_device_photo_json["photo_url"])

        repair_card_resp = client.get(f"/repairs/{catalog_order_id}?t={token}")
        check("the repair card now renders the uploaded device photo", device_photo_path in repair_card_resp.text)

        repairs_list_resp = client.get(f"/repairs?t={token}")
        check("the repairs list is now a card grid, not a table", 'class="cards"' in repairs_list_resp.text)

        saved_device_photo_file = os.path.join("webapp", "static", "device_photos", device_photo_path or "")
        check("the uploaded device photo file was actually written to disk",
              device_photo_path is not None and os.path.exists(saved_device_photo_file))
        if device_photo_path and os.path.exists(saved_device_photo_file):
            os.remove(saved_device_photo_file)  # test hygiene — don't leave uploaded test images on disk

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

        # Продажи: "+" dynamic rows (regression guard for 18.08 — checkout
        # used to hard-cap at 3 positions), product search by name/SKU (not
        # a <select>), and a sale can never invent a product that isn't in
        # the catalog the way Приход invents new stock items.
        sales_list_resp = client.get(f"/sales?t={token}")
        check("sales page renders the dynamic add-row button, not a fixed 3-slot form",
              'id="addSaleRowBtn"' in sales_list_resp.text and 'sale-rows.js' in sales_list_resp.text)

        beyond_cap_resp = client.post(f"/sales?t={token}", data={
            "channel": "offline", "row_count": "4",
            "product_id_0": str(wproduct_id), "qty_0": "1", "price_0": "1000",
            "product_id_3": str(wproduct_id), "qty_3": "1", "price_3": "1000",
        })
        check("a 4th row (past the old hardcoded 3-row cap) is still processed",
              beyond_cap_resp.status_code == 303 or (beyond_cap_resp.history and beyond_cap_resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            beyond_cap_order_id = int(str(beyond_cap_resp.url).split("/sales/")[1].split("?")[0])
            beyond_cap_items = sales.get_sale_items(conn, beyond_cap_order_id)
        check("both rows (0 and 3) landed as separate sale items", len(beyond_cap_items) == 2)

        # Phone fields default to "+380" as a typing template (19.08) so
        # staff don't retype the country code — checkout's client contact
        # is optional, so an untouched "+380" (nothing actually typed)
        # must be treated exactly like an empty field, not create a
        # phantom client with a garbage phone number. Own throwaway
        # product/cell/stock here — reusing wproduct_id would consume
        # stock the later "Добавить остаток" test assumes is still there.
        client.post(f"/inventory/products?t={token}", data={"name": "Тест Телефон", "sku": "PHONE-TEST-1", "unit": "шт", "min_qty": "0"})
        client.post(f"/inventory/cells?t={token}", data={"code": "PHONE-TEST-CELL"})
        with get_conn(db_path) as conn:
            phone_test_product_id = conn.execute("SELECT id FROM products WHERE sku = 'PHONE-TEST-1'").fetchone()["id"]
            phone_test_cell_id = conn.execute("SELECT id FROM storage_cells WHERE code = 'PHONE-TEST-CELL'").fetchone()["id"]
            clients_before = conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]
        client.post(f"/inventory/movements/receive?t={token}", data={
            "product_id": str(phone_test_product_id), "cell_id": str(phone_test_cell_id), "qty": "1",
        })
        untouched_phone_resp = client.post(f"/sales?t={token}", data={
            "channel": "offline", "client_phone": "+380",
            "product_id_0": str(phone_test_product_id), "qty_0": "1", "price_0": "1000",
        })
        with get_conn(db_path) as conn:
            clients_after = conn.execute("SELECT COUNT(*) AS n FROM clients").fetchone()["n"]
        check("an untouched '+380' template in checkout doesn't create a phantom client",
              (untouched_phone_resp.status_code == 303 or untouched_phone_resp.history)
              and clients_after == clients_before)

        unresolved_resp = client.post(f"/sales?t={token}", data={
            "channel": "offline",
            "product_name_0": "Товар которого нет в базе", "qty_0": "1", "price_0": "500",
        })
        check("a typed name that never resolved to a real product is rejected with a friendly error, not sold blind",
              "Не найден в каталоге" in unresolved_resp.text)

        empty_resp = client.post(f"/sales?t={token}", data={"channel": "offline"})
        check("submitting the checkout with no items shows a friendly error, not a silent no-op",
              "Добавьте хотя бы один товар" in empty_resp.text)

        # In-app camera scan (purchases_list.html "📷 Сканировать накладную"):
        # no OPENAI_API_KEY in the test env, so this must degrade to a
        # structured JSON error rather than a raw 500 — same contract as
        # every other "external dependency unavailable" path in this app.
        scan_resp = client.post(f"/purchases/scan?t={token}", files={"photo": ("invoice.jpg", b"fake-bytes", "image/jpeg")})
        check("POST /purchases/scan without an API key returns a structured error, not a 500",
              scan_resp.status_code == 502 and "rows" in scan_resp.json() and "error" in scan_resp.json())

        oversized_scan_resp = client.post(
            f"/purchases/scan?t={token}",
            files={"photo": ("huge.jpg", b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("POST /purchases/scan rejects an oversized photo before ever calling OpenAI",
              oversized_scan_resp.status_code == 413 and oversized_scan_resp.json()["rows"] == [])

        # Scan-to-fill SKU button (product create/edit forms).
        label_scan_resp = client.post(
            f"/inventory/products/scan-label?t={token}",
            files={"photo": ("label.jpg", b"fake-bytes", "image/jpeg")},
        )
        check("POST /inventory/products/scan-label without an API key returns a structured error, not a 500",
              label_scan_resp.status_code == 502 and label_scan_resp.json()["ok"] is False)

        real_label_scan_resp = client.post(
            f"/inventory/products/scan-label?t={token}",
            files={"photo": ("label.png", barcode_label.generate_label_png(sku="4600000000029", name="Тест", price=50), "image/png")},
        )
        check("a real barcode photo on the SKU scanner decodes instantly via zbar (sku only, no name)",
              real_label_scan_resp.status_code == 200
              and real_label_scan_resp.json() == {"ok": True, "name": None, "sku": "4600000000029"})

        oversized_label_resp = client.post(
            f"/inventory/products/scan-label?t={token}",
            files={"photo": ("huge.jpg", b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("POST /inventory/products/scan-label rejects an oversized photo before ever calling OpenAI",
              oversized_label_resp.status_code == 413 and oversized_label_resp.json()["ok"] is False)

        # Scan-to-fill device-info button (repair intake, next to Серийный №/IMEI).
        device_scan_resp = client.post(
            f"/repairs/scan-device?t={token}",
            files={"photo": ("device.jpg", b"fake-bytes", "image/jpeg")},
        )
        check("POST /repairs/scan-device without an API key returns a structured error, not a 500",
              device_scan_resp.status_code == 502 and device_scan_resp.json()["ok"] is False)

        oversized_device_scan_resp = client.post(
            f"/repairs/scan-device?t={token}",
            files={"photo": ("huge.jpg", b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("POST /repairs/scan-device rejects an oversized photo before ever calling OpenAI",
              oversized_device_scan_resp.status_code == 413 and oversized_device_scan_resp.json()["ok"] is False)

        check("repairs intake page renders the device scan button", 'device-scan-btn' in repairs_list_resp.text
              and 'repair-device-rows.js' in repairs_list_resp.text)

        # Product card (Склад → Товары → клик на товар): view, edit, photo.
        detail_resp = client.get(f"/inventory/products/{wproduct_id}?t={token}")
        check("product detail page renders", detail_resp.status_code == 200 and "Гарантийный товар" in detail_resp.text)

        client.post(f"/inventory/products/{wproduct_id}/edit?t={token}", data={
            "name": "Гарантийный товар PRO", "sku": "WARR-1", "unit": "шт", "min_qty": "0",
        })
        with get_conn(db_path) as conn:
            renamed = conn.execute("SELECT name FROM products WHERE id = ?", (wproduct_id,)).fetchone()
        check("editing a product from its card updates the name", renamed["name"] == "Гарантийный товар PRO")

        # "Добавить остаток" on the card — stock is per-cell (record_movement's
        # ledger), not a bare number on the product, so there's no direct
        # "set quantity" field; this is the same receive_stock() the full
        # Приход/Склад→Движения forms use, just scoped to one product.
        receive_resp = client.post(f"/inventory/products/{wproduct_id}/receive?t={token}", data={
            "cell_id": str(wcell_id), "qty": "3",
        })
        with get_conn(db_path) as conn:
            new_total = inventory.product_total_qty(conn, wproduct_id)
        check("«Добавить остаток» increases stock via the normal record_movement ledger",
              new_total == 5)  # 5 received + 3 sold earlier in this scenario (1 warranty sale + 2 "+"-row sale), +3 here
        check("«Добавить остаток» redirects back to the product's own card",
              receive_resp.status_code == 200 and "Гарантийный товар PRO" in receive_resp.text)

        missing_receive_resp = client.post(f"/inventory/products/{wproduct_id}/receive?t={token}", data={
            "cell_id": "", "qty": "",
        })
        check("«Добавить остаток» with missing fields shows a friendly error, not a 500",
              missing_receive_resp.status_code == 200 and "Выберите ячейку" in missing_receive_resp.text)

        create_new_product_resp = client.post(f"/inventory/products?t={token}", data={
            "name": "Свежесозданный товар", "unit": "шт", "min_qty": "0",
        })
        check("creating a product redirects straight to its own card, not the list",
              create_new_product_resp.url.path.startswith("/inventory/products/")
              and create_new_product_resp.url.path != "/inventory/products")

        # Photo upload is AJAX (JSON in/out) now, not a plain <form> POST —
        # a native-navigation upload just hangs blank in a WebView when the
        # request fails (e.g. nginx's client_max_body_size on a real phone
        # photo), with no way to show the user what went wrong.
        bad_photo_resp = client.post(
            f"/inventory/products/{wproduct_id}/photo?t={token}",
            files={"photo": ("note.txt", b"not an image", "text/plain")},
        )
        check("uploading a non-image is rejected with a friendly JSON error, not a 500",
              bad_photo_resp.status_code == 400 and bad_photo_resp.json()["ok"] is False
              and "JPEG" in bad_photo_resp.json()["error"])

        oversized_resp = client.post(
            f"/inventory/products/{wproduct_id}/photo?t={token}",
            files={"photo": ("huge.jpg", b"\xff\xd8\xff" + b"x" * (16 * 1024 * 1024), "image/jpeg")},
        )
        check("an oversized photo is rejected with a friendly JSON error, not accepted or hung",
              oversized_resp.status_code == 413 and oversized_resp.json()["ok"] is False)

        good_photo_resp = client.post(
            f"/inventory/products/{wproduct_id}/photo?t={token}",
            files={"photo": ("device.jpg", b"\xff\xd8\xff-fake-jpeg-bytes", "image/jpeg")},
        )
        good_photo_json = good_photo_resp.json()
        with get_conn(db_path) as conn:
            photo_path = conn.execute("SELECT photo_path FROM products WHERE id = ?", (wproduct_id,)).fetchone()["photo_path"]
        check("uploading a valid image stores a photo_path",
              good_photo_json["ok"] is True and bool(photo_path) and photo_path.endswith(".jpg"))
        check("the JSON response's photo_url matches the stored path", photo_path in good_photo_json["photo_url"])

        card_resp = client.get(f"/inventory/products/{wproduct_id}?t={token}")
        check("the product card now renders the uploaded photo", photo_path in card_resp.text)

        saved_photo_file = os.path.join("webapp", "static", "product_photos", photo_path or "")
        check("the uploaded photo file was actually written to disk", photo_path is not None and os.path.exists(saved_photo_file))
        if photo_path and os.path.exists(saved_photo_file):
            os.remove(saved_photo_file)  # test hygiene — don't leave uploaded test images on disk

        # 19.08 — every upload site now runs core.photos.compress_photo,
        # so a phone-camera-sized photo shouldn't be stored anywhere near
        # its original resolution/weight. A real (not fake-bytes) 3000px
        # PNG is the only way to actually exercise that code path, rather
        # than its fallback for undecodable data (the fake-bytes check
        # above).
        big_buf = io.BytesIO()
        Image.new("RGB", (3000, 3000), "red").save(big_buf, format="PNG")
        big_photo_bytes = big_buf.getvalue()
        big_photo_resp = client.post(
            f"/inventory/products/{wproduct_id}/photo?t={token}",
            files={"photo": ("huge-real.png", big_photo_bytes, "image/png")},
        )
        big_photo_path = big_photo_resp.json().get("photo_url", "").rsplit("/", 1)[-1]
        big_saved_file = os.path.join("webapp", "static", "product_photos", big_photo_path or "")
        if os.path.exists(big_saved_file):
            with Image.open(big_saved_file) as saved_im:
                check("a large real photo is downscaled to the cap, not stored at full resolution",
                      max(saved_im.size) <= 1600)
            check("a large real photo is re-encoded as JPEG regardless of the original format",
                  big_saved_file.endswith(".jpg"))
            os.remove(big_saved_file)
        else:
            check("a large real photo is downscaled to the cap, not stored at full resolution", False)

        # Касса, end to end over real HTTP: a priced repair can't be
        # marked "Выдан" without a payment method (friendly error, not a
        # raw 422, and the status transition itself must not go through);
        # picking one both issues the repair AND posts касса income;
        # re-saving the same already-issued repair must not double-charge.
        with get_conn(db_path) as conn:
            cash_client_id = clients.get_or_create_by_phone(conn, "Касса Клиент", "+380501230000", source="offline")
            cash_repair_id = repairs.create_repair(
                conn, cash_client_id, "Смартфон", "Apple", "iPhone 8", None, "не грузится", "offline", None, 800, staff_id,
            )
            repairs.set_price(conn, cash_repair_id, 800, 800)
            balance_before_issue = cash.cash_balance(conn)

        no_method_resp = client.post(f"/repairs/{cash_repair_id}/status?t={token}", data={"status": "issued", "comment": ""})
        check("issuing a priced repair with no payment method: no raw 422", no_method_resp.status_code != 422)
        check("issuing a priced repair with no payment method: friendly error shown",
              "Укажите способ оплаты" in no_method_resp.text)
        with get_conn(db_path) as conn:
            check("the blocked transition left the repair's status untouched",
                  repairs.get_repair(conn, cash_repair_id)["status"] == "new")
        with get_conn(db_path) as conn:
            check("cash balance unchanged when the transition was blocked", cash.cash_balance(conn) == balance_before_issue)

        issue_resp = client.post(f"/repairs/{cash_repair_id}/status?t={token}", data={
            "status": "issued", "comment": "", "payment_method": "cash",
        })
        check("issuing with a payment method redirects (303)",
              issue_resp.status_code == 303 or (issue_resp.history and issue_resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            check("cash balance increased by exactly the repair's price_final",
                  cash.cash_balance(conn) == balance_before_issue + 800)

        client.post(f"/repairs/{cash_repair_id}/status?t={token}", data={
            "status": "issued", "comment": "повторное сохранение", "payment_method": "cash",
        })
        with get_conn(db_path) as conn:
            check("re-submitting an already-issued repair does not double-charge the касса",
                  cash.cash_balance(conn) == balance_before_issue + 800)

        # /cash dashboard + manual expense/adjustment forms.
        dash_resp = client.get(f"/cash?t={token}")
        check("cash dashboard renders for an owner", dash_resp.status_code == 200 and "Касса" in dash_resp.text)

        with get_conn(db_path) as conn:
            master_id = auth.create_staff(conn, "cashmaster", "pass", "Мастер Касса", "master")
        master_token = make_token(master_id)
        denied_resp = client.get(f"/cash?t={master_token}")
        check("a master role is denied the cash dashboard (403), not shown financial data", denied_resp.status_code == 403)

        expense_resp = client.post(f"/cash/expense?t={token}", data={"method": "cash", "amount": "", "category": "rent"})
        check("missing expense amount: no raw 422", expense_resp.status_code != 422)
        check("missing expense amount: friendly error shown", "Укажите сумму расхода" in expense_resp.text)

        with get_conn(db_path) as conn:
            balance_before_expense = cash.cash_balance(conn)
        client.post(f"/cash/expense?t={token}", data={"method": "cash", "amount": "150", "category": "rent", "comment": "аренда"})
        with get_conn(db_path) as conn:
            check("a valid expense over HTTP actually reduces the cash balance",
                  cash.cash_balance(conn) == balance_before_expense - 150)

        adj_resp = client.post(f"/cash/adjustment?t={token}", data={"direction": "in", "amount": "0"})
        check("zero-amount adjustment: friendly error, no raw 422",
              adj_resp.status_code != 422 and "Укажите сумму" in adj_resp.text)

        # Мастера — CRUD over real HTTP, plus role gating (compensation
        # data, same owner/admin-only tier as Касса).
        masters_denied_resp = client.get(f"/masters?t={master_token}")
        check("a master role is denied the Мастера section (403), not shown pay rates",
              masters_denied_resp.status_code == 403)

        masters_list_resp = client.get(f"/masters?t={token}")
        check("Мастера list renders for an owner", masters_list_resp.status_code == 200)

        no_name_resp = client.post(f"/masters?t={token}", data={"name": "", "pay_type": "percent", "pay_value": "40"})
        check("missing master name: no raw 422", no_name_resp.status_code != 422)
        check("missing master name: friendly error shown", "Введите имя мастера" in no_name_resp.text)

        create_resp = client.post(f"/masters?t={token}", data={
            "name": "HTTP Тест Мастер", "telegram_id": "", "pay_type": "percent", "pay_value": "35",
        })
        check("creating a master redirects to their detail page (303)",
              create_resp.status_code == 303 or (create_resp.history and create_resp.history[0].status_code == 303))
        http_master_id = int(str(create_resp.url).split("/masters/")[1].split("?")[0])

        detail_resp = client.get(f"/masters/{http_master_id}?t={token}")
        check("the new master's detail page shows their configured rate",
              "HTTP Тест Мастер" in detail_resp.text and "35" in detail_resp.text)

        edit_resp = client.post(f"/masters/{http_master_id}/edit?t={token}", data={
            "name": "HTTP Тест Мастер Правка", "telegram_id": "", "pay_type": "fixed", "pay_value": "250",
        })
        check("editing a master redirects back to their card (303)",
              edit_resp.status_code == 303 or (edit_resp.history and edit_resp.history[0].status_code == 303))
        with get_conn(db_path) as conn:
            edited = auth.get_master(conn, http_master_id)
        check("the edit actually changed name and pay_type/value",
              edited["name"] == "HTTP Тест Мастер Правка" and edited["pay_type"] == "fixed" and edited["pay_value"] == 250)

        client.post(f"/masters/{http_master_id}/deactivate?t={token}")
        with get_conn(db_path) as conn:
            check("deactivating over HTTP actually flips active, not a hard delete",
                  auth.get_master(conn, http_master_id)["active"] == 0)
        list_after_deactivate = client.get(f"/masters?t={token}")
        check("a deactivated master still shows up in the list (for reactivation), just marked inactive",
              "HTTP Тест Мастер Правка" in list_after_deactivate.text)


def scenario_buyback_http(db_path: str) -> None:
    """Скупка техники у клиентов (24.08) — real HTTP requests, same
    TestClient/token shape as scenario_webapp_forms. purpose='parts' is
    just a log entry + cash expense; purpose='resale' also auto-creates a
    products row (qty=1) so the item is immediately sellable through the
    ordinary Продажи/Инвентарь flow, unchanged — that's the actual
    regression risk here, not the buyback_orders row itself."""
    print("scenario: Скупка (buyback) over real HTTP requests")
    if os.path.exists(db_path):
        os.remove(db_path)
    init_db(db_path)
    with get_conn(db_path) as conn:
        staff_id = auth.create_staff(conn, "buybacktest", "pass", "Скупка Тест", "owner")
    token = make_token(staff_id)

    import webapp.main  # noqa: F401 -- import after CRM_DB_PATH is set for this test

    fake_jpeg = b"\xff\xd8\xff-fake-jpeg-bytes"

    with TestClient(webapp.main.app) as client:
        form_resp = client.get(f"/buyback?t={token}")
        check("GET /buyback renders the intake form", 'id="buybackIntakeForm"' in form_resp.text)

        hub_resp = client.get(f"/warehouse?t={token}")
        check("Склад hub links to /buyback", '/buyback' in hub_resp.text)

        missing_phone_resp = client.post(f"/buyback?t={token}", data={
            "client_name": "Тест", "device_type": "Смартфон", "model": "X",
            "purchase_price": "500", "payment_method": "cash", "purpose": "parts",
        }, files={"photo": ("d.jpg", fake_jpeg, "image/jpeg")})
        check("missing client_phone: friendly error, no raw 422",
              missing_phone_resp.status_code != 422 and "Заполните имя и телефон" in missing_phone_resp.text)

        no_photo_resp = client.post(f"/buyback?t={token}", data={
            "client_name": "Тест", "client_phone": "+380501110001", "device_type": "Смартфон",
            "model": "X", "purchase_price": "500", "payment_method": "cash", "purpose": "parts",
        })
        check("missing photo: friendly error, no repair created",
              "Загрузите фото устройства" in no_photo_resp.text)

        missing_resale_price_resp = client.post(f"/buyback?t={token}", data={
            "client_name": "Без Цены", "client_phone": "+380501110002", "device_type": "Смартфон",
            "model": "X", "purchase_price": "500", "payment_method": "cash", "purpose": "resale",
        }, files={"photo": ("d.jpg", fake_jpeg, "image/jpeg")})
        check("purpose=resale without resale_price: friendly error",
              "Укажите цену продажи" in missing_resale_price_resp.text)

        parts_resp = client.post(f"/buyback?t={token}", data={
            "client_name": "Продавец Запчасти", "client_phone": "+380501110003", "device_type": "Смартфон",
            "model": "iPhone X", "purchase_price": "1000", "payment_method": "cash", "purpose": "parts",
        }, files={"photo": ("d.jpg", fake_jpeg, "image/jpeg")}, follow_redirects=False)
        check("purpose=parts intake redirects (303)", parts_resp.status_code == 303)
        parts_order_id = int(parts_resp.headers["location"].split("/buyback/")[1].split("?")[0])

        with get_conn(db_path) as conn:
            parts_order = buyback.get_buyback_order(conn, parts_order_id)
            check("parts order has no linked product", parts_order["product_id"] is None)
            cash_row = conn.execute(
                "SELECT * FROM cash_transactions WHERE ref_type='buyback_order' AND ref_id=?", (parts_order_id,)
            ).fetchone()
            check("parts intake recorded a cash expense for the exact amount paid",
                  cash_row is not None and cash_row["kind"] == "expense" and cash_row["amount"] == 1000
                  and cash_row["category"] == "buyback")

        resale_resp = client.post(f"/buyback?t={token}", data={
            "client_name": "Продавец Резейл", "client_phone": "+380501110004", "device_type": "Смартфон",
            "brand": "Apple", "model": "iPhone 12", "purchase_price": "3000", "payment_method": "card",
            "purpose": "resale", "resale_price": "5000",
        }, files={"photo": ("d.jpg", fake_jpeg, "image/jpeg")}, follow_redirects=False)
        check("purpose=resale intake redirects (303)", resale_resp.status_code == 303)
        resale_order_id = int(resale_resp.headers["location"].split("/buyback/")[1].split("?")[0])

        with get_conn(db_path) as conn:
            resale_order = buyback.get_buyback_order(conn, resale_order_id)
            check("resale order got a linked product", bool(resale_order["product_id"]))
            product = inventory.get_product(conn, resale_order["product_id"])
            check("the auto-created product carries the resale price", product["price"] == 5000)
            check("the auto-created product's photo is the buyback photo", bool(product["photo_path"]))
            check("the auto-created product name mentions «Б/У»", "Б/У" in product["name"])
            check("the auto-created product is sellable", product["is_sellable"] == 1)
            total_qty = inventory.product_total_qty(conn, resale_order["product_id"])
            check("the auto-created product has exactly qty=1 in stock", total_qty == 1)

        product_page_resp = client.get(f"/inventory/products/{resale_order['product_id']}?t={token}")
        check("the auto-created product's own card renders normally (real Склад flow, untouched)",
              product_page_resp.status_code == 200 and "iPhone 12" in product_page_resp.text)

        detail_resp = client.get(f"/buyback/{resale_order_id}?t={token}")
        check("buyback detail page renders and links to the product",
              detail_resp.status_code == 200 and f"/inventory/products/{resale_order['product_id']}" in detail_resp.text)

        filtered_resp = client.get(f"/buyback?t={token}&purpose=resale")
        check("purpose filter shows the resale order but not the parts one",
              "Продавец Резейл" in filtered_resp.text and "Продавец Запчасти" not in filtered_resp.text)

    # Photos land on real disk (webapp/static/buyback_photos) regardless of
    # which sqlite file db_path points at — same reason
    # scenario_webapp_forms cleans up intake_photo_file for repairs.
    for filename in (parts_order["photo_path"], resale_order["photo_path"]):
        path = os.path.join(buyback.PHOTO_DIR, filename)
        if os.path.exists(path):
            os.remove(path)

    if os.path.exists(db_path):
        os.remove(db_path)


def scenario_multi_store_http() -> None:
    """Two точки in ONE base behind the same running process: what is per-
    точка (ремонты, касса, склад) must follow the token's точка, what is
    shared (клиенты, мастера, каталог) must be the same from both."""
    print("scenario: two точки, one base — per-точка vs shared data over real HTTP requests")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "two.sqlite3", ("Точка A", "Точка B")) as db_path:
        with get_conn(db_path) as conn:
            owner_id = auth.create_staff(conn, "two-owner", "pass", "Владелец", "owner")
            auth.create_master(conn, "Уникальный Мастер Альфа", None, "fixed", 100, location_id=1)
            client_id = clients.get_or_create_by_phone(conn, "Клиент Общий", "+380501119900", source="offline")
            repairs.create_repair(
                conn, client_id, "Смартфон", "Xiaomi", "УникальныйРемонтA", None, "не грузится", "offline", None, None,
                owner_id, location_id=1,
            )
            cash.record_adjustment(conn, 777, "размен", owner_id, location_id=2)

        import webapp.main  # already imported by scenario_webapp_forms; re-import is a cached no-op

        with TestClient(webapp.main.app) as client:
            token_a = make_token(owner_id, "1")
            token_b = make_token(owner_id, "2")

            check("точка A's repair is in точка A's list", "УникальныйРемонтA" in client.get(f"/repairs?t={token_a}").text)
            check("and NOT in точка B's list — repairs belong to the точка that took them in",
                  "УникальныйРемонтA" not in client.get(f"/repairs?t={token_b}").text)
            check("masters are one list for the whole business — visible from both точки",
                  "Уникальный Мастер Альфа" in client.get(f"/masters?t={token_a}").text
                  and "Уникальный Мастер Альфа" in client.get(f"/masters?t={token_b}").text)
            check("clients too — one контрагент base", "Клиент Общий" in client.get(f"/clients?t={token_b}").text)
            cash_a = client.get(f"/cash?t={token_a}").text
            cash_b = client.get(f"/cash?t={token_b}").text
            check("each точка has its own касса balance", ">777<" in cash_b and ">777<" not in cash_a)
            check("the appbar names the точка the token is for",
                  "Точка A" in client.get(f"/?t={token_a}").text and "Точка B" in client.get(f"/?t={token_b}").text)


def scenario_multi_store_login_and_switch_http() -> None:
    """An owner works in every точка: login must land somewhere sensible,
    and switching must be remembered for the next login — exercised
    through the real /miniapp/auto and /store/switch endpoints."""
    print("scenario: login + точка switcher over real HTTP requests")
    prev_prefs = os.environ.get("CRM_STORE_PREFS_PATH")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "login.sqlite3", ("Кастомное Имя A", "Вторая Точка")) as db_path:
        telegram_id = 555000111
        master_telegram_id = 555000222
        with get_conn(db_path) as conn:
            auth.create_staff(conn, "loginowner", "pass", "Мультивладелец", "owner")
            auth.link_staff_telegram(conn, "loginowner", telegram_id)
            auth.create_staff(conn, "loginmaster", "pass", "Мастер Второй", "master", location_id=2)
            auth.link_staff_telegram(conn, "loginmaster", master_telegram_id)

        os.environ["CRM_STORE_PREFS_PATH"] = os.path.join(tmp, "login-prefs.sqlite3")
        try:
            import webapp.main
            from webapp.routers import miniapp as miniapp_router

            init_data = _build_init_data(miniapp_router.BOT_TOKEN, {"id": telegram_id, "first_name": "Тест"}, int(time.time()))
            master_init_data = _build_init_data(
                miniapp_router.BOT_TOKEN, {"id": master_telegram_id, "first_name": "Мастер"}, int(time.time()))

            with TestClient(webapp.main.app) as client:
                # follow_redirects=False: the store_id we need to inspect only
                # lives in the Location header of the redirect itself.
                first_login = client.post("/miniapp/auto", data={"initData": init_data}, follow_redirects=False)
                token_after_first_login = first_login.headers["location"].split("t=", 1)[1]
                check("first-ever login (no preference yet) lands in the first точка",
                      first_login.status_code == 303 and read_token(token_after_first_login)["store_id"] == "1")
                dash = client.get(f"/?t={token_after_first_login}")
                check("that token actually authenticates", dash.status_code == 200)

                switch_page = client.get(f"/store/switch?t={token_after_first_login}")
                check("the switcher lists every точка by its Кабинет name",
                      "Кастомное Имя A" in switch_page.text and "Вторая Точка" in switch_page.text)
                check("«Ещё» offers the switcher to someone with more than one точка",
                      "/store/switch" in client.get(f"/more?t={token_after_first_login}").text)

                switch = client.post("/store/switch?t=" + token_after_first_login, data={"store_id": "2"}, follow_redirects=False)
                check("switching to точка 2 redirects (303)", switch.status_code == 303)
                token_after_switch = switch.headers["location"].split("t=", 1)[1]
                check("the new token really is for точка 2, same person",
                      read_token(token_after_switch)["store_id"] == "2"
                      and read_token(token_after_switch)["staff_id"] == read_token(token_after_first_login)["staff_id"])

                second_login = client.post("/miniapp/auto", data={"initData": init_data}, follow_redirects=False)
                token_after_second_login = second_login.headers["location"].split("t=", 1)[1]
                check("a second login (after switching) lands back on точка 2 — the switch was remembered",
                      client.get(f"/?t={token_after_second_login}").status_code == 200
                      and read_token(token_after_second_login)["store_id"] == "2")

                bogus_switch = client.post("/store/switch?t=" + token_after_second_login, data={"store_id": "does-not-exist"})
                check("switching to a точка that doesn't exist doesn't crash, shows a friendly error",
                      bogus_switch.status_code == 200 and "нет доступа" in bogus_switch.text)

                master_login = client.post("/miniapp/auto", data={"initData": master_init_data}, follow_redirects=False)
                master_token = master_login.headers["location"].split("t=", 1)[1]
                check("a master logs straight into their own точка", read_token(master_token)["store_id"] == "2")
                check("and is not offered the switcher at all", "/store/switch" not in client.get(f"/more?t={master_token}").text)
                denied = client.post("/store/switch?t=" + master_token, data={"store_id": "1"})
                check("nor can they switch to another точка by hand",
                      denied.status_code == 200 and "нет доступа" in denied.text)
        finally:
            if prev_prefs is None:
                os.environ.pop("CRM_STORE_PREFS_PATH", None)
            else:
                os.environ["CRM_STORE_PREFS_PATH"] = prev_prefs


def scenario_store_settings_http() -> None:
    """Кабинет магазина over real HTTP — role gate + save round-trip, and
    the save touching only the точка it was made in."""
    print("scenario: Кабинет магазина over HTTP (role gate + save)")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "cabinet.sqlite3", ("Магазин", "Соседняя")) as db_path:
        with get_conn(db_path) as conn:
            owner_id = auth.create_staff(conn, "cabowner", "pass", "Кабинет Владелец", "owner")
            master_id = auth.create_staff(conn, "cabmaster", "pass", "Кабинет Мастер", "master")
        owner_token = make_token(owner_id, "1")
        master_token = make_token(master_id, "1")

        import webapp.main

        with TestClient(webapp.main.app) as client:
            master_get = client.get(f"/store/settings?t={master_token}")
            check("a master is denied the Кабинет магазина (403), not shown store settings",
                  master_get.status_code == 403)

            owner_get = client.get(f"/store/settings?t={owner_token}")
            check("an owner sees the current name on first visit", "Магазин" in owner_get.text)

            empty_name = client.post(f"/store/settings?t={owner_token}", data={
                "name": "   ", "address": "", "phone": "", "working_hours": "",
            })
            check("an empty name is rejected with a friendly error, no raw 422",
                  empty_name.status_code == 200 and "не может быть пустым" in empty_name.text)
            with get_conn(db_path) as conn:
                check("the rejected empty-name save left the store's real name untouched",
                      store_settings.get_settings(conn, 1)["name"] == "Магазин")

            saved = client.post(f"/store/settings?t={owner_token}", data={
                "name": "Ремонтная Мастерская №1", "address": "просп. Мира, 10",
                "phone": "+380671234567", "working_hours": "пн–сб 9:00–20:00",
            })
            check("a valid save succeeds (200, re-rendered with a success flash)", saved.status_code == 200)
            check("the success flash is shown", "Изменения сохранены" in saved.text)

            reload = client.get(f"/store/settings?t={owner_token}")
            check("the saved name is really persisted, visible on a fresh GET", "Ремонтная Мастерская №1" in reload.text)
            check("the saved address is really persisted too", "просп. Мира, 10" in reload.text)
            with get_conn(db_path) as conn:
                check("the other точка's profile was not touched by it",
                      store_settings.get_settings(conn, 2)["name"] == "Соседняя"
                      and store_settings.get_settings(conn, 2)["address"] is None)


def scenario_all_stores_report_http() -> None:
    """«Все магазины» summary over real HTTP — two точки seeded with
    deliberately DIFFERENT numbers on every metric (no shared values), so a
    bug that reads the wrong точка or sums wrong shows up as a wrong number
    rather than accidentally passing."""
    print("scenario: сводный отчёт «Все магазины» over HTTP")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "all.sqlite3", ("Магазин Alpha", "Магазин Bravo")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
            master_b = auth.create_staff(conn, "master", "pass", "Мастер Б", "master", location_id=2)

            # Точка A: 1 open repair, 1 issued @1000, 1 sale (qty2*100=200),
            # cash 500, 1 low-stock product (of the 3 in the shared catalog,
            # only "Товар A" has stock anywhere — at B — see below).
            client_id = clients.get_or_create_by_phone(conn, "Клиент A", "+380501110001", source="offline")
            open_order = repairs.create_repair(
                conn, client_id, "Смартфон", "Xiaomi", "Redmi", None, "не грузится", "offline", None, None, owner,
                location_id=1,
            )
            repairs.update_status(conn, open_order, "in_progress", owner)
            issued_order = repairs.create_repair(
                conn, client_id, "Ноутбук", "Dell", "XPS", None, "разбит экран", "offline", None, 1000, owner,
                location_id=1,
            )
            repairs.set_price(conn, issued_order, price_estimate=1000, price_final=1000)
            repairs.update_status(conn, issued_order, "issued", owner)
            cell_a = inventory.create_cell(conn, "ALL-A", None, None, location_id=1)
            cell_b = inventory.create_cell(conn, "ALL-B", None, None, location_id=2)
            # The catalog is shared: 3 products, min_qty 3 each. Stock is
            # per точка: A holds 5 of two of them (1 low at A), B holds 5
            # of one (2 low at B).
            product_1 = inventory.create_product(conn, "Товар 1", "SKU-ALL-1", None, "шт", False, True, 3, 100)
            product_2 = inventory.create_product(conn, "Товар 2", "SKU-ALL-2", None, "шт", False, True, 3, 50)
            product_3 = inventory.create_product(conn, "Товар 3", "SKU-ALL-3", None, "шт", False, True, 3, 300)
            inventory.receive_stock(conn, product_1, cell_a, 7, owner)
            inventory.receive_stock(conn, product_2, cell_a, 5, owner)
            inventory.receive_stock(conn, product_3, cell_b, 9, owner)
            sales.create_sale(conn, client_id, "offline", owner, [(product_1, 2, 100)], location_id=1)
            cash.record_adjustment(conn, 300, None, owner, location_id=1)   # 200 from the sale + 300 = 500

            # Точка B: 2 open repairs, 1 issued @3000, 2 sales (150+300=450),
            # cash 1200 — every number deliberately different from A's.
            client_b = clients.get_or_create_by_phone(conn, "Клиент B", "+380501110002", source="offline")
            for _ in range(2):
                oid = repairs.create_repair(
                    conn, client_b, "Смартфон", "Samsung", "A54", None, "не заряжается", "offline", None, None, owner,
                    location_id=2,
                )
                repairs.update_status(conn, oid, "in_progress", owner)
            issued_order_b = repairs.create_repair(
                conn, client_b, "Планшет", "Apple", "iPad", None, "треснул экран", "offline", None, 3000, owner,
                location_id=2,
            )
            repairs.set_price(conn, issued_order_b, price_estimate=3000, price_final=3000)
            repairs.update_status(conn, issued_order_b, "issued", owner)
            sales.create_sale(conn, client_b, "offline", owner, [(product_3, 3, 50)], location_id=2)
            sales.create_sale(conn, client_b, "offline", owner, [(product_3, 1, 300)], location_id=2)
            cash.record_adjustment(conn, 750, None, owner, location_id=2)   # 450 from sales + 750 = 1200

        # Its own connection, and the error escapes the `with`: get_conn()
        # only commits on a clean exit, so the half-made sale rolls back
        # instead of leaving an order row behind.
        try:
            with get_conn(db_path) as conn:
                sales.create_sale(conn, client_b, "offline", owner, [(product_1, 1, 100)], location_id=2)
            check("a sale at точка B can't take stock that is physically at точка A", False)
        except inventory.InsufficientStockError:
            check("a sale at точка B can't take stock that is physically at точка A", True)

        import webapp.main

        with TestClient(webapp.main.app) as client:
            owner_token = make_token(owner, "1")
            master_token = make_token(master_b, "2")

            master_resp = client.get(f"/reports/all-stores?t={master_token}")
            check("a master role is denied «Все магазины» (403), not shown cross-store financials",
                  master_resp.status_code == 403)

            resp = client.get(f"/reports/all-stores?t={owner_token}")
            text = resp.text
            check("the page loads for an owner", resp.status_code == 200)
            check("both точки appear by name", "Магазин Alpha" in text and "Магазин Bravo" in text)

            # Precise check, not just substring presence: every
            # <div class="stat-num"> in document order — A's 6 stats, then
            # B's 6, then the totals' 6 (template order: open_repairs,
            # sales_orders, low_stock_count, repairs_revenue,
            # sales_revenue, cash_balance).
            stat_nums = re.findall(r'<div class="stat-num">([^<]*)</div>', text)
            expected = [
                "1", "1", "1", "1000", "200", "500",       # точка A (low: Товар 3 — none of it here)
                "2", "2", "2", "3000", "450", "1200",      # точка B (low: Товар 1 and 2)
                "3", "3", "3", "4000", "650", "1700",      # итого — exact sum of both
            ]
            check("all 18 stat values (2 точки + totals, 6 each) appear in the exact expected order",
                  stat_nums == expected)


def scenario_sales_channel_http() -> None:
    """Витрина в канале (02.10): publish a product card from the product
    page, the card follows price edits, and a sale that empties the stock
    takes it down — delete first, ПРОДАНО stub when Telegram refuses the
    delete. Plus the «Купить» deep link's lead logic (bot/channel_orders).
    Telegram itself is faked at httpx.post, same as scenario_repair_card_notify."""
    print("scenario: sales channel — publish, auto-update, auto-remove on sale, buy lead")
    from bot import channel_orders
    from core import channel_posts, notify

    calls = []
    state = {"delete_ok": True, "send_ok": True, "next_id": 700}

    class _Resp:
        def __init__(self, ok, result=None):
            self.status_code = 200 if ok else 400
            self.text = "ok" if ok else "Bad Request"
            self._result = result or {}

        def json(self):
            return {"result": self._result}

    def _fake_post(url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[1]
        calls.append({"method": method, **(json or data or {}), "has_file": bool(files)})
        if method == "getMe":
            return _Resp(True, {"username": "shop_test_bot"})
        if method == "deleteMessage":
            return _Resp(state["delete_ok"])
        if method in ("sendPhoto", "sendMessage"):
            state["next_id"] += 1
            return _Resp(state["send_ok"], {"message_id": state["next_id"]})
        return _Resp(True)

    def _methods():
        return [c["method"] for c in calls]

    check("a pasted t.me link / bare name / @name all normalize to @username",
          {store_settings.normalize_sales_channel(v) for v in ("https://t.me/my_shop", "my_shop", "@my_shop", " t.me/my_shop/ ")} == {"@my_shop"})
    check("a private channel's numeric id is kept as-is, empty turns publishing off",
          store_settings.normalize_sales_channel("-1001234567890") == "-1001234567890"
          and store_settings.normalize_sales_channel("  ") is None)
    check("buy payload round-trips, including a store id with an underscore",
          channel_posts.parse_buy_payload(channel_posts.buy_payload(42, "shop_2")) == (42, "shop_2")
          and channel_posts.parse_buy_payload("buy_x_1") is None and channel_posts.parse_buy_payload("other") is None)

    orig_post, orig_token, orig_username = httpx.post, notify._BOT_TOKEN, notify._bot_username
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "channel.sqlite3", ("Канал-тест",)) as db_path:
        with get_conn(db_path) as conn:
            owner_id = auth.create_staff(conn, "chowner", "pass", "Канал Владелец", "owner")
            auth.link_staff_telegram(conn, "chowner", 9001)
            master_id = auth.create_staff(conn, "chmaster", "pass", "Канал Мастер", "master")
            cell_id = inventory.create_cell(conn, "V-01", None, None)
            phone_id = inventory.create_product(conn, "iPhone 16 Pro Max <256>", None, "Телефоны", "шт", False, True, 0, 52000)
            inventory.receive_stock(conn, phone_id, cell_id, 1, owner_id)
            case_id = inventory.create_product(conn, "Чехол", None, None, "шт", False, True, 0, 300)
            inventory.receive_stock(conn, case_id, cell_id, 3, owner_id)
            old_id = inventory.create_product(conn, "iPad Air", None, None, "шт", False, True, 0, 20000)
            inventory.receive_stock(conn, old_id, cell_id, 1, owner_id)
            noprice_id = inventory.create_product(conn, "Без цены", None, None, "шт", False, True, 0, None)
            inventory.receive_stock(conn, noprice_id, cell_id, 1, owner_id)
        owner_token = make_token(owner_id, "1")
        master_token = make_token(master_id, "1")

        httpx.post = _fake_post
        notify._BOT_TOKEN, notify._bot_username = "test-token", None
        try:
            import webapp.main

            with TestClient(webapp.main.app) as client:
                page = client.get(f"/inventory/products/{phone_id}?t={owner_token}")
                check("with no channel configured the product card points to Кабинет магазина, no publish button",
                      "Канал продаж не указан" in page.text and "/channel/publish" not in page.text)
                denied = client.post(f"/inventory/products/{phone_id}/channel/publish?t={owner_token}", data={"description": "x"})
                check("publishing with no channel configured is a friendly error, nothing sent",
                      "Канал продаж не указан" in denied.text and not calls)

                client.post(f"/store/settings?t={owner_token}", data={
                    "name": "Магазин 007", "address": "ул. Дерибасовская, 1", "phone": "+380501112233",
                    "working_hours": "", "sales_channel": "https://t.me/shop_channel",
                })
                with get_conn(db_path) as conn:
                    check("Кабинет магазина saves the channel, normalized to @username",
                          store_settings.get_settings(conn)["sales_channel"] == "@shop_channel")

                forbidden = client.post(f"/inventory/products/{phone_id}/channel/publish?t={master_token}", data={"description": "x"})
                check("a master can't publish to the channel (403)", forbidden.status_code == 403)

                no_price = client.post(f"/inventory/products/{noprice_id}/channel/publish?t={owner_token}", data={"description": ""})
                check("a product without a price is refused with a clear message", "Укажите цену" in no_price.text and "sendMessage" not in _methods())

                state["send_ok"] = False
                failed = client.post(f"/inventory/products/{phone_id}/channel/publish?t={owner_token}",
                                     data={"description": "Идеал, АКБ 100% <новый>"})
                state["send_ok"] = True
                with get_conn(db_path) as conn:
                    check("a Telegram refusal (bot not admin) shows a hint, records no post, but keeps the typed description",
                          "администратором" in failed.text and channel_posts.get_active_post(conn, phone_id) is None
                          and inventory.get_product(conn, phone_id)["description"] == "Идеал, АКБ 100% <новый>")

                calls.clear()
                client.post(f"/inventory/products/{phone_id}/channel/publish?t={owner_token}",
                            data={"description": "Идеал, АКБ 100% <новый>"})
                sent = next((c for c in calls if c["method"] == "sendMessage"), None)
                with get_conn(db_path) as conn:
                    post = channel_posts.get_active_post(conn, phone_id)
                check("publish sends one card to the store's channel and records it",
                      sent is not None and sent["chat_id"] == "@shop_channel" and post is not None
                      and post["message_id"] == state["next_id"] and post["chat_id"] == "@shop_channel")
                check("the card carries name, description, price and shop contacts, HTML-escaped",
                      sent is not None and "iPhone 16 Pro Max &lt;256&gt;" in sent["text"] and "&lt;новый&gt;" in sent["text"]
                      and "52\u00a0000\u00a0грн" in sent["text"] and "<blockquote>" in sent["text"] and "Дерибасовская" in sent["text"] and "+380501112233" in sent["text"])
                button = sent["reply_markup"]["inline_keyboard"][0][0] if sent else {}
                check("the «Купить» button deep-links into the bot with this product and store",
                      button.get("url") == f"https://t.me/shop_test_bot?start=buy_{phone_id}_1")
                check("spec-style description lines get a bold label, plain lines don't",
                      channel_posts._description_block("Состояние: идеал\n\nПросто текст\nОчень длинная фраза без смысла метки тут: хвост")
                      == "<blockquote><b>Состояние:</b> идеал\nПросто текст\nОчень длинная фраза без смысла метки тут: хвост</blockquote>"
                      and channel_posts._description_block("  ") == "")
                check("post_url builds a public t.me link to the card",
                      channel_posts.post_url(post) == f"https://t.me/shop_channel/{post['message_id']}")

                page = client.get(f"/inventory/products/{phone_id}?t={owner_token}")
                check("the product card now shows the live post with a «Снять с канала» action",
                      "/channel/unpublish" in page.text and f"t.me/shop_channel/{post['message_id']}" in page.text)
                again = client.post(f"/inventory/products/{phone_id}/channel/publish?t={owner_token}", data={"description": "x"})
                check("a second publish of the same product is refused, no duplicate card", "уже выставлен" in again.text)

                # ---- price edit follows through to the live card
                calls.clear()
                client.post(f"/inventory/products/{phone_id}/edit?t={owner_token}", data={
                    "name": "iPhone 16 Pro Max <256>", "sku": "", "category": "Телефоны", "unit": "шт",
                    "min_qty": "0", "price": "49900", "is_sellable": "on",
                })
                edit = next((c for c in calls if c["method"] == "editMessageText"), None)
                check("editing the price edits the channel card in place, button kept",
                      edit is not None and "49\u00a0900\u00a0грн" in edit["text"] and edit["message_id"] == post["message_id"]
                      and edit["reply_markup"]["inline_keyboard"][0][0]["url"].endswith(f"buy_{phone_id}_1"))
                calls.clear()
                client.post(f"/inventory/products/{phone_id}/edit?t={owner_token}", data={
                    "name": "iPhone 16 Pro Max <256>", "sku": "", "category": "Телефоны", "unit": "шт",
                    "min_qty": "0", "price": "49900", "is_sellable": "on",
                })
                check("saving the form with nothing changed makes no Telegram call", not calls)

                # ---- buy leads (bot side, pure function)
                reply, lead, store, managers = channel_orders.process_buy_request(
                    f"buy_{phone_id}_1", 777, "Вася <Пупкин>", "vasya")
                check("a first «Купить» tap confirms to the customer and produces a staff lead with a profile link",
                      "Заявка принята" in reply and lead is not None and "tg://user?id=777" in lead
                      and "@vasya" in lead and "Вася &lt;Пупкин&gt;" in lead and "49\u00a0900\u00a0грн" in lead)
                check("the lead resolves the right store and its owner as fallback recipient",
                      store is not None and store.id == "1" and managers == [9001])
                check("with no staff group the lead goes to the owner's DM; with one — to its sales topic, or General if none",
                      channel_orders.lead_destinations(store, managers) == [(9001, None)]
                      and channel_orders.lead_destinations(
                          stores.StoreConfig("9", "X", db_path, staff_group_chat_id=-100500, sales_topic_id=121), managers
                      ) == [(-100500, 121)]
                      and channel_orders.lead_destinations(
                          stores.StoreConfig("9", "X", db_path, staff_group_chat_id=-100500), managers
                      ) == [(-100500, None)])
                reply2, lead2, _s, _m = channel_orders.process_buy_request(f"buy_{phone_id}_1", 777, "Вася", "vasya")
                check("a repeat tap by the same person re-confirms but doesn't notify staff twice",
                      "Заявка принята" in reply2 and lead2 is None)
                reply3, _l, _s, _m = channel_orders.process_buy_request(f"buy_{phone_id}_1", 778, "Без Юзернейма", None)
                check("a customer without @username is told to contact the shop, with its phone",
                      "не указано имя пользователя" in reply3 and "+380501112233" in reply3)
                bad = channel_orders.process_buy_request("buy_999999_1", 1, "X", None)
                bad_store = channel_orders.process_buy_request("buy_1_nosuchstore", 1, "X", None)
                check("an unknown product or store is a polite not-found, no lead",
                      bad[0] == channel_orders.NOT_FOUND_TEXT and bad[1] is None
                      and bad_store[0] == channel_orders.NOT_FOUND_TEXT and bad_store[1] is None)

                # ---- a partial sale keeps the card, the last unit removes it
                client.post(f"/inventory/products/{case_id}/channel/publish?t={owner_token}", data={"description": ""})
                calls.clear()
                client.post(f"/sales?t={owner_token}", data={
                    "row_count": "1", "product_id_0": str(case_id), "qty_0": "2", "price_0": "300",
                    "channel": "offline", "payment_method": "cash",
                })
                with get_conn(db_path) as conn:
                    check("selling 2 of 3 leaves the card up (still in stock), no Telegram call",
                          channel_posts.get_active_post(conn, case_id) is not None and not calls)

                calls.clear()
                client.post(f"/sales?t={owner_token}", data={
                    "row_count": "1", "product_id_0": str(phone_id), "qty_0": "1", "price_0": "49900",
                    "channel": "offline", "payment_method": "cash",
                })
                deleted = next((c for c in calls if c["method"] == "deleteMessage"), None)
                with get_conn(db_path) as conn:
                    row = conn.execute("SELECT * FROM channel_posts WHERE product_id = ?", (phone_id,)).fetchone()
                    check("selling the last unit deletes the card from the channel",
                          deleted is not None and deleted["message_id"] == post["message_id"]
                          and row["status"] == "removed" and row["closed_at"] is not None
                          and channel_posts.get_active_post(conn, phone_id) is None)
                sold_reply, sold_lead, _s, _m = channel_orders.process_buy_request(f"buy_{phone_id}_1", 779, "Опоздавший", "late")
                check("a «Купить» tap on an already-sold product says so, no lead",
                      sold_reply == channel_orders.SOLD_OUT_TEXT and sold_lead is None)

                # ---- delete refused (post older than 48h) -> ПРОДАНО stub
                client.post(f"/inventory/products/{old_id}/channel/publish?t={owner_token}", data={"description": "как новый"})
                state["delete_ok"] = False
                calls.clear()
                client.post(f"/sales?t={owner_token}", data={
                    "row_count": "1", "product_id_0": str(old_id), "qty_0": "1", "price_0": "20000",
                    "channel": "offline", "payment_method": "cash",
                })
                stub = next((c for c in calls if c["method"] == "editMessageText"), None)
                with get_conn(db_path) as conn:
                    row = conn.execute("SELECT * FROM channel_posts WHERE product_id = ?", (old_id,)).fetchone()
                check("when Telegram refuses the delete, the card becomes a ПРОДАНО stub with the button removed",
                      _methods()[:2] == ["deleteMessage", "editMessageText"] and stub is not None
                      and "ПРОДАНО" in stub["text"] and "000" not in stub["text"]
                      and stub["reply_markup"] == {"inline_keyboard": []} and row["status"] == "sold")
                state["delete_ok"] = True

                # ---- manual take-down
                calls.clear()
                client.post(f"/inventory/products/{case_id}/channel/unpublish?t={owner_token}")
                with get_conn(db_path) as conn:
                    check("«Снять с канала» deletes the card while the product stays in stock",
                          "deleteMessage" in _methods() and channel_posts.get_active_post(conn, case_id) is None
                          and inventory.product_total_qty(conn, case_id) == 1)
                page = client.get(f"/inventory/products/{case_id}?t={owner_token}")
                check("after take-down the product can be published again", "/channel/publish" in page.text)

            # ---- photo card: sendPhoto + caption edits
            photo_name = "channel_test_photo.jpg"
            photo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webapp", "static", "product_photos")
            os.makedirs(photo_dir, exist_ok=True)
            photo_path = os.path.join(photo_dir, photo_name)
            buf = io.BytesIO()
            Image.new("RGB", (40, 40), "white").save(buf, format="JPEG")
            with open(photo_path, "wb") as f:
                f.write(buf.getvalue())
            try:
                with get_conn(db_path) as conn:
                    inventory.set_product_photo(conn, case_id, photo_name)
                    calls.clear()
                    channel_posts.publish(conn, case_id, "1", owner_id)
                    photo_post = channel_posts.get_active_post(conn, case_id)
                    check("a product with a photo is posted as one photo message with the card as caption",
                          _methods() == ["sendPhoto"] and calls[0]["has_file"] and "Чехол" in calls[0]["caption"]
                          and photo_post["has_photo"] == 1)
                    inventory.set_product_description(conn, case_id, "силикон")
                    calls.clear()
                    channel_posts.sync_product(conn, case_id, "1")
                    check("a photo card is updated through editMessageCaption, not editMessageText",
                          _methods() == ["editMessageCaption"] and "силикон" in calls[0]["caption"])
            finally:
                os.remove(photo_path)
        finally:
            httpx.post = orig_post
            notify._BOT_TOKEN, notify._bot_username = orig_token, orig_username


def scenario_documents_journal() -> None:
    """core.documents / core.doc_cancel: every operation leaves a journal
    row with the right number, точка and author; a repeat with the same
    key creates nothing; a cancel reverses stock and money and keeps the
    row."""
    print("scenario: единый журнал документов — регистрация, идемпотентность, отмена")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "journal.sqlite3", ("Первая", "Вторая")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "j-owner", "pass", "Журнал Владелец", "owner")
            client_id = clients.get_or_create_by_phone(conn, "Журнал Клиент", "+380501234500", source="offline")
            repair_id = repairs.create_repair(
                conn, client_id, "Смартфон", "Apple", "iPhone 13", None, "экран", "offline", None, 1500, owner,
                location_id=2, key="k-repair",
            )
            doc = documents.get_for(conn, "repair", repair_id)
            check("a new repair gets its document", doc is not None and doc["status"] == "posted")
            check("the repair document's number is the repair's own number (РК-NNN = «Ремонт №NNN»)",
                  doc["number"] == repair_id and documents.doc_label(doc) == f"РК-{repair_id:03d}")
            check("it records the точка, the author, the контрагент, a title and the estimate",
                  doc["location_id"] == 2 and doc["staff_id"] == owner and doc["client_id"] == client_id
                  and doc["title"] == "Смартфон Apple iPhone 13" and doc["amount"] == 1500
                  and doc["location_name"] == "Вторая" and doc["client_name"] == "Журнал Клиент")
            check("find_by_key returns it for the key it was created with",
                  documents.find_by_key(conn, "k-repair")["id"] == doc["id"]
                  and documents.find_by_key(conn, "no-such-key") is None and documents.find_by_key(conn, None) is None)
            repairs.set_price(conn, repair_id, price_estimate=1500, price_final=1800)
            check("the final price follows through to the document",
                  documents.get_for(conn, "repair", repair_id)["amount"] == 1800)

            cell_1 = inventory.create_cell(conn, "J-1", None, None, location_id=1)
            product = inventory.create_product(conn, "Журнал Товар", "J-SKU", None, "шт", False, True, 0, 400)
            inventory.receive_stock(conn, product, cell_1, 5, owner, key="k-in")
            stock_doc = documents.find_by_key(conn, "k-in")
            check("a manual «добавить остаток» is a document of its own (ОП), at the cell's точка",
                  stock_doc["doc_type"] == "stock_in" and stock_doc["location_id"] == 1
                  and documents.doc_label(stock_doc) == "ОП-001" and stock_doc["title"] == "Журнал Товар × 5")
            try:
                inventory.create_cell(conn, "J-1", None, None, location_id=2)
                check("a duplicate cell code is a friendly DuplicateCellError, not a raw IntegrityError", False)
            except inventory.DuplicateCellError:
                check("a duplicate cell code is a friendly DuplicateCellError, not a raw IntegrityError", True)

            sale_id = sales.create_sale(conn, client_id, "offline", owner, [(product, 2, 400)], location_id=1, key="k-sale")
            sale_doc = documents.get_for(conn, "sale", sale_id)
            check("a sale gets its document with the total", sale_doc["amount"] == 800 and sale_doc["doc_type"] == "sale")
            check("the sale's own stock movement did NOT get a separate document (it belongs to the sale)",
                  conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type IN ('writeoff','stock_in')").fetchone()["n"] == 1)

            expense_tx = cash.record_expense(conn, "cash", 300, "rent", "аренда", owner, location_id=1, key="k-exp")
            cash.record_adjustment(conn, 1000, "размен", owner, location_id=1)
            exp_doc = documents.find_by_key(conn, "k-exp")
            check("a manual expense is РКО-001, a negative amount (money out)",
                  documents.doc_label(exp_doc) == "РКО-001" and exp_doc["amount"] == -300 and exp_doc["ref_id"] == expense_tx)
            check("«внести» is a корректировка document (КР-001)",
                  documents.doc_label(documents.get_for(conn, "cash_adjust", exp_doc["ref_id"] + 1)) == "КР-001")
            check("касса of точка 1 = 800 sale − 300 expense + 1000 = 1500; точка 2's is untouched",
                  cash.cash_balance(conn, 1) == 1500 and cash.cash_balance(conn, 2) == 0 and cash.cash_balance(conn) == 1500)

            supplier_id = purchases.create_supplier(conn, "Журнал Поставщик", None)
            receipt_id = purchases.create_receipt(conn, supplier_id, "N-1", owner, [(product, cell_1, 10, 250)], location_id=1)
            receipt_doc = documents.get_for(conn, "receipt", receipt_id)
            check("a приход gets its document: supplier as title, qty × cost as amount",
                  receipt_doc["title"] == "Журнал Поставщик" and receipt_doc["amount"] == 2500)
            return_id = purchases.create_supplier_return(conn, product, supplier_id, receipt_id, cell_1, 1, "брак", owner)
            check("a return to the supplier gets its document (ВП)",
                  documents.get_for(conn, "supplier_return", return_id)["doc_type"] == "supplier_return")

            buyback_id = buyback.create_buyback_intake(
                conn, client_name="Продавец", client_phone="+380501234501", device_type="Смартфон", brand=None,
                model="A52", serial_number=None, condition_note=None, purchase_price=2000, payment_method="cash",
                purpose="resale", resale_price=3500, staff_id=owner, photo=None, location_id=2, key="k-buy",
            )
            buy_doc = documents.get_for(conn, "buyback", buyback_id)
            check("a покупка gets ONE document (ПК) — its payout and its stock-in are part of it, not documents of their own",
                  buy_doc["amount"] == 2000 and buy_doc["location_id"] == 2
                  and conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'cash_out'").fetchone()["n"] == 1
                  and conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'stock_in'").fetchone()["n"] == 1)
            check("the bought phone sits in точка 2's own «СКУПКА-2» cell, and the payout came out of точка 2's касса",
                  conn.execute("SELECT code FROM storage_cells WHERE id = (SELECT cell_id FROM stock WHERE product_id = ?)",
                               (conn.execute("SELECT product_id FROM buyback_orders WHERE id = ?", (buyback_id,)).fetchone()["product_id"],)
                               ).fetchone()["code"] == "СКУПКА-2"
                  and cash.cash_balance(conn, 2) == -2000)

            feed = documents.list_journal(conn)
            check("the feed holds every document, newest first",
                  len(feed) == 8 and feed[0]["doc_type"] == "buyback" and feed[-1]["doc_type"] == "repair")
            check("the feed filters by точка, by type and by контрагент",
                  {d["doc_type"] for d in documents.list_journal(conn, location_id=2)} == {"repair", "buyback"}
                  and len(documents.list_journal(conn, doc_type="sale")) == 1
                  and {d["doc_type"] for d in documents.list_journal(conn, client_id=client_id)} == {"repair", "sale"})

            # ---- cancel
            before = len(feed)
            for bad_reason, label in (("", "an empty reason"), ("   ", "a blank reason")):
                try:
                    doc_cancel.cancel_document(conn, sale_doc["id"], owner, bad_reason)
                    check(f"cancelling with {label} is refused", False)
                except documents.DocumentError:
                    check(f"cancelling with {label} is refused", True)
            try:
                doc_cancel.cancel_document(conn, receipt_doc["id"], owner, "ошибка")
                check("a document type with no reverser yet is refused, not half-cancelled", False)
            except documents.DocumentError:
                check("a document type with no reverser yet is refused, not half-cancelled", True)

            stock_before = inventory.product_total_qty(conn, product, 1)
            doc_cancel.cancel_document(conn, sale_doc["id"], owner, "пробили не тот товар")
            cancelled = documents.get(conn, sale_doc["id"])
            check("a cancelled sale stays in the journal, marked with reason, who and when",
                  cancelled["status"] == "cancelled" and cancelled["cancel_reason"] == "пробили не тот товар"
                  and cancelled["cancelled_by"] == owner and cancelled["cancelled_at"] is not None
                  and len(documents.list_journal(conn)) == before)
            check("its stock is back in the cell it left", inventory.product_total_qty(conn, product, 1) == stock_before + 2)
            check("its money no longer counts: касса 1500 − 800 = 700", cash.cash_balance(conn, 1) == 700)
            check("the sale order itself is marked cancelled and drops out of sales reports",
                  sales.get_sale(conn, sale_id)["status"] == "cancelled"
                  and len(sales.list_sales(conn, location_id=1, include_cancelled=False)) == 0
                  and len(sales.list_sales(conn, location_id=1)) == 1)
            check("the trail shows both events, in order",
                  [e["event"] for e in documents.get_events(conn, sale_doc["id"])] == ["created", "cancelled"])
            try:
                doc_cancel.cancel_document(conn, sale_doc["id"], owner, "ещё раз")
                check("cancelling twice is refused (no double stock return)", False)
            except documents.DocumentError:
                check("cancelling twice is refused (no double stock return)", True)
            check("and the stock did not move a second time", inventory.product_total_qty(conn, product, 1) == stock_before + 2)

            doc_cancel.cancel_document(conn, exp_doc["id"], owner, "ввели дважды")
            check("a cancelled expense comes back into the balance (700 + 300), the row itself is kept and flagged",
                  cash.cash_balance(conn, 1) == 1000
                  and conn.execute("SELECT cancelled_at FROM cash_transactions WHERE id = ?", (expense_tx,)).fetchone()["cancelled_at"] is not None)

        # ---- backfill: a base that had operations before the journal existed
        with get_conn(db_path) as conn:
            total = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            conn.execute("DELETE FROM document_events")
            conn.execute("DELETE FROM documents")
        init_db(db_path)
        with get_conn(db_path) as conn:
            rebuilt = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            check("init_db rebuilds the whole journal from the business tables (same number of documents)", rebuilt == total)
            repair_doc = documents.get_for(conn, "repair", repair_id)
            check("a backfilled document keeps the original date, точка and author — not «now»",
                  repair_doc["created_at"] == repairs.get_repair(conn, repair_id)["created_at"]
                  and repair_doc["location_id"] == 2 and repair_doc["staff_id"] == owner)
            check("a sale that was already cancelled comes back as cancelled",
                  documents.get_for(conn, "sale", sale_id)["status"] == "cancelled")
        init_db(db_path)
        with get_conn(db_path) as conn:
            check("running it again adds nothing (idempotent)",
                  conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"] == total)


def scenario_journal_http() -> None:
    """Журнал, отмена and form idempotency through the real web app."""
    print("scenario: журнал документов over HTTP — лента, фильтры, отмена, повторный сабмит")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "journal-http.sqlite3", ("Первая", "Вторая")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "jh-owner", "pass", "Владелец Журнала", "owner")
            master = auth.create_staff(conn, "jh-master", "pass", "Мастер Журнала", "master")
            keeper = auth.create_staff(conn, "jh-keeper", "pass", "Кладовщик", "storekeeper")
            cell = inventory.create_cell(conn, "JH-1", None, None, location_id=1)
            product = inventory.create_product(conn, "HTTP Товар", "JH-SKU", None, "шт", False, True, 0, 250)
            inventory.receive_stock(conn, product, cell, 10, owner)
        token = make_token(owner, "1")
        token_2 = make_token(owner, "2")
        master_token = make_token(master, "1")
        keeper_token = make_token(keeper, "1")

        import webapp.main

        with TestClient(webapp.main.app) as client:
            sales_page = client.get(f"/sales?t={token}").text
            idem = re.search(r'name="idem" value="([0-9a-f]{32})"', sales_page)
            check("the sale form carries a hidden idempotency token", idem is not None)
            form = {
                "idem": idem.group(1), "row_count": "1", "product_id_0": str(product), "product_name_0": "HTTP Товар",
                "qty_0": "2", "price_0": "250", "channel": "offline", "payment_method": "cash",
                "client_name": "Покупатель", "client_phone": "+380509998877",
            }
            first = client.post(f"/sales?t={token}", data=form, follow_redirects=False)
            second = client.post(f"/sales?t={token}", data=form, follow_redirects=False)
            with get_conn(db_path) as conn:
                sale_count = conn.execute("SELECT COUNT(*) AS n FROM sales_orders").fetchone()["n"]
                stock_left = inventory.product_total_qty(conn, product, 1)
            check("submitting the very same form twice creates ONE sale", sale_count == 1)
            check("the second submit lands on the first one's page instead",
                  first.status_code == 303 and second.status_code == 303
                  and first.headers["location"].split("?")[0] == second.headers["location"].split("?")[0])
            check("and stock was written off once (10 − 2)", stock_left == 8)
            form_new = dict(form, idem="f" * 32)
            client.post(f"/sales?t={token}", data=form_new, follow_redirects=False)
            with get_conn(db_path) as conn:
                check("a NEW form (new token) with the same contents is a new sale — only repeats are deduplicated",
                      conn.execute("SELECT COUNT(*) AS n FROM sales_orders").fetchone()["n"] == 2)

            cash_idem = re.search(r'name="idem" value="([0-9a-f]{32})"', client.get(f"/cash?t={token}").text).group(1)
            for _ in range(2):
                client.post(f"/cash/expense?t={token}", data={"idem": cash_idem, "amount": "100", "category": "other", "method": "cash", "comment": "чай"})
            adj = client.post(f"/cash/adjustment?t={token}", data={"idem": cash_idem, "amount": "40", "direction": "in"}, follow_redirects=False)
            with get_conn(db_path) as conn:
                check("a double-submitted expense is recorded once (500 + 500 − 100 + 40)",
                      cash.cash_balance(conn, 1) == 940)
            check("the same page's OTHER form (внести) is not swallowed by the expense's token", adj.status_code == 303)

            check("the journal is owner/admin only (a master gets 403)", client.get(f"/journal?t={master_token}").status_code == 403)
            check("«Ещё» shows the journal card to an owner, not to a master",
                  "/journal" in client.get(f"/more?t={token}").text and "/journal" not in client.get(f"/more?t={master_token}").text)
            journal = client.get(f"/journal?t={token}")
            check("the journal lists today's documents with their labels",
                  journal.status_code == 200 and "ПД-001" in journal.text and "ПД-002" in journal.text
                  and "РКО-001" in journal.text and "КР-001" in journal.text and "ОП-001" in journal.text)
            check("it shows the точка and the employee", "Первая" in journal.text and "Владелец Журнала" in journal.text)
            only_sales = client.get(f"/journal?t={token}&doc_type=sale").text
            check("the type filter narrows it down", "ПД-001" in only_sales and "РКО-001" not in only_sales)
            check("the точка filter too — nothing happened at точка 2",
                  "ПД-001" not in client.get(f"/journal?t={token}&location=2").text)
            check("and the date filter — nothing happened in 2020",
                  "ПД-001" not in client.get(f"/journal?t={token}&date_from=2020-01-01&date_to=2020-01-02").text)
            check("a garbage date doesn't crash the page",
                  client.get(f"/journal?t={token}&date_from=oops&date_to=").status_code == 200)

            with get_conn(db_path) as conn:
                sale_doc = documents.list_journal(conn, doc_type="sale")[-1]
                stock_doc = documents.list_journal(conn, doc_type="stock_in")[0]
            doc_page = client.get(f"/journal/{sale_doc['id']}?t={token}")
            check("a document's page shows its trail and a cancel form",
                  doc_page.status_code == 200 and "ПД-001" in doc_page.text and f"/journal/{sale_doc['id']}/cancel" in doc_page.text)
            client.post(f"/cash/shift/open?t={token}", data={})
            with get_conn(db_path) as conn:
                shift_doc = documents.list_journal(conn, doc_type="shift")[0]
            shift_page = client.get(f"/journal/{shift_doc['id']}?t={token}").text
            check("a document type with nothing to reverse (a shift opening) says so, with no cancel form",
                  f"/journal/{shift_doc['id']}/cancel" not in shift_page and "нельзя отменить" in shift_page)
            check("a manual stock-in CAN be cancelled now (партии made it reversible)",
                  f"/journal/{stock_doc['id']}/cancel" in client.get(f"/journal/{stock_doc['id']}?t={token}").text)
            no_reason = client.post(f"/journal/{sale_doc['id']}/cancel?t={token}", data={"reason": "  "})
            check("cancelling without a reason is a friendly error", no_reason.status_code == 200 and "Укажите причину" in no_reason.text)
            check("a master can't cancel a document (403)",
                  client.post(f"/journal/{sale_doc['id']}/cancel?t={master_token}", data={"reason": "x"}).status_code == 403)
            done = client.post(f"/journal/{sale_doc['id']}/cancel?t={token}", data={"reason": "ошиблись товаром"}, follow_redirects=False)
            check("cancelling with a reason goes through", done.status_code == 303)
            with get_conn(db_path) as conn:
                check("stock is back (6 + 2) and the money is out of the касса (940 − 500)",
                      inventory.product_total_qty(conn, product, 1) == 8 and cash.cash_balance(conn, 1) == 440)
            after = client.get(f"/journal/{sale_doc['id']}?t={token}").text
            check("the document page now shows it cancelled, with the reason, and no cancel form",
                  "ошиблись товаром" in after and f"/journal/{sale_doc['id']}/cancel" not in after)
            check("the sale's own page and the sales list mark it cancelled",
                  "ошиблись товаром" in client.get(f"/sales/{sale_doc['ref_id']}?t={token}").text
                  and "pill-cancelled" in client.get(f"/sales?t={token}").text)
            check("the cancelled expense-free cash feed still lists the sale's income, flagged",
                  "pill-cancelled" in client.get(f"/cash?t={token}").text)
            check("an unknown document id just goes back to the journal",
                  client.get(f"/journal/999999?t={token}", follow_redirects=False).status_code == 303)

            # ---- per-точка stock on the shared catalog
            check("точка 1 sees its stock of the shared product", "8 шт" in client.get(f"/inventory/products?t={token}").text)
            check("точка 2 sees the same product with nothing in stock",
                  "HTTP Товар" in client.get(f"/inventory/products?t={token_2}").text
                  and "0 шт" in client.get(f"/inventory/products?t={token_2}").text)
            check("точка 2's cell list doesn't show точка 1's cell", "JH-1" not in client.get(f"/inventory/cells?t={token_2}").text)
            dup = client.post(f"/inventory/cells?t={keeper_token}", data={"code": "JH-1", "zone": "", "note": ""})
            check("creating a cell with a taken code is a friendly error, not a 500",
                  dup.status_code == 200 and "уже есть" in dup.text)

            # ---- one phone = one контрагент
            taken = client.post(f"/clients?t={token}", data={"name": "Двойник", "phone": "0509998877", "notes": ""})
            check("a second client card with an existing phone is refused with the existing client's name",
                  taken.status_code == 200 and "уже есть" in taken.text and "Покупатель" in taken.text)

            # ---- master's точка
            client.post(f"/masters?t={token}", data={"name": "Мастер Со Второй", "location_id": "2", "pay_type": "", "pay_value": ""})
            with get_conn(db_path) as conn:
                row = conn.execute("SELECT * FROM staff WHERE name = 'Мастер Со Второй'").fetchone()
            check("a master can be tied to a точка from the Мастера form", row is not None and row["location_id"] == 2)


def scenario_merge_stores_tool() -> None:
    """tools/merge_stores.py — the one-off that folds the per-store files
    into one base. Rebuilds the pre-merge layout (a main base with data and
    no locations table, two empty store files with only the owner and a
    name, a stores.json) and runs the real script against it."""
    print("scenario: перенос трёх баз в одну (tools/merge_stores.py)")
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "merge_stores", os.path.join(os.path.dirname(__file__), "tools", "merge_stores.py"))
    merge_stores = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(merge_stores)

    def _to_old_layout(path: str, name: str, sales_channel: str | None = None) -> None:
        with get_conn(path) as conn:
            conn.execute("UPDATE store_settings SET name = ?, sales_channel = ? WHERE id = 1", (name, sales_channel))
            conn.execute("UPDATE cash_transactions SET account_id = NULL")
            # Service cells (a master's, «В пути») came with склады.
            conn.execute("DELETE FROM storage_cells WHERE code = 'В-ПУТИ' OR code LIKE 'МАСТЕР-%'")
            conn.execute("UPDATE storage_cells SET warehouse_id = NULL")
            for table in ("document_events", "documents", "shifts", "money_transfers", "money_exchanges",
                          "money_accounts", "stock_transfer_items", "stock_transfers", "warehouses", "locations"):
                conn.execute(f"DROP TABLE {table}")

    prev_db, prev_config, prev_argv = os.environ["CRM_DB_PATH"], os.environ.get("CRM_STORES_CONFIG"), sys.argv
    with tempfile.TemporaryDirectory() as tmp:
        main_db, db2, db3 = (os.path.join(tmp, n) for n in ("crm.sqlite3", "store2.sqlite3", "store3.sqlite3"))
        try:
            for path in (main_db, db2, db3):
                init_db(path)
                with get_conn(path) as conn:
                    auth.create_staff(conn, "owner", "pass", "Павел", "owner")
                    auth.link_staff_telegram(conn, "owner", 1417)
            with get_conn(main_db) as conn:
                owner = auth.get_staff_by_login(conn, "owner")["id"]
                client_id = clients.get_or_create_by_phone(conn, "Старый Клиент", "+380500000001", source="offline")
                repair_id = repairs.create_repair(conn, client_id, "Смартфон", None, "A12", None, "звук", "offline", None, 700, owner)
                cell = inventory.create_cell(conn, "OLD-1", None, None)
                conn.execute("UPDATE storage_cells SET warehouse_id = NULL")
                conn.execute("UPDATE repair_orders SET location_id = NULL")
            _to_old_layout(main_db, "007", "@OO7servis")
            _to_old_layout(db2, "Гибрид сервис")
            _to_old_layout(db3, "Магазин")

            config = os.path.join(tmp, "stores.json")
            with open(config, "w", encoding="utf-8") as f:
                json.dump([
                    {"id": "1", "name": "Магазин 1", "db_path": main_db, "staff_group_chat_id": -1001,
                     "repair_topic_id": 5, "sales_topic_id": 121, "masters_group_chat_id": -1002},
                    {"id": "2", "name": "Магазин 2", "db_path": db2, "staff_group_chat_id": None},
                    {"id": "3", "name": "Магазин 3", "db_path": db3},
                ], f)
            os.environ["CRM_STORES_CONFIG"] = config

            sys.argv = ["merge_stores.py"]
            merge_stores.main()
            with get_conn(main_db) as conn:
                check("a dry run changes nothing (no locations table yet)",
                      conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'locations'").fetchone() is None)

            sys.argv = ["merge_stores.py", "--apply"]
            merge_stores.main()
            os.environ["CRM_DB_PATH"] = main_db
            merged = stores.load_stores()
            check("all three точки are in the one base, under the names their owners gave them",
                  [(s.id, s.name) for s in merged] == [("1", "007"), ("2", "Гибрид сервис"), ("3", "Магазин")])
            check("точка 1 kept its Telegram groups and topics from stores.json",
                  merged[0].staff_group_chat_id == -1001 and merged[0].repair_topic_id == 5
                  and merged[0].sales_topic_id == 121 and merged[0].masters_group_chat_id == -1002
                  and stores.store_for_chat_id(-1002).id == "1")
            with get_conn(main_db) as conn:
                check("точка 1 kept its sales channel", store_settings.get_settings(conn, 1)["sales_channel"] == "@OO7servis")
                check("the existing repair now belongs to точка 1 and is in the journal as РК-NNN",
                      repairs.get_repair(conn, repair_id)["location_id"] == 1
                      and documents.doc_label(documents.get_for(conn, "repair", repair_id)) == f"РК-{repair_id:03d}")
                check("the existing cell now belongs to точка 1's склад",
                      [c["id"] for c in inventory.list_cells(conn, 1)] == [cell] and inventory.list_cells(conn, 2) == [])
                check("each точка has its склад",
                      conn.execute("SELECT COUNT(*) AS n FROM warehouses WHERE kind = 'point'").fetchone()["n"] == 3)
            check("the owner — one staff row — can now work in all three",
                  [s.id for s, _ in store_access.accessible_stores(1417)] == ["1", "2", "3"])

            merge_stores.main()
            check("running it a second time changes nothing", len(stores.load_stores()) == 3)

            # A store file that isn't empty must stop the merge, not lose data.
            with get_conn(db2) as conn:
                conn.execute("INSERT INTO clients (name, phone) VALUES ('Забытый', '+380500000099')")
            try:
                merge_stores.main()
                check("a store file that still holds data stops the merge", False)
            except SystemExit as exc:
                check("a store file that still holds data stops the merge", "СТОП" in str(exc))
        finally:
            sys.argv = prev_argv
            os.environ["CRM_DB_PATH"] = prev_db
            if prev_config is None:
                os.environ.pop("CRM_STORES_CONFIG", None)
            else:
                os.environ["CRM_STORES_CONFIG"] = prev_config


def scenario_money() -> None:
    """Заход 2 core: accounts and their balances, split payments, обмен,
    перемещение with confirmation, shifts, cancel."""
    print("scenario: деньги — счета, сплит-оплата, обмен, перемещение, смены")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "money.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "m-owner", "pass", "Виталий", "owner")
            acc = {(a["location_id"], a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn)}
            check("every точка starts with the default set of six accounts",
                  len(acc) == 12 and [a["name"] for a in accounts.list_accounts(conn, 1)]
                  == ["Наличные", "Наличные USD", "Наличные EUR", "Счёт ФОП", "Карта", "Криптокошелёк USDT"])
            cash_uah, cash_usd, fop, card, usdt = (acc[(1, k, c)] for k, c in
                                                   (("cash", "UAH"), ("cash", "USD"), ("fop", "UAH"), ("card", "UAH"), ("crypto", "USDT")))
            check("parse_amount accepts what people type and refuses what isn't an amount",
                  accounts.parse_amount("1 200") == 1200 and accounts.parse_amount("12,5") == 12.5
                  and accounts.parse_amount("12.50") == 12.5 and isinstance(accounts.parse_amount("500.0"), int)
                  and accounts.parse_amount("") is None and accounts.parse_amount("-5") is None
                  and accounts.parse_amount("0") is None and accounts.parse_amount("abc") is None)

            # ---- split payment of a sale (макет: нал 2000 + ФОП 1500 + карта 1000 + USDT 12.5 × 40 = 5000)
            cell = inventory.create_cell(conn, "M-1", None, None, location_id=1)
            phone = inventory.create_product(conn, "iPhone 13", "M-SKU", None, "шт", False, True, 0, 5000)
            inventory.receive_stock(conn, phone, cell, 3, owner)
            sale_id = sales.create_sale(
                conn, None, "offline", owner, [(phone, 1, 5000)], location_id=1,
                payments=[(cash_uah, "2000", None), (fop, "1500", None), (card, "1000", None), (usdt, "12,5", "40")],
            )
            check("each part landed on its own account, in that account's currency",
                  accounts.balance(conn, cash_uah) == 2000 and accounts.balance(conn, fop) == 1500
                  and accounts.balance(conn, card) == 1000 and accounts.balance(conn, usdt) == 12.5)
            check("the sale is marked as paid into several accounts", sales.get_sale(conn, sale_id)["payment_method"] == "mixed")
            paid = cash.payments_for(conn, "sales_order", sale_id)
            check("the USDT part remembers its rate and its гривня value (12.5 × 40 = 500)",
                  [(p["account_name"], p["amount"], p["amount_uah"]) for p in paid][-1] == ("Криптокошелёк USDT", 12.5, 500)
                  and paid[-1]["rate"] == 40 and sum(p["amount_uah"] for p in paid) == 5000)
            check("«наличные гривни» is only the cash-UAH account, never the other balances mixed in",
                  cash.cash_balance(conn, 1) == 2000)

            def _refused(payments, needle, label):
                try:
                    # Same connection on purpose: a refused payment is
                    # raised before create_sale writes anything.
                    sales.create_sale(conn, None, "offline", owner, [(phone, 1, 5000)], location_id=1, payments=payments)
                    check(label, False)
                except cash.PaymentError as exc:
                    check(label, needle in str(exc))

            _refused([(cash_uah, "3000", None)], "не хватает 2000", "an underpaid split is refused, saying how much is missing")
            _refused([(cash_uah, "6000", None)], "лишние 1000", "an overpaid one too")
            _refused([(usdt, "125", None)], "Укажите курс", "a foreign-currency part without a rate is refused")
            _refused([(acc[(2, "cash", "UAH")], "5000", None)], "нет у этой точки", "another точка's account can't take this точка's sale")
            _refused([(cash_uah, "abc", None)], "Проверьте сумму", "a garbage amount is refused")
            check("none of the refused sales left anything behind (no order, no stock gone, no money)",
                  len(sales.list_sales(conn, location_id=1)) == 1 and inventory.product_total_qty(conn, phone, 1) == 2
                  and accounts.balance(conn, cash_uah) == 2000)
            one_tap = sales.create_sale(conn, None, "offline", owner, [(phone, 1, 5000)], payment_method="card", location_id=1)
            check("the one-tap «карта» path still works: whole total onto the точка's card account",
                  accounts.balance(conn, card) == 6000 and sales.get_sale(conn, one_tap)["payment_method"] == "card")

            # ---- обмен: −45 000 UAH, +1 000 USD по курсу 45
            cash.record_adjustment(conn, 50000, "стартовый остаток", owner, location_id=1)
            exchange_id = money_ops.exchange(conn, cash_uah, cash_usd, "45000", "1000", owner)
            check("an exchange moves both balances", accounts.balance(conn, cash_uah) == 7000 and accounts.balance(conn, cash_usd) == 1000)
            ex_doc = documents.get_for(conn, "exchange", exchange_id)
            check("it is a document (ОБ) with the derived rate in its title",
                  documents.doc_label(ex_doc).startswith("ОБ-") and "курс 45" in ex_doc["title"])
            today = timefmt.kyiv_today()
            utc_start, utc_end = timefmt.kyiv_date_range_utc(today, today)
            summary = cash.period_summary(conn, utc_start, utc_end, 1)
            check("an exchange is neither income nor expense in the period summary (деньги ≠ прибыль)",
                  summary["income_total"] == 60000 and summary["expense_total"] == 0)
            check("the summary splits наличные vs безнал in гривня: 52 000 cash, 8 000 non-cash (incl. 500 грн of USDT)",
                  summary["income_cash"] == 52000 and summary["income_card"] == 8000)
            for args, needle, label in (
                ((cash_uah, cash_usd, "999999", "100"), "только", "an exchange can't take more than the account holds"),
                ((cash_uah, fop, "100", "100"), "одна валюта", "same-currency accounts are a перемещение, not an exchange"),
                ((cash_uah, acc[(2, "cash", "USD")], "100", "2"), "одной точки", "an exchange across точки is refused"),
                ((cash_uah, cash_uah, "100", "100"), "разными", "an account can't be exchanged into itself"),
                ((cash_uah, cash_usd, "", "5"), "сколько отдали", "missing amounts are refused"),
            ):
                try:
                    money_ops.exchange(conn, *args, owner)
                    check(label, False)
                except accounts.AccountError as exc:
                    check(label, needle in str(exc))

            # ---- перемещение между точками: Отправил → В пути → Принял
            usd_2 = acc[(2, "cash", "USD")]
            transfer_id = money_ops.send_transfer(conn, cash_usd, usd_2, "400", owner, "в магазин")
            check("sent money leaves the source at once and is NOT yet on the destination",
                  accounts.balance(conn, cash_usd) == 600 and accounts.balance(conn, usd_2) == 0)
            check("it is «в пути»: incoming for точка 2, outgoing for точка 1",
                  [t["id"] for t in money_ops.list_in_transit(conn, 2, "incoming")] == [transfer_id]
                  and [t["id"] for t in money_ops.list_in_transit(conn, 1, "outgoing")] == [transfer_id]
                  and money_ops.list_in_transit(conn, 1, "incoming") == [])
            try:
                money_ops.receive_transfer(conn, transfer_id, owner, "390")
                check("receiving a different amount without saying why is refused", False)
            except accounts.AccountError:
                check("receiving a different amount without saying why is refused", True)
            money_ops.receive_transfer(conn, transfer_id, owner, "390", "одной купюры не хватило")
            received = money_ops.get_transfer(conn, transfer_id)
            check("the destination gets what was actually counted; the difference stays on record",
                  accounts.balance(conn, usd_2) == 390 and received["status"] == "received"
                  and received["discrepancy_note"] == "одной купюры не хватило"
                  and [t["id"] for t in money_ops.list_discrepancies(conn)] == [transfer_id])
            tr_doc = documents.get_for(conn, "money_transfer", transfer_id)
            check("the document's trail shows the discrepancy",
                  [e["event"] for e in documents.get_events(conn, tr_doc["id"])] == ["created", "discrepancy"])
            try:
                money_ops.receive_transfer(conn, transfer_id, owner)
                check("a transfer can't be received twice", False)
            except accounts.AccountError:
                check("a transfer can't be received twice", True)
            inside = money_ops.send_transfer(conn, cash_uah, fop, "1000", owner)
            check("a перемещение inside one точка needs no confirmation — it arrives immediately",
                  money_ops.get_transfer(conn, inside)["status"] == "received"
                  and accounts.balance(conn, cash_uah) == 6000 and accounts.balance(conn, fop) == 2500)
            try:
                money_ops.send_transfer(conn, cash_uah, cash_usd, "10", owner)
                check("a перемещение between different currencies is refused (that's an обмен)", False)
            except accounts.AccountError:
                check("a перемещение between different currencies is refused (that's an обмен)", True)
            summary = cash.period_summary(conn, utc_start, utc_end)
            check("transfers don't inflate the business-wide summary either", summary["income_total"] == 60000 and summary["expense_total"] == 0)

            # ---- отмена
            pending = money_ops.send_transfer(conn, cash_uah, acc[(2, "cash", "UAH")], "500", owner)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "money_transfer", pending)["id"], owner, "не тот счёт")
            check("cancelling a transfer still «в пути» returns the money and it can no longer be received",
                  accounts.balance(conn, cash_uah) == 6000 and money_ops.get_transfer(conn, pending)["status"] == "cancelled"
                  and money_ops.list_in_transit(conn, 2, "incoming") == [])
            doc_cancel.cancel_document(conn, ex_doc["id"], owner, "не тот курс")
            check("cancelling an exchange puts both sides back",
                  accounts.balance(conn, cash_uah) == 51000 and accounts.balance(conn, cash_usd) == -400)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", sale_id)["id"], owner, "возврат")
            check("cancelling a split-paid sale takes the money back out of EVERY account it went into",
                  accounts.balance(conn, usdt) == 0 and accounts.balance(conn, card) == 5000
                  and accounts.balance(conn, fop) == 1000 and accounts.balance(conn, cash_uah) == 49000)

            # ---- счета точки
            try:
                accounts.set_active(conn, card, False)
                check("an account that still holds money can't be switched off", False)
            except accounts.AccountError:
                check("an account that still holds money can't be switched off", True)
            eur = acc[(1, "cash", "EUR")]
            accounts.set_active(conn, eur, False)
            check("a switched-off empty account disappears from pickers and from the balances list",
                  eur not in [a["id"] for a in accounts.list_accounts(conn, 1)]
                  and eur not in [a["id"] for a in accounts.balances(conn, 1)])
            mono = accounts.create_account(conn, 1, "card", "UAH", "  Монобанк  ")
            check("a new account can be added and renamed",
                  accounts.get_account(conn, mono)["name"] == "Монобанк" and accounts.balance(conn, mono) == 0)
            for bad in (("nope", "UAH", "X"), ("card", "RUB", "X"), ("card", "UAH", "  ")):
                try:
                    accounts.create_account(conn, 1, *bad)
                    check(f"a bad account {bad} is refused", False)
                except accounts.AccountError:
                    check(f"a bad account {bad} is refused", True)

            # ---- смены
            check("no shift is open at first", shifts.current_shift(conn, owner, 1) is None)
            snap = shifts.snapshot(conn, 1)
            check("«сверить остатки» lists the точка's accounts with balances, and goods across the whole business",
                  {a["name"]: a["balance"] for a in snap["accounts"]}["Наличные"] == 49000 and snap["stock_value"] == 0)
            shift_id = shifts.open_shift(conn, owner, 1)
            check("opening a shift makes it current; a second open is the same shift, not a new one",
                  shifts.current_shift(conn, owner, 1)["id"] == shift_id and shifts.open_shift(conn, owner, 1) == shift_id)
            check("a shift belongs to its точка — none is open at the other one", shifts.current_shift(conn, owner, 2) is None)
            conn.execute("UPDATE shifts SET opened_at = datetime('now', '-2 days') WHERE id = ?", (shift_id,))
            check("yesterday's shift doesn't count as open today", shifts.current_shift(conn, owner, 1) is None)
            new_shift = shifts.open_shift(conn, owner, 1, "в кассе на 100 грн больше")
            check("opening today's shift closes the stale one and records the discrepancy",
                  new_shift != shift_id
                  and conn.execute("SELECT closed_at FROM shifts WHERE id = ?", (shift_id,)).fetchone()["closed_at"] is not None
                  and [s["id"] for s in shifts.list_discrepancies(conn)] == [new_shift])
            shifts.close_shift(conn, new_shift, owner)
            check("a closed shift is no longer current", shifts.current_shift(conn, owner, 1) is None)

        # ---- a base from before accounts: rows knew only cash/card
        with get_conn(db_path) as conn:
            conn.execute("DELETE FROM cash_transactions")
            conn.execute(
                "INSERT INTO cash_transactions (kind, method, amount, staff_id, location_id) VALUES ('income', 'cash', 700, ?, 2)", (owner,))
            conn.execute(
                "INSERT INTO cash_transactions (kind, method, amount, staff_id, location_id) VALUES ('income', 'card', 300, ?, 2)", (owner,))
        init_db(db_path)
        with get_conn(db_path) as conn:
            check("init_db puts pre-accounts rows onto their точка's default cash / card accounts, worth their own amount",
                  accounts.balance(conn, acc[(2, "cash", "UAH")]) == 700 and accounts.balance(conn, acc[(2, "card", "UAH")]) == 300
                  and conn.execute("SELECT COUNT(*) AS n FROM cash_transactions WHERE amount_uah IS NULL").fetchone()["n"] == 0)


def scenario_money_http() -> None:
    print("scenario: деньги over HTTP — касса, сплит-оплата продажи и ремонта, обмен, перемещение, смена, счета")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "money-http.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "mh-owner", "pass", "Владелец Денег", "owner")
            keeper = auth.create_staff(conn, "mh-keeper", "pass", "Кладовщик Первой", "storekeeper", location_id=1)
            keeper_2 = auth.create_staff(conn, "mh-keeper2", "pass", "Кладовщик Второй", "storekeeper", location_id=2)
            master = auth.create_staff(conn, "mh-master", "pass", "Мастер", "master")
            acc = {(a["location_id"], a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn)}
            cell = inventory.create_cell(conn, "MH-1", None, None, location_id=1)
            product = inventory.create_product(conn, "Кабель", "MH-SKU", None, "шт", False, True, 0, 400)
            inventory.receive_stock(conn, product, cell, 10, owner)
            client_id = clients.get_or_create_by_phone(conn, "Клиент Ремонта", "+380501117700", source="offline")
            repair_id = repairs.create_repair(conn, client_id, "Смартфон", None, "A54", None, "экран", "offline", None, 3000, owner, location_id=1)
            repairs.set_price(conn, repair_id, 3000, 3000)
        cash_uah, fop, usd, usdt = acc[(1, "cash", "UAH")], acc[(1, "fop", "UAH")], acc[(1, "cash", "USD")], acc[(1, "crypto", "USDT")]
        token, token_2 = make_token(owner, "1"), make_token(owner, "2")
        keeper_token, keeper2_token, master_token = make_token(keeper, "1"), make_token(keeper_2, "2"), make_token(master, "1")

        import webapp.main

        with TestClient(webapp.main.app) as client:
            # ---- продажа: наличные 500 + ФОП 700 = 1200 (макет «Клиент и оплата»)
            sales_page = client.get(f"/sales?t={token}").text
            check("the sale form has an amount field per account of the точка, and a rate field for foreign ones",
                  f'name="pay_{cash_uah}"' in sales_page and f'name="pay_{fop}"' in sales_page
                  and f'name="rate_{usdt}"' in sales_page and f'name="rate_{cash_uah}"' not in sales_page)
            check("and no field for another точка's accounts", f'name="pay_{acc[(2, "cash", "UAH")]}"' not in sales_page)
            sale_form = {"row_count": "1", "product_id_0": str(product), "product_name_0": "Кабель", "qty_0": "3", "price_0": "400",
                         "channel": "offline", f"pay_{cash_uah}": "500", f"pay_{fop}": "700"}
            done = client.post(f"/sales?t={token}", data=sale_form, follow_redirects=False)
            check("a sale split across two accounts goes through", done.status_code == 303)
            with get_conn(db_path) as conn:
                check("500 landed in наличные, 700 on ФОП",
                      accounts.balance(conn, cash_uah) == 500 and accounts.balance(conn, fop) == 700)
            sale_page = client.get(done.headers["location"]).text
            check("the sale's page shows how it was paid, account by account",
                  "Счёт ФОП" in sale_page and "700" in sale_page and "Несколько счетов" in sale_page)
            wrong = client.post(f"/sales?t={token}", data=dict(sale_form, **{f"pay_{fop}": "600"}))
            check("a split that doesn't add up is a friendly error, not a 500", wrong.status_code == 200 and "не сходится" in wrong.text)
            with get_conn(db_path) as conn:
                check("and wrote nothing (stock still 10 − 3)", inventory.product_total_qty(conn, product, 1) == 7)
            client.post(f"/sales?t={token}", data={"row_count": "1", "product_id_0": str(product), "product_name_0": "Кабель",
                                                    "qty_0": "1", "price_0": "400", "channel": "offline"})
            with get_conn(db_path) as conn:
                check("with no payment fields filled the whole total goes to наличные (one tap)", accounts.balance(conn, cash_uah) == 900)

            # ---- ремонт: выдача с оплатой частично в USD
            repair_page = client.get(f"/repairs/{repair_id}?t={token_2}").text
            check("the repair card offers the accounts of the точка that TOOK the repair, even opened from another",
                  f'name="pay_{cash_uah}"' in repair_page and f'name="pay_{acc[(2, "cash", "UAH")]}"' not in repair_page)
            no_pay = client.post(f"/repairs/{repair_id}/status?t={token}", data={"status": "issued"})
            check("marking «Выдан» with a price and no payment is still refused", "Укажите способ оплаты" in no_pay.text)
            short = client.post(f"/repairs/{repair_id}/status?t={token}", data={"status": "issued", f"pay_{cash_uah}": "1000"})
            check("an underpaid выдача is refused with the missing amount", "не хватает 2000" in short.text)
            with get_conn(db_path) as conn:
                check("the repair is still not issued", repairs.get_repair(conn, repair_id)["status"] != "issued")
            issued = client.post(f"/repairs/{repair_id}/status?t={token}", data={
                "status": "issued", f"pay_{cash_uah}": "1360", f"pay_{usd}": "40", f"rate_{usd}": "41"}, follow_redirects=False)
            check("1360 грн + 40 USD × 41 = 3000 goes through", issued.status_code == 303)
            with get_conn(db_path) as conn:
                check("the repair is issued, 40 USD sit on the USD account",
                      repairs.get_repair(conn, repair_id)["status"] == "issued" and accounts.balance(conn, usd) == 40
                      and accounts.balance(conn, cash_uah) == 2260)
            check("the repair card now shows how it was paid instead of the payment fields",
                  "Наличные USD" in client.get(f"/repairs/{repair_id}?t={token}").text
                  and f'name="pay_{cash_uah}"' not in client.get(f"/repairs/{repair_id}?t={token}").text)
            client.post(f"/repairs/{repair_id}/status?t={token}", data={"status": "issued", "comment": "повторно"})
            with get_conn(db_path) as conn:
                check("re-saving an already issued repair takes no money twice", accounts.balance(conn, cash_uah) == 2260)

            # ---- касса: страница
            cash_page = client.get(f"/cash?t={token}").text
            check("Касса lists every account with its balance",
                  all(name in cash_page for name in ("Наличные USD", "Счёт ФОП", "Криптокошелёк USDT")) and "2260" in cash_page)
            check("a storekeeper sees Касса but not the «корректировка» form or the accounts link",
                  "/cash/adjustment" not in client.get(f"/cash?t={keeper_token}").text
                  and "/cash/accounts" not in client.get(f"/cash?t={keeper_token}").text
                  and "/cash/adjustment" in cash_page)
            check("a storekeeper can't post a correction (403) — only owner/admin",
                  client.post(f"/cash/adjustment?t={keeper_token}", data={"amount": "100", "direction": "in"}).status_code == 403)
            check("a master can't open Касса at all (403)", client.get(f"/cash?t={master_token}").status_code == 403)
            client.post(f"/cash/expense?t={keeper_token}", data={"amount": "150,50", "category": "other", "account_id": str(fop), "comment": "канцтовары"})
            with get_conn(db_path) as conn:
                check("an expense can be taken from a chosen account, with kopecks", accounts.balance(conn, fop) == 549.5)
            foreign = client.post(f"/cash/expense?t={token}", data={"amount": "10", "account_id": str(acc[(2, "cash", "UAH")])})
            check("an expense from another точка's account is refused", "Выберите счёт этой точки" in foreign.text)

            # ---- обмен
            bad_ex = client.post(f"/cash/exchange?t={token}", data={"from_account_id": str(cash_uah), "to_account_id": str(usd), "amount_from": "999999", "amount_to": "5"})
            check("an exchange beyond the balance is a friendly error", bad_ex.status_code == 200 and "только" in bad_ex.text)
            client.post(f"/cash/exchange?t={token}", data={"from_account_id": str(cash_uah), "to_account_id": str(usd), "amount_from": "2050", "amount_to": "50"})
            with get_conn(db_path) as conn:
                check("2050 грн → 50 USD: both balances moved", accounts.balance(conn, cash_uah) == 210 and accounts.balance(conn, usd) == 90)

            # ---- перемещение на другую точку
            usd_2 = acc[(2, "cash", "USD")]
            client.post(f"/cash/transfer?t={token}", data={"from_account_id": str(usd), "to_account_id": str(usd_2), "amount": "60", "comment": "инкассация"})
            with get_conn(db_path) as conn:
                transfer = money_ops.list_in_transit(conn, 2, "incoming")[0]
                check("the money left точка 1 and is in transit", accounts.balance(conn, usd) == 30 and accounts.balance(conn, usd_2) == 0)
            check("точка 1 shows it as sent and waiting", "ждёт подтверждения" in client.get(f"/cash?t={token}").text)
            check("точка 2's Касса asks to confirm it", f"/cash/transfer/{transfer['id']}/receive" in client.get(f"/cash?t={keeper2_token}").text)
            stranger = client.post(f"/cash/transfer/{transfer['id']}/receive?t={keeper_token}", data={"received_amount": "60"})
            check("someone from the SENDING точка can't confirm receipt for the destination", "другой точке" in stranger.text)
            client.post(f"/cash/transfer/{transfer['id']}/receive?t={keeper2_token}", data={"received_amount": "60"})
            with get_conn(db_path) as conn:
                check("the destination's own storekeeper confirms — the money is now there", accounts.balance(conn, usd_2) == 60)
            check("and nothing is left to confirm", "/receive" not in client.get(f"/cash?t={keeper2_token}").text)

            # ---- журнал: новые типы и отмена обмена
            journal = client.get(f"/journal?t={token}").text
            check("the journal shows the exchange, the transfer and the opening balances' documents",
                  "ОБ-001" in journal and "ДП-001" in journal and "Обмен валют" in journal)
            with get_conn(db_path) as conn:
                ex_doc = documents.list_journal(conn, doc_type="exchange")[0]
            client.post(f"/journal/{ex_doc['id']}/cancel?t={token}", data={"reason": "ошибка курса"})
            with get_conn(db_path) as conn:
                check("cancelling the exchange from the journal restores both accounts", accounts.balance(conn, cash_uah) == 2260 and accounts.balance(conn, usd) == -20)

            # ---- смена
            check("the dashboard invites to open the shift while none is open", "/cash/shift" in client.get(f"/?t={master_token}").text)
            shift_page = client.get(f"/cash/shift?t={master_token}").text
            check("any employee (a master too) sees «сверить остатки» with the точка's balances",
                  "Сверить остатки" in shift_page and "Наличные" in shift_page and "В товаре по всему бизнесу" in shift_page)
            empty_note = client.post(f"/cash/shift/open?t={master_token}", data={"mismatch": "1", "note": "  "})
            check("«есть расхождение» without saying what is a friendly error", "что именно не сходится" in empty_note.text)
            client.post(f"/cash/shift/open?t={master_token}", data={"mismatch": "1", "note": "не хватает 50 грн"})
            with get_conn(db_path) as conn:
                shift = shifts.current_shift(conn, master, 1)
                check("the shift opens with the discrepancy on record", shift is not None and shift["discrepancy_note"] == "не хватает 50 грн")
            check("the dashboard stops asking once the shift is open", "/cash/shift" not in client.get(f"/?t={master_token}").text)
            check("the owner's own shift is separate — still not open", "/cash/shift" in client.get(f"/?t={token}").text)
            client.post(f"/cash/shift/open?t={token}", data={})
            client.post(f"/cash/shift/close?t={token}", data={})
            with get_conn(db_path) as conn:
                check("«всё совпадает» opens it, «закрыть смену» closes it", shifts.current_shift(conn, owner, 1) is None
                      and conn.execute("SELECT COUNT(*) AS n FROM shifts WHERE staff_id = ? AND closed_at IS NOT NULL", (owner,)).fetchone()["n"] == 1)

            # ---- счета точки
            check("the accounts page is owner/admin only", client.get(f"/cash/accounts?t={keeper_token}").status_code == 403)
            client.post(f"/cash/accounts?t={token}", data={"name": "Монобанк", "kind": "card", "currency": "UAH"})
            with get_conn(db_path) as conn:
                mono = [a for a in accounts.list_accounts(conn, 1) if a["name"] == "Монобанк"]
            check("a new account appears in the точка's list and in the sale form",
                  len(mono) == 1 and f'name="pay_{mono[0]["id"]}"' in client.get(f"/sales?t={token}").text)
            client.post(f"/cash/accounts/{mono[0]['id']}?t={token}", data={"action": "rename", "name": "Моно ФОП"})
            client.post(f"/cash/accounts/{mono[0]['id']}?t={token}", data={"action": "off"})
            with get_conn(db_path) as conn:
                row = accounts.get_account(conn, mono[0]["id"])
            check("it can be renamed and switched off; switched off it leaves the sale form",
                  row["name"] == "Моно ФОП" and not row["active"] and f'name="pay_{mono[0]["id"]}"' not in client.get(f"/sales?t={token}").text)
            busy = client.post(f"/cash/accounts/{cash_uah}?t={token}", data={"action": "off"})
            check("switching off an account that holds money is a friendly error", "есть остаток" in busy.text)
            other = client.post(f"/cash/accounts/{acc[(2, 'cash', 'UAH')]}?t={token}", data={"action": "rename", "name": "Чужой"})
            check("another точка's account can't be edited from here", "не найден у этой точки" in other.text)


def _stock_is_consistent(conn) -> bool:
    """The Заход 3 invariant: every cell's total equals the sum of its партии."""
    rows = conn.execute(
        """SELECT stock.qty AS total,
                  COALESCE((SELECT SUM(bs.qty) FROM batch_stock bs JOIN batches b ON b.id = bs.batch_id
                            WHERE b.product_id = stock.product_id AND bs.cell_id = stock.cell_id), 0) AS by_batch
           FROM stock"""
    ).fetchall()
    return all(r["total"] == r["by_batch"] for r in rows)


def scenario_stock() -> None:
    """Заход 3 core: партии (origin + cost travel with the goods), FIFO,
    serial units by IMEI, склады мастеров, перемещение «Отправил → В пути →
    Принял» with a shortfall, cancelling stock documents."""
    print("scenario: склад — партии, IMEI, склады мастеров, перемещения, отмена складских документов")
    from core import stock_transfers, warehouses

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "stock.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "s-owner", "pass", "Владелец", "owner")
            keeper_2 = auth.create_staff(conn, "s-keeper2", "pass", "Кладовщик Магазина", "storekeeper", location_id=2)
            master = auth.create_master(conn, "Сергей", None, "percent", 33)
            master_row = auth.get_master(conn, master)
            keeper_2_row = auth.get_staff_by_id(conn, keeper_2)
            owner_row = auth.get_staff_by_id(conn, owner)
            sup_a = purchases.create_supplier(conn, "Поставщик А", None)
            sup_b = purchases.create_supplier(conn, "Поставщик Б", None)
            c1 = inventory.create_cell(conn, "ST-1", None, None, location_id=1)
            c2 = inventory.create_cell(conn, "ST-2", None, None, location_id=2)
            display = inventory.create_product(conn, "Дисплей iPhone 13", "ST-DSP", None, "шт", True, True, 0, 3500)

            # ---- один SKU — много партий
            r1 = purchases.create_receipt(conn, sup_a, "125", owner, [(display, c1, 2, 2000)], location_id=1)
            r2 = purchases.create_receipt(conn, sup_b, "138", owner, [(display, c1, 1, 2200)], location_id=1)
            batches = {b["supplier_name"]: b for b in inventory.list_batches(conn, display)}
            batch_a, batch_b = batches["Поставщик А"]["id"], batches["Поставщик Б"]["id"]
            check("each приход line became its own партия: supplier, receipt, cost, quantity left",
                  len(batches) == 2 and batches["Поставщик А"]["qty_left"] == 2 and batches["Поставщик А"]["unit_cost_uah"] == 2000
                  and batches["Поставщик Б"]["receipt_id"] == r2 and batches["Поставщик Б"]["unit_cost_uah"] == 2200)
            check("«в товаре» is exact by партия: 2 × 2000 + 1 × 2200", inventory.stock_value(conn) == 6200 and inventory.stock_value(conn, 2) == 0)

            ret = purchases.create_supplier_return(conn, display, sup_b, r2, c1, 1, "брак", owner)
            moved = conn.execute("SELECT * FROM stock_movements WHERE ref_type = 'supplier_return' AND ref_id = ?", (ret,)).fetchone()
            check("a return to supplier Б comes out of supplier Б's партия, not just the oldest one", moved["batch_id"] == batch_b)
            used = inventory.record_movement(conn, display, 1, "repair_use", owner, from_cell_id=c1, ref_type="repair_order", ref_id=1)
            row = conn.execute("SELECT * FROM stock_movements WHERE id = ?", (used,)).fetchone()
            check("with no партия named, stock leaves oldest-first and snapshots THAT партия's cost", row["batch_id"] == batch_a and row["unit_cost"] == 2000)
            try:
                inventory.record_movement(conn, display, 1, "repair_use", owner, from_cell_id=c1, batch_id=batch_b)
                check("taking from a named партия that is empty is refused", False)
            except inventory.InsufficientStockError:
                check("taking from a named партия that is empty is refused", True)
            check("and the refusal changed nothing (1 left, in партия А)",
                  inventory.product_total_qty(conn, display) == 1 and _stock_is_consistent(conn))

            # ---- приход в валюте
            cable = inventory.create_product(conn, "Кабель", "ST-CBL", None, "шт", False, True, 0, 300)
            usd_receipt = purchases.create_receipt(conn, sup_a, None, owner, [(cable, c1, 10, 3)], location_id=1, currency="USD", rate=41.5)
            usd_batch = purchases.get_receipt_batches(conn, usd_receipt)[0]
            check("a USD приход keeps the price in USD, the rate, and the гривня cost (3 × 41.5)",
                  usd_batch["unit_cost"] == 3 and usd_batch["currency"] == "USD" and usd_batch["rate"] == 41.5 and usd_batch["unit_cost_uah"] == 124.5)
            check("its document's amount is in гривня (10 × 124.5)", documents.get_for(conn, "receipt", usd_receipt)["amount"] == 1245)
            purchases.create_receipt(conn, sup_b, None, owner, [(cable, c1, 5, 100)], location_id=1)
            sale_id = sales.create_sale(conn, None, "offline", owner, [(cable, 12, 300)], location_id=1)
            item = sales.get_sale_items(conn, sale_id)[0]
            check("a sale spanning two партии remembers its real blended cost: (10 × 124.5 + 2 × 100) / 12",
                  item["unit_cost"] == 120.42)
            check("…written as one movement per партия",
                  [(m["qty"], m["unit_cost"]) for m in conn.execute(
                      "SELECT qty, unit_cost FROM stock_movements WHERE ref_type = 'sales_order' AND ref_id = ? ORDER BY id", (sale_id,))]
                  == [(10, 124.5), (2, 100)])

            # ---- серийный товар
            phone = inventory.create_product(conn, "iPhone 13 128GB", None, None, "шт", False, True, 0, 12000, is_serial=True)
            receipts_before = len(purchases.list_receipts(conn))
            for bad_imeis, needle, label in (
                (["356000000000001"], "нужно 2 IMEI", "a serial line with fewer IMEIs than units is refused"),
                (["356000000000001", "356 000 000 000 001"], "дважды", "the same IMEI twice in one приход is refused"),
            ):
                try:
                    purchases.create_receipt(conn, sup_a, None, owner, [(phone, c1, 2, 8000, bad_imeis)], location_id=1)
                    check(label, False)
                except purchases.ReceiptError as exc:
                    check(label, needle in str(exc))
            check("the refused приходы wrote nothing", len(purchases.list_receipts(conn)) == receipts_before)
            purchases.create_receipt(conn, sup_a, None, owner, [(phone, c1, 2, 8000, ["356 000 000 000 001", "356000000000002"])], location_id=1)
            unit_1 = inventory.find_unit_by_imei(conn, "356000000000001")
            unit_2 = inventory.find_unit_by_imei(conn, " 356000000000002 ")
            check("each phone is its own партия of one unit, found by IMEI in any spacing",
                  unit_1 is not None and unit_2 is not None and unit_1["id"] != unit_2["id"] and unit_1["qty"] == 1 and unit_1["cell_id"] == c1)
            try:
                purchases.create_receipt(conn, sup_b, None, owner, [(phone, c1, 1, 7000, ["356000000000001"])], location_id=1)
                check("an IMEI already in stock can't be received again", False)
            except purchases.ReceiptError:
                check("an IMEI already in stock can't be received again", True)
            try:
                sales.create_sale(conn, None, "offline", owner, [(phone, 1, 12000)], location_id=1)
                check("a serial product can't be sold without saying which unit", False)
            except sales.SaleError as exc:
                check("a serial product can't be sold without saying which unit", "выберите IMEI" in str(exc))
            phone_sale = sales.create_sale(conn, None, "offline", owner, [(phone, 1, 12000, unit_1["id"])], location_id=1)
            phone_item = sales.get_sale_items(conn, phone_sale)[0]
            check("the sale line carries the unit's IMEI, its supplier and its exact cost",
                  phone_item["imei"] == "356000000000001" and phone_item["unit_cost"] == 8000 and phone_item["supplier_name"] == "Поставщик А")
            check("the sold unit is gone from stock", inventory.find_unit_by_imei(conn, "356000000000001") is None)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", phone_sale)["id"], owner, "возврат")
            back = inventory.find_unit_by_imei(conn, "356000000000001")
            check("cancelling the sale puts the SAME unit back — same партия, same cost", back is not None and back["id"] == unit_1["id"])

            # ---- склады
            master_wh = warehouses.master_warehouse(conn, master)
            point_1, point_2 = warehouses.point_warehouse(conn, 1), warehouses.point_warehouse(conn, 2)
            check("a master gets his own склад the moment he is added",
                  master_wh is not None and warehouses.display_name(master_wh) == "Мастер: Сергей" and len(warehouses.cells(conn, master_wh["id"])) == 1)
            check("склады overview: both точки, the master, «В пути»",
                  [w["kind"] for w in warehouses.summary(conn)] == ["point", "point", "master", "transit"])
            check("who sends from where: master — his own, a storekeeper — their точка's, owner — any but «В пути»",
                  [w["id"] for w in warehouses.sendable_from(conn, master_row, 1)] == [master_wh["id"]]
                  and [w["id"] for w in warehouses.sendable_from(conn, keeper_2_row, 2)] == [point_2["id"]]
                  and len(warehouses.sendable_from(conn, owner_row, 1)) == 3)

            # ---- перемещение точка → мастер, с недостачей
            lines = {(l["product_id"], l["imei"]): l for l in inventory.stock_lines(conn, warehouse_id=point_1["id"])}
            cable_line, phone_line = lines[(cable, None)], lines[(phone, "356000000000002")]
            for bad, needle, label in (
                ((point_1["id"], master_wh["id"], [(cable_line["batch_id"], cable_line["cell_id"], 99)]), "нельзя отправить", "sending more than the партия holds is refused"),
                ((point_1["id"], point_1["id"], [(cable_line["batch_id"], cable_line["cell_id"], 1)]), "должен быть другим", "sending to the same склад is refused"),
                ((point_1["id"], master_wh["id"], []), "хотя бы одну", "an empty transfer is refused"),
            ):
                try:
                    stock_transfers.send(conn, *bad, owner)
                    check(label, False)
                except stock_transfers.TransferError as exc:
                    check(label, needle in str(exc))
            transfer_id = stock_transfers.send(
                conn, point_1["id"], master_wh["id"],
                [(cable_line["batch_id"], cable_line["cell_id"], 2), (phone_line["batch_id"], phone_line["cell_id"], 1)], owner,
            )
            check("sent goods leave the точка at once and are «в пути», not yet the master's",
                  inventory.product_total_qty(conn, cable, 1) == 1 and inventory.product_total_qty(conn, phone, 1) == 1
                  and warehouses.total_qty(conn, master_wh["id"]) == 0
                  and len(inventory.stock_lines(conn, warehouse_id=warehouses.list_warehouses(conn)[-1]["id"])) == 2)
            try:
                sales.create_sale(conn, None, "offline", owner, [(phone, 1, 12000, unit_2["id"])], location_id=1)
                check("a unit that is «в пути» can't be sold", False)
            except inventory.InsufficientStockError:
                check("a unit that is «в пути» can't be sold", True)
            transfer_doc = stock_transfers.document(conn, transfer_id)
            check("the transfer is a document (ПМ) with its route",
                  documents.doc_label(transfer_doc).startswith("ПМ-") and "Мастерская → Мастер: Сергей" in transfer_doc["title"]
                  and documents.source_path(transfer_doc) == f"/transfers/{transfer_id}")
            target = warehouses.get_warehouse(conn, master_wh["id"])
            check("only the master (or an owner) may say «Принял» for his склад — not another точка's storekeeper",
                  warehouses.may_receive(conn, master_row, target, set()) and warehouses.may_receive(conn, owner_row, target, {1, 2})
                  and not warehouses.may_receive(conn, keeper_2_row, target, {2}))
            items = {i["product_id"]: i for i in stock_transfers.get_items(conn, transfer_id)}
            try:
                stock_transfers.receive(conn, transfer_id, master, {items[cable]["id"]: 1})
                check("receiving less than was sent without a note is refused", False)
            except stock_transfers.TransferError:
                check("receiving less than was sent without a note is refused", True)
            stock_transfers.receive(conn, transfer_id, master, {items[cable]["id"]: 1}, note="одного кабеля нет в пакете")
            check("what arrived is the master's responsibility now: 1 cable + the phone",
                  warehouses.total_qty(conn, master_wh["id"]) == 2
                  and inventory.find_unit_by_imei(conn, "356000000000002")["cell_id"] == warehouses.cells(conn, master_wh["id"])[0]["id"])
            check("the missing cable is still «в пути», flagged on the transfer",
                  [(i["product_id"], i["qty"] - i["received_qty"]) for i in stock_transfers.shortage(conn, transfer_id)] == [(cable, 1)]
                  and [t["id"] for t in stock_transfers.list_discrepancies(conn)] == [transfer_id]
                  and [e["event"] for e in documents.get_events(conn, transfer_doc["id"])] == ["created", "discrepancy"])
            try:
                stock_transfers.receive(conn, transfer_id, master)
                check("a transfer can't be received twice", False)
            except stock_transfers.TransferError:
                check("a transfer can't be received twice", True)
            writeoffs_before = len(documents.list_journal(conn, doc_type="writeoff"))
            stock_transfers.write_off_shortage(conn, transfer_id, owner, "утеряно при передаче")
            check("the owner settles the недостача: written off from «В пути» by a списание document",
                  stock_transfers.shortage(conn, transfer_id) == []
                  and len(documents.list_journal(conn, doc_type="writeoff")) == writeoffs_before + 1
                  and inventory.product_total_qty(conn, cable) == 2)

            # ---- мастер → другая точка (готовый аппарат возвращается на витрину)
            master_line = next(l for l in inventory.stock_lines(conn, warehouse_id=master_wh["id"]) if l["imei"])
            back_id = stock_transfers.send(conn, master_wh["id"], point_2["id"], [(master_line["batch_id"], master_line["cell_id"], 1)], master)
            stock_transfers.receive(conn, back_id, keeper_2)
            check("received with no cell named, it lands in the destination's cell; origin and cost came along",
                  inventory.find_unit_by_imei(conn, "356000000000002")["cell_id"] == c2
                  and inventory.product_total_qty(conn, phone, 2) == 1 and inventory.get_batch(conn, unit_2["id"])["unit_cost_uah"] == 8000)
            sold_at_2 = sales.create_sale(conn, None, "offline", keeper_2, [(phone, 1, 12500, unit_2["id"])], location_id=2)
            check("and can be sold there, at its original cost", sales.get_sale_items(conn, sold_at_2)[0]["unit_cost"] == 8000)

            # ---- отмена складских документов
            last_cable = next(l for l in inventory.stock_lines(conn, warehouse_id=point_1["id"]) if l["product_id"] == cable)
            pending = stock_transfers.send(conn, point_1["id"], point_2["id"], [(last_cable["batch_id"], last_cable["cell_id"], 1)], owner)
            doc_cancel.cancel_document(conn, stock_transfers.document(conn, pending)["id"], owner, "передумали")
            check("cancelling a transfer still «в пути» returns the goods to the cell they left",
                  inventory.product_total_qty(conn, cable, 1) == 1 and stock_transfers.get_transfer(conn, pending)["status"] == "cancelled")
            try:
                doc_cancel.cancel_document(conn, transfer_doc["id"], owner, "поздно")
                check("a transfer already received can't be cancelled", False)
            except documents.DocumentError:
                check("a transfer already received can't be cancelled", True)

            glass = inventory.create_product(conn, "Стекло", "ST-GLS", None, "шт", True, True, 0, 200)
            fresh = purchases.create_receipt(conn, sup_a, None, owner, [(glass, c1, 5, 50)], location_id=1)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "receipt", fresh)["id"], owner, "не тот поставщик")
            check("an untouched приход can be cancelled: its goods leave again", inventory.product_total_qty(conn, glass) == 0)
            touched = purchases.create_receipt(conn, sup_a, None, owner, [(glass, c1, 5, 50)], location_id=1)
            sales.create_sale(conn, None, "offline", owner, [(glass, 1, 200)], location_id=1)
            try:
                doc_cancel.cancel_document(conn, documents.get_for(conn, "receipt", touched)["id"], owner, "поздно")
                check("a приход whose goods were partly sold can't be cancelled", False)
            except documents.DocumentError as exc:
                check("a приход whose goods were partly sold can't be cancelled", "уже частично" in str(exc))
            check("and the refusal moved nothing", inventory.product_total_qty(conn, glass) == 4)
            inventory.receive_stock(conn, glass, c1, 3, owner, key="st-in")
            doc_cancel.cancel_document(conn, documents.find_by_key(conn, "st-in")["id"], owner, "пересчитали")
            inventory.write_off_stock(conn, glass, c1, 2, owner, comment="бой", key="st-out")
            doc_cancel.cancel_document(conn, documents.find_by_key(conn, "st-out")["id"], owner, "нашлись")
            check("a manual оприходование and a списание can both be cancelled (4 + 3 − 3 − 2 + 2)",
                  inventory.product_total_qty(conn, glass) == 4)
            try:
                inventory.receive_stock(conn, phone, c1, 1, owner)
                check("a serial product can't be оприходован without an IMEI", False)
            except inventory.SerialUnitError:
                check("a serial product can't be оприходован without an IMEI", True)
            check("after all of the above every cell's total still equals the sum of its партии", _stock_is_consistent(conn))

            # ---- остаток, попавший мимо партий
            orphan = inventory.create_product(conn, "Старый остаток", "ST-OLD", None, "шт", False, True, 0, 10)
            conn.execute("INSERT INTO stock (product_id, cell_id, qty) VALUES (?, ?, 3)", (orphan, c1))
            inventory.record_movement(conn, orphan, 2, "adjustment", owner, from_cell_id=c1, comment="тест")
            check("stock with no партия behind it heals itself on first use (a 'legacy' партия)",
                  _stock_is_consistent(conn) and inventory.list_batches(conn, orphan)[0]["source"] == "legacy")
            conn.execute("INSERT INTO stock (product_id, cell_id, qty) VALUES (?, ?, 6)", (orphan, c2))
        init_db(db_path)
        with get_conn(db_path) as conn:
            check("and init_db covers whatever is left uncovered at startup", _stock_is_consistent(conn))


def scenario_stock_http() -> None:
    print("scenario: склад over HTTP — серийный товар, приход с IMEI и валютой, продажа по IMEI, склады, перемещение")
    from core import stock_transfers, warehouses

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "stock-http.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "sh-owner", "pass", "Владелец", "owner")
            keeper = auth.create_staff(conn, "sh-keeper", "pass", "Кладовщик", "storekeeper", location_id=1)
            keeper_2 = auth.create_staff(conn, "sh-keeper2", "pass", "Кладовщик Второй", "storekeeper", location_id=2)
            master = auth.create_master(conn, "Мастер Аутсорс", None, None, None)
            supplier = purchases.create_supplier(conn, "Поставщик HTTP", None)
            cell = inventory.create_cell(conn, "SH-1", None, None, location_id=1)
            cell_2 = inventory.create_cell(conn, "SH-2", None, None, location_id=2)
            part = inventory.create_product(conn, "АКБ iPhone 13", "SH-AKB", None, "шт", True, True, 0, 900)
            master_wh = warehouses.master_warehouse(conn, master)["id"]
            point_1 = warehouses.point_warehouse(conn, 1)["id"]
        token, keeper_token, keeper2_token = make_token(owner, "1"), make_token(keeper, "1"), make_token(keeper_2, "2")
        master_token = make_token(master, "1")

        import webapp.main

        with TestClient(webapp.main.app) as client:
            created = client.post(f"/inventory/products?t={token}", data={
                "name": "iPhone 13 Black", "sku": "", "unit": "шт", "price": "15000", "is_sellable": "on", "is_serial": "on"},
                follow_redirects=False)
            phone = int(created.headers["location"].split("/")[-1].split("?")[0])
            with get_conn(db_path) as conn:
                check("a product can be marked серийный on creation", inventory.get_product(conn, phone)["is_serial"] == 1)
            check("its card asks for an IMEI when adding stock by hand", 'name="imei"' in client.get(f"/inventory/products/{phone}?t={token}").text)
            no_imei = client.post(f"/inventory/products/{phone}/receive?t={token}", data={"cell_id": str(cell), "qty": "1"})
            check("adding a serial unit without an IMEI is a friendly error", no_imei.status_code == 200 and "с IMEI" in no_imei.text)

            page = client.get(f"/purchases?t={token}").text
            check("the приход form has the invoice currency, a rate field and IMEI rows wired for serial products",
                  'name="currency"' in page and 'name="rate"' in page and 'name="imeis_0"' in page and f"[{phone}]" in page.replace(" ", ""))
            base = {"supplier_id": str(supplier), "row_count": "2",
                    "product_id_0": str(phone), "product_name_0": "iPhone 13 Black", "cell_id_0": str(cell), "qty_0": "2", "unit_cost_0": "200",
                    "product_id_1": str(part), "product_name_1": "АКБ iPhone 13", "cell_id_1": str(cell), "qty_1": "4", "unit_cost_1": "12,5"}
            no_rate = client.post(f"/purchases?t={token}", data=dict(base, currency="USD", imeis_0="111111111111111\n222222222222222"))
            check("a USD накладная without a rate is refused", "Укажите курс" in no_rate.text)
            short = client.post(f"/purchases?t={token}", data=dict(base, currency="USD", rate="41", imeis_0="111111111111111"))
            check("fewer IMEIs than phones is refused, naming the product", "нужно 2 IMEI" in short.text and "iPhone 13 Black" in short.text)
            with get_conn(db_path) as conn:
                check("neither refused приход left anything", len(purchases.list_receipts(conn)) == 0 and inventory.product_total_qty(conn, part) == 0)
            ok = client.post(f"/purchases?t={token}", data=dict(base, currency="USD", rate="41", imeis_0="111111111111111, 222222222222222"),
                             follow_redirects=False)
            check("a correct one goes through", ok.status_code == 303)
            with get_conn(db_path) as conn:
                receipt_id = purchases.list_receipts(conn)[0]["id"]
                unit = inventory.find_unit_by_imei(conn, "111111111111111")
                check("phones came in as two units costing 200 USD × 41 = 8200 грн each; parts as one партия at 12.5 × 41",
                      unit is not None and unit["unit_cost_uah"] == 8200 and inventory.product_total_qty(conn, phone, 1) == 2
                      and inventory.list_batches(conn, part)[0]["unit_cost_uah"] == 512.5)
            receipt_page = client.get(f"/purchases/{receipt_id}?t={token}").text
            check("the приход's page lists its партии with IMEIs and the currency", "IMEI 111111111111111" in receipt_page and "USD" in receipt_page)
            card = client.get(f"/inventory/products/{phone}?t={token}").text
            check("the product card shows its партии: IMEI, supplier, cost, where", "IMEI 222222222222222" in card and "Поставщик HTTP" in card and "SH-1" in card)
            found = client.get(f"/warehouse/find?t={token}&code=222222222222222", follow_redirects=False)
            check("scanning a phone's IMEI on «Склад» opens its product card",
                  found.status_code == 303 and f"/inventory/products/{phone}" in found.headers["location"])

            # ---- продажа серийного
            sales_page = client.get(f"/sales?t={token}").text
            check("the sale form knows this точка's serial units and has an IMEI field per row",
                  "111111111111111" in sales_page and 'name="imei_0"' in sales_page)
            sale = {"row_count": "1", "product_id_0": str(phone), "product_name_0": "iPhone 13 Black", "qty_0": "1", "price_0": "15000", "channel": "offline"}
            check("selling a serial product with no IMEI is refused", "выберите IMEI" in client.post(f"/sales?t={token}", data=sale).text)
            check("…and with an IMEI that isn't in stock too", "не найден на остатке" in client.post(f"/sales?t={token}", data=dict(sale, imei_0="999")).text)
            sold = client.post(f"/sales?t={token}", data=dict(sale, imei_0="111 111 111 111 111"), follow_redirects=False)
            check("with the right IMEI (typed with spaces) it sells", sold.status_code == 303)
            check("the sale's page names the IMEI", "IMEI 111111111111111" in client.get(sold.headers["location"]).text)
            check("the sold IMEI is no longer offered", "111111111111111" not in client.get(f"/sales?t={token}").text)

            # ---- склады
            overview = client.get(f"/inventory/warehouses?t={keeper_token}").text
            check("«Склады» lists both точки, the master's склад and «В пути»",
                  all(x in overview for x in ("Мастерская", "Магазин", "Мастер: Мастер Аутсорс", "В пути")))
            check("a склад's page lists what it holds, by партия",
                  "IMEI 222222222222222" in client.get(f"/inventory/warehouses/{point_1}?t={keeper_token}").text)
            check("the Склад hub links to Перемещение and Склады",
                  "/transfers" in client.get(f"/warehouse?t={token}").text and "/inventory/warehouses" in client.get(f"/warehouse?t={token}").text)

            # ---- перемещение точка → мастер
            form_page = client.get(f"/transfers?t={keeper_token}").text
            check("the transfer form offers this точка's stock lines and the other склады as destinations",
                  # (line labels sit in a JSON blob, Cyrillic escaped — match the ASCII cell code and qty)
                  "SH-1 \\u00b7 4" in form_page and "Мастер: Мастер Аутсорс" in form_page and "transferForm" in form_page)
            with get_conn(db_path) as conn:
                lines = {l["product_id"]: l for l in inventory.stock_lines(conn, warehouse_id=point_1)}
            part_line = f"{lines[part]['batch_id']}:{lines[part]['cell_id']}"
            phone_line = f"{lines[phone]['batch_id']}:{lines[phone]['cell_id']}"
            send = {"from_warehouse_id": str(point_1), "to_warehouse_id": str(master_wh), "row_count": "2",
                    "line_0": "АКБ", "line_id_0": part_line, "qty_0": "3", "line_1": "iPhone", "line_id_1": phone_line, "qty_1": "1"}
            foreign = client.post(f"/transfers?t={keeper2_token}", data=send)
            check("a storekeeper of another точка can't send from this точка's склад", "отправлять не можете" in foreign.text)
            too_many = client.post(f"/transfers?t={keeper_token}", data=dict(send, qty_0="40"))
            check("sending more than there is is a friendly error", too_many.status_code == 200 and "нельзя отправить" in too_many.text)
            unpicked = client.post(f"/transfers?t={keeper_token}", data=dict(send, line_id_1=""))
            check("a typed line that wasn't picked from the list is reported, not skipped", "выберите позицию из списка" in unpicked.text)
            sent = client.post(f"/transfers?t={keeper_token}", data=send, follow_redirects=False)
            check("a valid transfer is sent and opens its own page", sent.status_code == 303 and "/transfers/" in sent.headers["location"])
            transfer_id = int(sent.headers["location"].split("/transfers/")[1].split("?")[0])
            with get_conn(db_path) as conn:
                check("the goods left the точка (1 part and no phone remain here)",
                      inventory.product_total_qty(conn, part, 1) == 1 and inventory.product_total_qty(conn, phone, 1) == 0)
            detail_sender = client.get(f"/transfers/{transfer_id}?t={keeper_token}").text
            check("the sender sees it «в пути», with no receive form (not theirs to accept)",
                  "В пути" in detail_sender and f"/transfers/{transfer_id}/receive" not in detail_sender)
            check("and can't force the receive either",
                  "отвечает за склад-получатель" in client.post(f"/transfers/{transfer_id}/receive?t={keeper_token}", data={}).text)
            check("the master's Склад hub and transfer list show it waiting for him",
                  "Перемещение · 1" in client.get(f"/warehouse?t={master_token}").text
                  and f"/transfers/{transfer_id}" in client.get(f"/transfers?t={master_token}").text)
            detail_master = client.get(f"/transfers/{transfer_id}?t={master_token}").text
            check("the master's view of it has the receive form", f"/transfers/{transfer_id}/receive" in detail_master)
            with get_conn(db_path) as conn:
                items = {i["product_id"]: i["id"] for i in stock_transfers.get_items(conn, transfer_id)}
            short_recv = client.post(f"/transfers/{transfer_id}/receive?t={master_token}", data={f"recv_{items[part]}": "2", f"recv_{items[phone]}": "1"})
            check("accepting fewer than sent without a note is a friendly error", "напишите, чего не хватает" in short_recv.text)
            client.post(f"/transfers/{transfer_id}/receive?t={master_token}",
                        data={f"recv_{items[part]}": "2", f"recv_{items[phone]}": "1", "note": "одной АКБ нет"})
            with get_conn(db_path) as conn:
                check("the master now holds 2 parts and the phone", warehouses.total_qty(conn, master_wh) == 3)
            detail_after = client.get(f"/transfers/{transfer_id}?t={token}").text
            check("the transfer's page shows the недостача and offers the owner to write it off",
                  "одной АКБ нет" in detail_after and f"/transfers/{transfer_id}/shortage" in detail_after)
            check("a storekeeper is not offered (and can't post) the write-off",
                  f"/transfers/{transfer_id}/shortage" not in client.get(f"/transfers/{transfer_id}?t={keeper_token}").text
                  and "только владелец" in client.post(f"/transfers/{transfer_id}/shortage?t={keeper_token}", data={"reason": "x"}).text)
            client.post(f"/transfers/{transfer_id}/shortage?t={token}", data={"reason": "утеряна"})
            with get_conn(db_path) as conn:
                check("after the owner's write-off nothing is left «в пути»", stock_transfers.shortage(conn, transfer_id) == []
                      and inventory.product_total_qty(conn, part) == 3)
            journal = client.get(f"/journal?t={token}").text
            check("the journal shows the transfer (ПМ) and links it to its page",
                  "ПМ-" in journal and "Мастерская → Мастер: Мастер Аутсорс" in journal)
            card = client.get(f"/inventory/products/{phone}?t={token}").text
            check("the phone's product card now says it is with the master", "Мастер Аутсорс" in card)


def scenario_transfer_chat() -> None:
    """«🔁 Перемещение» in the bot, end to end: a storekeeper sends a part
    and a phone to a master, the master gets a DM and presses «Принял всё»."""
    print("scenario: перемещение в боте — отправка кладовщиком, уведомление и приём мастером")
    import asyncio
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa
    from bot import transfer_flow as tf
    from core import stock_transfers, warehouses

    KEEPER_TG, MASTER_TG, CHAT_ID = 881001, 881002, 881001

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "transfer-chat.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "tc-owner", "pass", "Владелец", "owner")
            keeper = auth.create_staff(conn, "tc-keeper", "pass", "Виталий", "storekeeper", location_id=1)
            auth.link_staff_telegram(conn, "tc-keeper", KEEPER_TG)
            master = auth.create_master(conn, "Сергей", MASTER_TG, None, None)
            cell = inventory.create_cell(conn, "TC-1", None, None, location_id=1)
            part = inventory.create_product(conn, "Дисплей iPhone 13", "TC-DSP", None, "шт", True, True, 0, 3000)
            phone = inventory.create_product(conn, "iPhone 13", None, None, "шт", False, True, 0, 12000, is_serial=True)
            purchases.create_receipt(conn, None, None, owner, [(part, cell, 5, 2000), (phone, cell, 1, 8000, ["356000000000777"])], location_id=1)
            shifts.open_shift(conn, keeper, 1)
            master_wh = warehouses.master_warehouse(conn, master)["id"]

        sent_dms: list[dict] = []

        class _Chat:
            def __init__(self):
                self.log, self._next = [], 100

            def add(self, author, text, markup=None):
                self._next += 1
                self.log.append({"id": self._next, "author": author, "text": text, "markup": markup})
                return self._next

            def delete(self, message_id):
                self.log = [m for m in self.log if m["id"] != message_id]

            def edit(self, message_id, text, markup):
                for m in self.log:
                    if m["id"] == message_id:
                        m["text"], m["markup"] = text, markup

        class _Bot:
            def __init__(self, chat):
                self.chat = chat

            async def delete_message(self, chat_id, message_id):
                self.chat.delete(message_id)

            async def delete_messages(self, chat_id, message_ids):
                for mid in message_ids:
                    self.chat.delete(mid)

            async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
                self.chat.edit(message_id, text, reply_markup)

            async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                sent_dms.append({"to": chat_id, "text": text, "markup": reply_markup})

        class _Msg:
            def __init__(self, chat, bot, text):
                self._log, self.bot, self.text = chat, bot, text
                self.chat = SimpleNamespace(id=CHAT_ID, type="private")
                self.from_user = SimpleNamespace(id=KEEPER_TG, full_name="Виталий")
                self.message_id = chat.add("staff", text)

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

        class _Cb:
            def __init__(self, chat, bot, data, message_id, user_id=KEEPER_TG):
                self.data, self.bot = data, bot
                self.from_user = SimpleNamespace(id=user_id)
                self.message = SimpleNamespace(chat=SimpleNamespace(id=CHAT_ID, type="private"), message_id=message_id)
                self.answered = []

            async def answer(self, text=None, show_alert=False):
                self.answered.append((text, show_alert))

        def _buttons(markup) -> list[str]:
            return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] if markup else []

        async def run() -> None:
            chat = _Chat()
            bot = _Bot(chat)
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=KEEPER_TG))
            say = lambda text: _Msg(chat, bot, text)
            tap = lambda data, user=KEEPER_TG: _Cb(chat, bot, data, chat.log[-1]["id"], user)
            screen = lambda: [m for m in chat.log if m["author"] == "bot"][-1]

            await tf.transfer_start(say(qa.BTN_TRANSFER), state)
            check("«Перемещение» names the sender's склад and offers the other склады as destinations",
                  "Со склада: Мастерская" in screen()["text"] and f"tr_to:{master_wh}" in _buttons(screen()["markup"]))
            await tf.transfer_pick_target(tap(f"tr_to:{master_wh}"), state)
            check("then asks what to move", "Мастерская → Мастер: Сергей" in screen()["text"] and "IMEI" in screen()["text"])

            await tf.transfer_search(say("ничего такого"), state)
            check("an unknown item keeps the question up with a warning", "не нашёл" in screen()["text"] and "Что перемещаем" in screen()["text"])
            await tf.transfer_search(say("дисплей"), state)
            check("a non-serial item found by part of its name asks how many, showing what is available",
                  "Сколько перемещаем" in screen()["text"] and "На складе: 5" in screen()["text"])
            await tf.transfer_got_qty(say("9"), state)
            check("more than available is refused", "не больше 5" in screen()["text"])
            await tf.transfer_got_qty(say("2"), state)
            check("a valid quantity lands on the review screen with send / add-more buttons",
                  "Дисплей iPhone 13" in screen()["text"] and "— 2 шт" in screen()["text"]
                  and {"tr_more", "tr_send", "tr_cancel"} <= set(_buttons(screen()["markup"])))
            await tf.transfer_more(tap("tr_more"), state)
            await tf.transfer_search(say("356000000000777"), state)
            check("a scanned IMEI adds that exact phone straight away (no quantity question)",
                  "IMEI 356000000000777" in screen()["text"] and "— 1 шт" in screen()["text"] and "Сколько" not in screen()["text"])
            check("the chat holds exactly one bot screen and none of the employee's messages (Clean Chat)",
                  [m["author"] for m in chat.log] == ["bot"])

            send_tap = tap("tr_send")
            await tf.transfer_send(send_tap, state)
            check("sending confirms with the document number and says the goods are in transit",
                  "ПМ-" in screen()["text"] and "отправлено" in screen()["text"] and "в пути" in screen()["text"].lower())
            with get_conn(db_path) as conn:
                transfer = stock_transfers.list_transfers(conn, status="sent")[0]
                check("the transfer exists, from the точка to the master, with both lines",
                      transfer["to_warehouse_id"] == master_wh and len(stock_transfers.get_items(conn, transfer["id"])) == 2
                      and inventory.product_total_qty(conn, part, 1) == 3 and warehouses.total_qty(conn, master_wh) == 0)
            to_master = [d for d in sent_dms if d["to"] == MASTER_TG]
            check("the master gets a DM listing the items, with a «Принял всё» button; the sender gets none",
                  len(to_master) == 1 and "IMEI 356000000000777" in to_master[0]["text"]
                  and f"tr_recv:{transfer['id']}" in _buttons(to_master[0]["markup"])
                  and KEEPER_TG not in [d["to"] for d in sent_dms])
            await tf.transfer_send(_Cb(chat, bot, "tr_send", send_tap.message.message_id), state)
            with get_conn(db_path) as conn:
                check("a second tap on «Отправить» creates no second transfer", len(stock_transfers.list_transfers(conn)) == 1)

            stranger = _Cb(chat, bot, f"tr_recv:{transfer['id']}", 1, KEEPER_TG)
            await tf.transfer_receive(stranger)
            check("the sender can't press «Принял» for the master",
                  stranger.answered and stranger.answered[-1][1] is True and "отвечает за склад" in stranger.answered[-1][0])
            notice_id = chat.add("bot", to_master[0]["text"], to_master[0]["markup"])
            await tf.transfer_receive(_Cb(chat, bot, f"tr_recv:{transfer['id']}", notice_id, MASTER_TG))
            with get_conn(db_path) as conn:
                check("the master's «Принял всё» puts everything on his склад",
                      stock_transfers.get_transfer(conn, transfer["id"])["status"] == "received"
                      and warehouses.total_qty(conn, master_wh) == 3 and _stock_is_consistent(conn))
            check("and his notification turns into a receipt confirmation with no buttons",
                  "Принято" in chat.log[-1]["text"] and not chat.log[-1]["markup"])

        asyncio.run(run())


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
        scenario_supplier_returns(db_path)
        scenario_purchase_import(db_path)
        scenario_purchase_drafts_and_vision(db_path)
        scenario_sales(db_path)
        scenario_cash(db_path)
        scenario_masters(db_path)
        scenario_repair_card_notify(db_path)
        scenario_repair_actions(db_path)
        scenario_quick_cash_chat(db_path)
        scenario_quick_client_search_chat(db_path)
        scenario_repair_attachments(db_path)
        scenario_phone_normalization(db_path)
        scenario_store_settings(db_path)

    scenario_timefmt()
    scenario_telegram_auth()
    scenario_session_token()
    scenario_stores_config()
    scenario_storage_context()
    scenario_store_prefs()
    scenario_store_access()
    scenario_miniapp_boot_template()
    scenario_quick_intake_chat()
    scenario_bot_html_safety()
    scenario_webapp_forms(_WEBAPP_TEST_DB)
    scenario_buyback_http(_WEBAPP_TEST_DB)
    scenario_multi_store_http()
    scenario_multi_store_login_and_switch_http()
    scenario_store_settings_http()
    scenario_all_stores_report_http()
    scenario_sales_channel_http()
    scenario_documents_journal()
    scenario_journal_http()
    scenario_merge_stores_tool()
    scenario_money()
    scenario_money_http()
    scenario_stock()
    scenario_stock_http()
    scenario_transfer_chat()
    if os.path.exists(_WEBAPP_TEST_DB):
        os.remove(_WEBAPP_TEST_DB)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
