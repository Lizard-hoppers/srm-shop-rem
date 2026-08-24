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
# Same reasoning, same unconditional-overwrite fix, for Фаза B's registry:
# core.stores.load_stores() falls back to a synthetic single store built
# from CRM_DB_PATH ONLY when stores.json doesn't exist at CRM_STORES_CONFIG
# (or the default ./stores.json next to this file) — and since Фаза A this
# repo's real deploy directory legitimately HAS a real stores.json (pointing
# at the real crm.sqlite3/store2.sqlite3/store3.sqlite3). Running this suite
# from inside that directory without overriding CRM_STORES_CONFIG made every
# make_token(staff_id) call (no explicit store_id) default to the real store
# 1 instead of the throwaway _WEBAPP_TEST_DB above — caught 23.08 while
# testing Фаза B: scenario_webapp_forms silently authenticated against an
# empty real-shaped db and its later assertions crashed on missing rows.
# Point this at a path that can never exist so the legacy single-store
# fallback always wins by default; scenarios that need a real multi-store
# stores.json set CRM_STORES_CONFIG themselves and restore this value after.
os.environ["CRM_STORES_CONFIG"] = os.path.join(tempfile.gettempdir(), "crm_simulate_tests_no_such_stores.json")

import hashlib
import hmac
import json
import re
import time
import urllib.parse

import httpx
import jinja2
from PIL import Image

from core import auth, barcode_label, cash, clients, device_catalog, inventory, masters, purchases, qr, repairs, sales, store_access, store_prefs, store_settings, stores, timefmt
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
    kb_in_progress = repairs.render_keyboard(order_id, "in_progress")
    check("keyboard for 'in_progress' offers done + release",
          {b["callback_data"] for b in kb_in_progress["inline_keyboard"][0]}
          == {f"repair_done:{order_id}", f"repair_release:{order_id}"})
    check("keyboard for 'ready' has nothing left to press", repairs.render_keyboard(order_id, "ready") is None)

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
    print("scenario: store registry (core/stores.py)")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    try:
        # No stores.json at this path -> single synthetic store from the
        # legacy CRM_DB_PATH/CRM_*_GROUP_CHAT_ID env vars (backward compat).
        os.environ["CRM_STORES_CONFIG"] = os.path.join(tempfile.gettempdir(), "definitely-not-a-real-stores-file.json")
        legacy = stores.load_stores()
        check("no stores.json -> exactly one synthetic store", len(legacy) == 1)
        check("synthetic store's db_path is the legacy CRM_DB_PATH", legacy[0].db_path == storage.DB_PATH)
        check("default_store_id() picks the only store", stores.default_store_id() == legacy[0].id)

        with tempfile.TemporaryDirectory() as tmp:
            config_path = os.path.join(tmp, "stores.json")
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(
                    [
                        {"id": "1", "name": "Первый", "db_path": "s1.sqlite3", "staff_group_chat_id": -100, "repair_topic_id": 5, "masters_group_chat_id": -200},
                        {"id": "2", "name": "Второй", "db_path": os.path.join(tmp, "s2.sqlite3")},
                    ],
                    f,
                )
            os.environ["CRM_STORES_CONFIG"] = config_path
            parsed = stores.load_stores()
            check("stores.json with 2 entries parses to 2 stores", len(parsed) == 2)
            check("relative db_path is resolved against the repo root", os.path.isabs(parsed[0].db_path))
            check("absolute db_path passes through unchanged", parsed[1].db_path == os.path.join(tmp, "s2.sqlite3"))
            check("optional group-chat fields default to None when omitted", parsed[1].staff_group_chat_id is None)
            check("get_store finds a known id", stores.get_store("2").name == "Второй")
            check("default_store_id() is the first entry", stores.default_store_id() == "1")
            try:
                stores.get_store("nope")
                check("get_store raises on an unknown id", False)
            except KeyError:
                check("get_store raises on an unknown id", True)

            # store_for_chat_id (Фаза C, 23.08) — group -> store reverse lookup.
            check("store_for_chat_id resolves the staff group to its store", stores.store_for_chat_id(-100).id == "1")
            check("store_for_chat_id resolves the masters group to its store too", stores.store_for_chat_id(-200).id == "1")
            check("a topic id is not a chat_id — resolves to nothing", stores.store_for_chat_id(5) is None)
            check("an unrecognized chat_id resolves to no store", stores.store_for_chat_id(-999) is None)
            check("store_for_chat_id accepts a string chat_id too (Telegram hands either)",
                  stores.store_for_chat_id("-100").id == "1")
            check("a non-numeric chat_id resolves to no store, doesn't raise", stores.store_for_chat_id("not-a-number") is None)
    finally:
        if prev_config is None:
            os.environ.pop("CRM_STORES_CONFIG", None)
        else:
            os.environ["CRM_STORES_CONFIG"] = prev_config


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
    print("scenario: cross-store telegram_id lookup (core.store_access)")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    prev_prefs = os.environ.get("CRM_STORE_PREFS_PATH")
    with tempfile.TemporaryDirectory() as tmp:
        db1, db2, db3 = (os.path.join(tmp, f"s{i}.sqlite3") for i in (1, 2, 3))
        for db_path in (db1, db2, db3):
            init_db(db_path)
        config_path = os.path.join(tmp, "stores.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"id": "1", "name": "Один", "db_path": db1},
                    {"id": "2", "name": "Два", "db_path": db2},
                    {"id": "3", "name": "Три", "db_path": db3},
                ],
                f,
            )
        os.environ["CRM_STORES_CONFIG"] = config_path
        os.environ["CRM_STORE_PREFS_PATH"] = os.path.join(tmp, "prefs.sqlite3")
        store_prefs.init_db()
        try:
            with get_conn(db1) as conn:
                owner_id_1 = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
                auth.link_staff_telegram(conn, "owner", 777)
            with get_conn(db3) as conn:
                owner_id_3 = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
                auth.link_staff_telegram(conn, "owner", 777)
            with get_conn(db2) as conn:
                auth.create_staff(conn, "somemaster", "pass", "Мастер Два", "master")
                # telegram_id 777 deliberately NOT linked in store 2

            found = store_access.accessible_stores(777)
            check("accessible_stores finds exactly the 2 stores this telegram_id has an identity in",
                  {s.id for s, _ in found} == {"1", "3"})
            check("each match carries the right staff_id for its own store",
                  {s.id: st["id"] for s, st in found} == {"1": owner_id_1, "3": owner_id_3})
            check("a telegram_id with no identity anywhere finds nothing", store_access.accessible_stores(4242) == [])

            single = [(s, st) for s, st in found if s.id == "1"]
            check("pick_default_store with a single match just returns it",
                  store_access.pick_default_store(777, single).id == "1")

            check("pick_default_store with no preference recorded yet falls back to the first configured store",
                  store_access.pick_default_store(777, found).id == "1")

            store_prefs.set_last_store(777, "3")
            check("pick_default_store honors a recorded preference among the accessible stores",
                  store_access.pick_default_store(777, found).id == "3")

            store_prefs.set_last_store(777, "2")
            check("a stale preference pointing at a store this telegram_id no longer has access to falls back to the first",
                  store_access.pick_default_store(777, found).id == "1")
        finally:
            for env_name, prev in (("CRM_STORES_CONFIG", prev_config), ("CRM_STORE_PREFS_PATH", prev_prefs)):
                if prev is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = prev


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


def scenario_multi_store_http() -> None:
    """Фаза A end-to-end regression guard: two different store_id tokens
    hitting the SAME running FastAPI process must read/write two completely
    separate SQLite files — proving webapp.main's middleware (not the fixed
    CRM_DB_PATH the process started with) decides where a request's data
    goes. Uses a real stores.json + two real staff DBs."""
    print("scenario: multi-store request isolation over real HTTP requests")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    with tempfile.TemporaryDirectory() as tmp:
        store_a_db = os.path.join(tmp, "storeA.sqlite3")
        store_b_db = os.path.join(tmp, "storeB.sqlite3")
        config_path = os.path.join(tmp, "stores.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"id": "A", "name": "Магазин A", "db_path": store_a_db},
                    {"id": "B", "name": "Магазин B", "db_path": store_b_db},
                ],
                f,
            )

        init_db(store_a_db)
        init_db(store_b_db)
        with get_conn(store_a_db) as conn:
            staff_a = auth.create_staff(conn, "storeA-owner", "pass", "Владелец A", "owner")
            auth.create_master(conn, "Уникальный Мастер Альфа", None, "fixed", 100)
        with get_conn(store_b_db) as conn:
            staff_b = auth.create_staff(conn, "storeB-owner", "pass", "Владелец B", "owner")

        os.environ["CRM_STORES_CONFIG"] = config_path
        try:
            import webapp.main  # already imported by scenario_webapp_forms; re-import is a cached no-op

            with TestClient(webapp.main.app) as client:
                token_a = make_token(staff_a, "A")
                token_b = make_token(staff_b, "B")

                masters_a = client.get(f"/masters?t={token_a}")
                check("store A's token reaches store A's own db (sees its master)",
                      "Уникальный Мастер Альфа" in masters_a.text)

                masters_b = client.get(f"/masters?t={token_b}")
                check("store B's token never sees store A's data — real db-level isolation",
                      "Уникальный Мастер Альфа" not in masters_b.text)

                dash_b = client.get(f"/?t={token_b}")
                check("store B's own owner can still reach their own dashboard (200, not leaked into store A)",
                      dash_b.status_code == 200)
        finally:
            if prev_config is None:
                os.environ.pop("CRM_STORES_CONFIG", None)
            else:
                os.environ["CRM_STORES_CONFIG"] = prev_config


def scenario_multi_store_login_and_switch_http() -> None:
    """Фаза B end-to-end: a Telegram id with an owner identity in two
    stores must actually land somewhere sensible at login, and switching
    must be rememberd for the next login — the whole point of
    core.store_access/core.store_prefs, exercised through the real
    /miniapp/auto and /store/switch endpoints, not just their building
    blocks in isolation."""
    print("scenario: cross-store login + switcher over real HTTP requests")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    prev_prefs = os.environ.get("CRM_STORE_PREFS_PATH")
    with tempfile.TemporaryDirectory() as tmp:
        store_a_db = os.path.join(tmp, "loginA.sqlite3")
        store_b_db = os.path.join(tmp, "loginB.sqlite3")
        config_path = os.path.join(tmp, "stores.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"id": "A", "name": "Магазин Логин A", "db_path": store_a_db},
                    {"id": "B", "name": "Магазин Логин B", "db_path": store_b_db},
                ],
                f,
            )
        init_db(store_a_db)
        init_db(store_b_db)
        telegram_id = 555000111
        with get_conn(store_a_db) as conn:
            auth.create_staff(conn, "loginowner", "pass", "Мультивладелец", "owner")
            auth.link_staff_telegram(conn, "loginowner", telegram_id)
            # Кабинет-edited name, deliberately different from stores.json's
            # static "Магазин Логин A" label — regression guard for 23.08:
            # the switcher used to show the stale stores.json name because
            # it never looked at store_settings at all.
            store_settings.update_settings(conn, "Кастомное Имя A", None, None, None)
        with get_conn(store_b_db) as conn:
            auth.create_staff(conn, "loginowner", "pass", "Мультивладелец", "owner")
            auth.link_staff_telegram(conn, "loginowner", telegram_id)

        os.environ["CRM_STORES_CONFIG"] = config_path
        os.environ["CRM_STORE_PREFS_PATH"] = os.path.join(tmp, "login-prefs.sqlite3")
        try:
            import webapp.main
            from webapp.routers import miniapp as miniapp_router

            init_data = _build_init_data(miniapp_router.BOT_TOKEN, {"id": telegram_id, "first_name": "Тест"}, int(time.time()))

            with TestClient(webapp.main.app) as client:
                # follow_redirects=False: TestClient follows 303s by default,
                # but the store_id we need to inspect only lives in the
                # Location header of the redirect itself, not the page it
                # points to.
                first_login = client.post("/miniapp/auto", data={"initData": init_data}, follow_redirects=False)
                token_after_first_login = first_login.headers["location"].split("t=", 1)[1]
                check("first-ever login (no preference yet) lands in the first configured store",
                      first_login.status_code == 303 and read_token(token_after_first_login)["store_id"] == "A")
                dash = client.get(f"/?t={token_after_first_login}")
                check("that token actually authenticates", dash.status_code == 200)

                switch_page = client.get(f"/store/switch?t={token_after_first_login}")
                check("the switcher shows the Кабинет-edited name (store_settings), not stores.json's static label",
                      "Кастомное Имя A" in switch_page.text and "Магазин Логин A" not in switch_page.text)
                check("a store that was never renamed still shows its seeded default name",
                      "Магазин Логин B" not in switch_page.text)

                switch = client.post("/store/switch?t=" + token_after_first_login, data={"store_id": "B"}, follow_redirects=False)
                check("switching to store B redirects (303)", switch.status_code == 303)
                token_after_switch = switch.headers["location"].split("t=", 1)[1]
                check("the new token really is for store B, not a no-op",
                      token_after_switch != token_after_first_login)

                second_login = client.post("/miniapp/auto", data={"initData": init_data}, follow_redirects=False)
                token_after_second_login = second_login.headers["location"].split("t=", 1)[1]
                dash2 = client.get(f"/?t={token_after_second_login}")
                check("a second login (after switching) authenticates too", dash2.status_code == 200)
                check("and it landed back on store B (the switch was remembered), not store A again",
                      read_token(token_after_second_login)["store_id"] == "B")

                bogus_switch = client.post("/store/switch?t=" + token_after_second_login, data={"store_id": "does-not-exist"})
                check("switching to a store this telegram_id has no identity in doesn't crash, shows a friendly error",
                      bogus_switch.status_code == 200 and "нет доступа" in bogus_switch.text)
        finally:
            for env_name, prev in (("CRM_STORES_CONFIG", prev_config), ("CRM_STORE_PREFS_PATH", prev_prefs)):
                if prev is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = prev


def scenario_store_settings_http() -> None:
    """Фаза B: Кабинет магазина over real HTTP — role gate + save round-trip."""
    print("scenario: Кабинет магазина over HTTP (role gate + save)")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "cabinet.sqlite3")
        init_db(db_path)
        with get_conn(db_path) as conn:
            owner_id = auth.create_staff(conn, "cabowner", "pass", "Кабинет Владелец", "owner")
            master_id = auth.create_staff(conn, "cabmaster", "pass", "Кабинет Мастер", "master")
        owner_token = make_token(owner_id, "cab")
        master_token = make_token(master_id, "cab")

        config_path = os.path.join(tmp, "stores.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump([{"id": "cab", "name": "Кабинет-тест", "db_path": db_path}], f)
        os.environ["CRM_STORES_CONFIG"] = config_path
        try:
            import webapp.main

            with TestClient(webapp.main.app) as client:
                master_get = client.get(f"/store/settings?t={master_token}")
                check("a master is denied the Кабинет магазина (403), not shown store settings",
                      master_get.status_code == 403)

                owner_get = client.get(f"/store/settings?t={owner_token}")
                check("an owner sees the default seeded name on first visit", "Магазин" in owner_get.text)

                empty_name = client.post(f"/store/settings?t={owner_token}", data={
                    "name": "   ", "address": "", "phone": "", "working_hours": "",
                })
                check("an empty name is rejected with a friendly error, no raw 422",
                      empty_name.status_code == 200 and "не может быть пустым" in empty_name.text)
                with get_conn(db_path) as conn:
                    check("the rejected empty-name save left the store's real name untouched",
                          store_settings.get_settings(conn)["name"] == "Магазин")

                saved = client.post(f"/store/settings?t={owner_token}", data={
                    "name": "Ремонтная Мастерская №1", "address": "просп. Мира, 10",
                    "phone": "+380671234567", "working_hours": "пн–сб 9:00–20:00",
                })
                check("a valid save succeeds (200, re-rendered with a success flash)", saved.status_code == 200)
                check("the success flash is shown", "Изменения сохранены" in saved.text)

                reload = client.get(f"/store/settings?t={owner_token}")
                check("the saved name is really persisted, visible on a fresh GET", "Ремонтная Мастерская №1" in reload.text)
                check("the saved address is really persisted too", "просп. Мира, 10" in reload.text)
        finally:
            if prev_config is None:
                os.environ.pop("CRM_STORES_CONFIG", None)
            else:
                os.environ["CRM_STORES_CONFIG"] = prev_config


def scenario_all_stores_report_http() -> None:
    """Фаза D: «Все магазины» summary over real HTTP — two stores seeded
    with deliberately DIFFERENT numbers on every metric (no shared values
    between stores), so a bug that reads the wrong DB or sums wrong shows
    up as a wrong number rather than accidentally passing."""
    print("scenario: сводный отчёт «Все магазины» over HTTP")
    prev_config = os.environ.get("CRM_STORES_CONFIG")
    with tempfile.TemporaryDirectory() as tmp:
        db_a = os.path.join(tmp, "allA.sqlite3")
        db_b = os.path.join(tmp, "allB.sqlite3")
        init_db(db_a)
        init_db(db_b)
        telegram_id = 700700700

        # Store A: 1 open repair, 1 issued @1000, 1 sale (qty2*100=200),
        # cash 500, 1 low-stock product.
        with get_conn(db_a) as conn:
            owner_a = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "owner", telegram_id)
            store_settings.update_settings(conn, "Магазин Alpha", None, None, None)
            client_id = clients.get_or_create_by_phone(conn, "Клиент A", "+380501110001", source="offline")
            open_order = repairs.create_repair(
                conn, client_id, "Смартфон", "Xiaomi", "Redmi", None, "не грузится", "offline", None, None, owner_a,
            )
            repairs.update_status(conn, open_order, "in_progress", owner_a)
            issued_order = repairs.create_repair(
                conn, client_id, "Ноутбук", "Dell", "XPS", None, "разбит экран", "offline", None, 1000, owner_a,
            )
            repairs.set_price(conn, issued_order, price_estimate=1000, price_final=1000)
            repairs.update_status(conn, issued_order, "issued", owner_a)
            product_a = inventory.create_product(conn, "Товар A", "SKU-ALL-A", None, "шт", False, True, 3, 100)
            conn.execute(
                "INSERT INTO sales_orders (client_id, channel, status, staff_id) VALUES (?, 'offline', 'completed', ?)",
                (client_id, owner_a),
            )
            order_a_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO sales_order_items (order_id, product_id, qty, price) VALUES (?, ?, 2, 100)",
                (order_a_id, product_a),
            )
            cash.record_income(conn, "cash", 500, "manual", 0, owner_a)

        # Store B: 2 open repairs, 1 issued @3000, 2 sales (150+300=450),
        # cash 1200, 2 low-stock products — every number deliberately
        # different from store A's.
        with get_conn(db_b) as conn:
            owner_b = auth.create_staff(conn, "owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "owner", telegram_id)
            store_settings.update_settings(conn, "Магазин Bravo", None, None, None)
            master_b = auth.create_staff(conn, "master", "pass", "Мастер Б", "master")
            client_id = clients.get_or_create_by_phone(conn, "Клиент B", "+380501110002", source="offline")
            for _ in range(2):
                oid = repairs.create_repair(
                    conn, client_id, "Смартфон", "Samsung", "A54", None, "не заряжается", "offline", None, None, owner_b,
                )
                repairs.update_status(conn, oid, "in_progress", owner_b)
            issued_order_b = repairs.create_repair(
                conn, client_id, "Планшет", "Apple", "iPad", None, "треснул экран", "offline", None, 3000, owner_b,
            )
            repairs.set_price(conn, issued_order_b, price_estimate=3000, price_final=3000)
            repairs.update_status(conn, issued_order_b, "issued", owner_b)
            product_b1 = inventory.create_product(conn, "Товар B1", "SKU-ALL-B1", None, "шт", False, True, 3, 50)
            product_b2 = inventory.create_product(conn, "Товар B2", "SKU-ALL-B2", None, "шт", False, True, 3, 300)
            conn.execute(
                "INSERT INTO sales_orders (client_id, channel, status, staff_id) VALUES (?, 'offline', 'completed', ?)",
                (client_id, owner_b),
            )
            order_b1_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO sales_order_items (order_id, product_id, qty, price) VALUES (?, ?, 3, 50)",
                (order_b1_id, product_b1),
            )
            conn.execute(
                "INSERT INTO sales_orders (client_id, channel, status, staff_id) VALUES (?, 'offline', 'completed', ?)",
                (client_id, owner_b),
            )
            order_b2_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            conn.execute(
                "INSERT INTO sales_order_items (order_id, product_id, qty, price) VALUES (?, ?, 1, 300)",
                (order_b2_id, product_b2),
            )
            cash.record_income(conn, "cash", 1200, "manual", 0, owner_b)

        config_path = os.path.join(tmp, "stores.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(
                [
                    {"id": "allA", "name": "Магазин Alpha", "db_path": db_a},
                    {"id": "allB", "name": "Магазин Bravo", "db_path": db_b},
                ],
                f,
            )
        os.environ["CRM_STORES_CONFIG"] = config_path
        try:
            import webapp.main

            with TestClient(webapp.main.app) as client:
                owner_token = make_token(owner_a, "allA")
                master_token = make_token(master_b, "allB")

                master_resp = client.get(f"/reports/all-stores?t={master_token}")
                check("a master role is denied «Все магазины» (403), not shown cross-store financials",
                      master_resp.status_code == 403)

                resp = client.get(f"/reports/all-stores?t={owner_token}")
                text = resp.text
                check("the page loads for an owner with identity in both stores", resp.status_code == 200)
                check("both store names appear", "Магазин Alpha" in text and "Магазин Bravo" in text)

                # Precise check, not just substring presence: extract every
                # <div class="stat-num"> value in document order and compare
                # against the exact expected sequence — store A's 6 stats,
                # then store B's 6, then the totals' 6 (row order in the
                # template: open_repairs, sales_orders, low_stock_count,
                # repairs_revenue, sales_revenue, cash_balance).
                stat_nums = re.findall(r'<div class="stat-num">([^<]*)</div>', text)
                expected = [
                    "1", "1", "1", "1000", "200", "500",       # store A
                    "2", "2", "2", "3000", "450", "1200",      # store B
                    "3", "3", "3", "4000", "650", "1700",      # итого — exact sum of both
                ]
                check("all 18 stat values (2 stores + totals, 6 each) appear in the exact expected order",
                      stat_nums == expected)
        finally:
            if prev_config is None:
                os.environ.pop("CRM_STORES_CONFIG", None)
            else:
                os.environ["CRM_STORES_CONFIG"] = prev_config


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
        scenario_repair_attachments(db_path)
        scenario_store_settings(db_path)

    scenario_timefmt()
    scenario_telegram_auth()
    scenario_session_token()
    scenario_stores_config()
    scenario_storage_context()
    scenario_store_prefs()
    scenario_store_access()
    scenario_miniapp_boot_template()
    scenario_webapp_forms(_WEBAPP_TEST_DB)
    scenario_multi_store_http()
    scenario_multi_store_login_and_switch_http()
    scenario_store_settings_http()
    scenario_all_stores_report_http()
    if os.path.exists(_WEBAPP_TEST_DB):
        os.remove(_WEBAPP_TEST_DB)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
