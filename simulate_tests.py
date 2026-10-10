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

# Photos written during the run go to a throwaway folder, never into
# webapp/static: this file is also run inside the LIVE folder after every
# deploy, and until 07.10 each run left its fake покупка photos next to the
# real ones (and `git add -A` then committed them).
import atexit
import shutil

_TEST_PHOTO_ROOT = tempfile.mkdtemp(prefix="crm-test-photos-")
atexit.register(shutil.rmtree, _TEST_PHOTO_ROOT, True)
repairs.PHOTO_DIR = os.path.join(_TEST_PHOTO_ROOT, "device_photos")
buyback.PHOTO_DIR = os.path.join(_TEST_PHOTO_ROOT, "buyback_photos")

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
    check("repair card is headed with its document number (РК-NNN)", f"РК-{order_id:03d} •" in text)
    check("repair card header uses the status label", "Новый" in text)

    kb_new = repairs.render_keyboard(order_id, "new")
    check("keyboard for 'new' offers to take the job",
          kb_new["inline_keyboard"][0][0]["callback_data"] == f"repair_take:{order_id}")
    check("keyboard for 'new' also offers Открыть в CRM",
          kb_new["inline_keyboard"][-1][0]["callback_data"] == f"open_crm:repair:{order_id}")
    kb_in_progress = repairs.render_keyboard(order_id, "in_progress")
    check("keyboard for 'in_progress' offers «указать запчасть» + «готово», and «не удалось починить» below",
          {b["callback_data"] for b in kb_in_progress["inline_keyboard"][0]}
          == {f"repair_part:{order_id}", f"repair_done:{order_id}"}
          and kb_in_progress["inline_keyboard"][1][0]["callback_data"] == f"repair_release:{order_id}")
    kb_ready = repairs.render_keyboard(order_id, "ready")
    check("keyboard for 'ready' still lets the part be named, plus Открыть в CRM — no status buttons",
          [row[0]["callback_data"] for row in kb_ready["inline_keyboard"]]
          == [f"repair_part:{order_id}", f"open_crm:repair:{order_id}"])
    kb_issued = repairs.render_keyboard(order_id, "issued")
    check("keyboard for 'issued' has nothing left to press but Открыть в CRM",
          kb_issued["inline_keyboard"] == [[{"text": "🔗 Открыть в CRM", "callback_data": f"open_crm:repair:{order_id}"}]])

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
    # …nor a clients table for the «returning client» lookup on the phone step.
    original_get_by_phone = clients.get_by_phone
    clients.get_by_phone = lambda conn, phone: None
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
        clients.get_by_phone = original_get_by_phone
        qa.get_store = original_get_store
        auth.get_staff_by_telegram_id = original_get_staff

    # Покупка телефона: шесть нумерованных шагов; курс, оплата и карточка
    # подтверждения идут без шапки «шаг N из M».
    buy_step = qa._flow_screen("PhoneBuy:imei", {"seller_phone": "+380501112233", "photo_count": 6, "model": "iPhone 13"}, "IMEI:")
    check("the purchase flow numbers its six data steps and recaps what is already entered",
          "шаг 4 из 6" in buy_step and "Продавец: +380501112233" in buy_step and "Фото: 6" in buy_step and "Модель: iPhone 13" in buy_step)
    check("its payment screens carry no step header",
          qa._flow_screen("PhoneBuy:pay_review", {}, "Распределите оплату") == "Распределите оплату")
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

        shell = client.get(f"/?t={token}").text
        head = shell.split("</head>")[0]
        check("every page asks Telegram for the full screen before it paints (the script is in <head>, after Telegram's own)",
              "/static/telegram-fullscreen.js" in head and head.index("telegram-web-app.js") < head.index("telegram-fullscreen.js")
              and "/static/telegram-fullscreen.js" in client.get("/miniapp").text.split("</head>")[0])
        script = client.get("/static/telegram-fullscreen.js").text
        check("it expands everywhere, goes fullscreen on phones only, keeps a swipe from closing the app, and reports the room Telegram's controls take",
              all(x in script for x in ("tg.expand()", "requestFullscreen", 'tg.platform === "ios" || tg.platform === "android"',
                                        "disableVerticalSwipes", "--tg-top", "contentSafeAreaInset")) and "if (!tg || !tg.initData) return;" in script)
        css = client.get("/static/style.css").text
        check("the header and the tab bar leave that room; outside Telegram it is zero", "var(--tg-top, 0px)" in css and "var(--tg-bottom, 0px)" in css)
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
        intake_photo_file = os.path.join(repairs.PHOTO_DIR, intake_photo_path or "")
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

        saved_device_photo_file = os.path.join(repairs.PHOTO_DIR, device_photo_path or "")
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
    """Покупка телефона (Заход 4): core.buyback.create_purchase, the Mini
    App form and card page, cancel — «один телефон, одна покупка»."""
    print("scenario: покупка телефона — core, форма, карточка, этикетка, отмена")
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "purchase.sqlite3", ("Мастерская", "Магазин")) as base:
        with get_conn(base) as conn:
            owner = auth.create_staff(conn, "bb-owner", "pass", "Виталий", "owner")
            master = auth.create_staff(conn, "bb-master", "pass", "Мастер", "master")
            acc = {(a["location_id"], a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn)}
            cash_uah, fop, card, usdt = (acc[(1, k, c)] for k, c in (("cash", "UAH"), ("fop", "UAH"), ("card", "UAH"), ("crypto", "USDT")))
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            known = clients.get_or_create_by_phone(conn, "Андрей", "+380970073090", source="offline")

            # ---- core: макет «Распределить оплату» — нал 2000 + ФОП 1500 + карта 1000 + 12.5 USDT × 40
            photo = io.BytesIO()
            Image.new("RGB", (60, 60), "white").save(photo, format="JPEG")
            six = [(photo.getvalue(), ".jpg", label) for label in buyback.PHOTO_SLOTS]
            order_id = buyback.create_purchase(
                conn, seller_phone="097 007 30 90", model="iPhone 13 ·  128 GB · Black", imei="356 000 000 000 001",
                comment="Разбит дисплей", price="5000", staff_id=owner, location_id=1, photos=six, key="bb-1",
                payments=[(cash_uah, "2000", None), (fop, "1500", None), (card, "1000", None), (usdt, "12,5", "40")],
            )
            order = buyback.get_buyback_order(conn, order_id)
            check("the seller is found by phone number — the existing контрагент, not a new card",
                  order["client_id"] == known and order["client_name"] == "Андрей")
            check("the order keeps model (spaces tidied), IMEI (normalized), поломки, price and its гривня value",
                  order["model"] == "iPhone 13 · 128 GB · Black" and order["imei"] == "356000000000001"
                  and order["condition_note"] == "Разбит дисплей" and order["purchase_price"] == 5000 and order["purchase_price_uah"] == 5000)
            unit = inventory.find_unit_by_imei(conn, "356000000000001")
            product = inventory.get_product(conn, order["product_id"])
            check("the phone is a serial unit on the buying точка's склад: its IMEI, the price as its cost",
                  unit is not None and unit["id"] == order["batch_id"] and unit["unit_cost_uah"] == 5000 and unit["source"] == "buyback"
                  and product["is_serial"] == 1 and product["name"] == "iPhone 13 · 128 GB · Black"
                  and inventory.product_total_qty(conn, product["id"], 1) == 1 and inventory.stock_value(conn, 1) == 5000)
            check("the payout left each source: 18 000 cash, −1 500 ФОП, −1 000 карта, −12.5 USDT",
                  accounts.balance(conn, cash_uah) == 18000 and accounts.balance(conn, fop) == -1500
                  and accounts.balance(conn, card) == -1000 and accounts.balance(conn, usdt) == -12.5)
            check("all six photos are stored with their sides, the first is the thumbnail",
                  [p["label"] for p in buyback.get_photos(conn, order_id)] == list(buyback.PHOTO_SLOTS)
                  and order["photo_path"] == buyback.get_photos(conn, order_id)[0]["path"] and len(buyback.read_photos(conn, order_id)) == 6)
            doc = documents.get_for(conn, "buyback", order_id)
            check("one document (ПК) for the whole thing — amount, контрагент, model",
                  documents.doc_label(doc) == f"ПК-{order_id:03d}" and doc["amount"] == 5000 and doc["client_id"] == known
                  and doc["title"] == "iPhone 13 · 128 GB · Black"
                  and conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type IN ('cash_out','stock_in')").fetchone()["n"] == 0)
            text = buyback.card_text(conn, order_id)
            check("the card reads like the макет: number · model, контрагент, IMEI, поломки, «Куплен … · точка», оплата",
                  f"ПК-{order_id:03d} · iPhone 13 · 128 GB · Black" in text and "+380970073090" in text and "356000000000001" in text
                  and "Поломки: Разбит дисплей" in text and "Куплен: 5000 грн · Мастерская" in text and "Криптокошелёк USDT — 12.5 USDT" in text)

            second = buyback.create_purchase(
                conn, seller_phone="0501230000", seller_name="Новый Продавец", model="iphone 13 · 128 gb · black",
                imei="356000000000002", comment=None, price="120", currency="USD", rate="41,5", staff_id=owner, location_id=1,
            )
            order_2 = buyback.get_buyback_order(conn, second)
            check("a second phone of the same model is another unit of the SAME product card (name matched case-insensitively)",
                  order_2["product_id"] == order["product_id"] and inventory.product_total_qty(conn, product["id"], 1) == 2)
            check("a price in USD keeps the currency and rate; cost and payout are its гривня value (120 × 41.5 = 4980, from наличные)",
                  order_2["currency"] == "USD" and order_2["purchase_price_uah"] == 4980
                  and inventory.find_unit_by_imei(conn, "356000000000002")["unit_cost_uah"] == 4980
                  and accounts.balance(conn, cash_uah) == 13020)
            check("a new seller becomes a контрагент with the given name", order_2["client_name"] == "Новый Продавец")
            check("the model step can now suggest what was bought before", buyback.recent_models(conn) == ["iPhone 13 · 128 GB · Black"])

            before = len(buyback.list_buyback_orders(conn))
            for kwargs, needle, label in (
                ({"seller_phone": "не помню"}, "номер телефона продавца", "a покупка without the seller's phone number is refused"),
                ({"imei": "356000000000001"}, "уже числится", "an IMEI already in stock is refused"),
                ({"imei": "12"}, "Проверьте IMEI", "a too-short IMEI is refused"),
                ({"model": "  "}, "модель", "a missing model is refused"),
                ({"price": "0"}, "цену", "a zero price is refused"),
                ({"currency": "USD"}, "Укажите курс", "a foreign-currency price without a rate is refused"),
            ):
                args = {"seller_phone": "0501239999", "model": "Samsung A54", "imei": "356000000000099", "comment": None, "price": "3000"}
                args.update(kwargs)
                try:
                    buyback.create_purchase(conn, staff_id=owner, location_id=1, **args)
                    check(label, False)
                except buyback.PurchaseError as exc:
                    check(label, needle in str(exc))
            try:
                buyback.create_purchase(conn, seller_phone="0501239999", model="Samsung A54", imei="356000000000099", comment=None,
                                        price="3000", staff_id=owner, location_id=1, payments=[(cash_uah, "1000", None)])
                check("an underpaid split is refused", False)
            except cash.PaymentError as exc:
                check("an underpaid split is refused", "не хватает 2000" in str(exc))
            check("none of the refused покупки wrote anything (no order, no контрагент, no product)",
                  len(buyback.list_buyback_orders(conn)) == before and clients.get_by_phone(conn, "0501239999") is None
                  and not [p for p in inventory.list_products(conn) if p["name"] == "Samsung A54"])

            doc_cancel.cancel_document(conn, doc["id"], owner, "клиент передумал")
            check("cancelling a покупка takes the phone off stock and returns the money to every source",
                  inventory.find_unit_by_imei(conn, "356000000000001") is None and accounts.balance(conn, cash_uah) == 15020
                  and accounts.balance(conn, fop) == 0 and accounts.balance(conn, usdt) == 0)
            sales.create_sale(conn, None, "offline", owner, [(product["id"], 1, 9000, order_2["batch_id"])], location_id=1)
            try:
                doc_cancel.cancel_document(conn, documents.get_for(conn, "buyback", second)["id"], owner, "поздно")
                check("a покупка whose phone is already sold can't be cancelled", False)
            except documents.DocumentError:
                check("a покупка whose phone is already sold can't be cancelled", True)

        # ---- форма и карточка
        token, master_token = make_token(owner, "1"), make_token(master, "1")
        import webapp.main

        def _jpeg():
            buf = io.BytesIO()
            Image.new("RGB", (80, 80), "white").save(buf, format="JPEG")
            return ("phone.jpg", buf.getvalue(), "image/jpeg")

        with TestClient(webapp.main.app) as client:
            page = client.get(f"/buyback?t={token}").text
            check("the form has the макет's fields: seller phone, model with suggestions, IMEI, поломки, six photo slots, price + currency, sources",
                  all(x in page for x in ('name="seller_phone"', 'name="model"', 'name="imei"', 'name="comment"', 'name="photo_5"',
                                          'name="currency"', f'name="pay_{cash_uah}"', "iPhone 13 · 128 GB · Black", "Передняя", "Левая")))
            form = {"seller_phone": "0671112233", "model": "Samsung A25", "imei": "357000000000011", "comment": "Не работает зарядка",
                    "price": "4000", "currency": "UAH", f"pay_{cash_uah}": "2500", f"pay_{fop}": "1500"}
            check("a master can't buy (403)", client.post(f"/buyback?t={master_token}", data=form, files={"photo_0": _jpeg()}).status_code == 403)
            no_photo = client.post(f"/buyback?t={token}", data=form)
            check("no photo at all is a friendly error", no_photo.status_code == 200 and "хотя бы одно фото" in no_photo.text)
            bad_type = client.post(f"/buyback?t={token}", data=form, files={"photo_0": ("x.txt", b"hello", "text/plain")})
            check("a non-image upload is a friendly error", "JPEG, PNG или WebP" in bad_type.text)
            dup = client.post(f"/buyback?t={token}", data=dict(form, imei="356000000000002"), files={"photo_0": _jpeg()})
            check("an IMEI that isn't new... is fine if that phone is no longer in stock (it was sold) — the same phone can come back",
                  dup.status_code == 200 and "/buyback/" in str(dup.url))
            short = client.post(f"/buyback?t={token}", data=dict(form, **{f"pay_{fop}": "1000"}), files={"photo_0": _jpeg()})
            check("sources that don't add up to the price are a friendly error", "не сходится" in short.text)
            no_phone = client.post(f"/buyback?t={token}", data=dict(form, seller_phone="+380"), files={"photo_0": _jpeg()})
            check("the untouched «+380» template is not a phone number", "номер телефона продавца" in no_phone.text)
            done = client.post(f"/buyback?t={token}", data=form, files={"photo_0": _jpeg(), "photo_1": _jpeg(), "photo_3": _jpeg()},
                               follow_redirects=False)
            check("a valid покупка goes through and opens its card", done.status_code == 303 and "/buyback/" in done.headers["location"])
            order_id = int(done.headers["location"].split("/buyback/")[1].split("?")[0])
            with get_conn(base) as conn:
                check("three photos given — three stored, each under the side it was uploaded as",
                      [p["label"] for p in buyback.get_photos(conn, order_id)] == ["Передняя", "Задняя", "Низ"])
                check("paid 2500 from наличные and 1500 from ФОП (on top of the re-bought phone's 1500 above)",
                      accounts.balance(conn, fop) == -3000)
            card = client.get(f"/buyback/{order_id}?t={token}").text
            check("the card shows number · model, контрагент, IMEI, поломки, price, where the phone is, how it was paid",
                  all(x in card for x in (f"ПК-{order_id:03d}", "Samsung A25", "+380671112233", "357000000000011", "Не работает зарядка",
                                          "4000", "СКУПКА", "Счёт ФОП")))
            check("…with the IMEI barcode label and a print button", f"/buyback/{order_id}/barcode.png" in card and "printBuybackLabelBtn" in card)
            label = client.get(f"/buyback/{order_id}/barcode.png?t={token}")
            compact = client.get(f"/buyback/{order_id}/barcode.png?compact=1&t={token}")
            check("the label endpoint returns a real PNG in both sizes",
                  label.status_code == 200 and label.content[:8] == b"\x89PNG\r\n\x1a\n" and compact.content[:8] == b"\x89PNG\r\n\x1a\n")
            found = client.get(f"/warehouse/find?t={token}&code=357000000000011", follow_redirects=False)
            check("scanning that label on «Склад» finds the phone", found.status_code == 303 and "/inventory/products/" in found.headers["location"])
            check("the list shows the покупка as a card with its model and price",
                  "Samsung A25" in client.get(f"/buyback?t={token}").text and "4000" in client.get(f"/buyback?t={token}").text)
            check("точка 2's list doesn't show точка 1's покупки", "Samsung A25" not in client.get(f"/buyback?t={make_token(owner, '2')}").text.split('<div class="cards">')[1])
            check("an unknown id goes back to the list", client.get(f"/buyback/999999?t={token}", follow_redirects=False).status_code == 303)

            # ---- дубль карточки в тему «Скупка» рабочей группы
            from core import notify
            sent_calls = []

            class _Resp:
                status_code, text = 200, "ok"

                def json(self):
                    return {"result": [{"message_id": 4242}]}

            def _fake_post(url, **kwargs):
                sent_calls.append({"method": url.rsplit("/", 1)[1], "data": kwargs.get("data") or kwargs.get("json"), "files": kwargs.get("files")})
                return _Resp()

            orig_post, orig_token = httpx.post, notify._BOT_TOKEN
            httpx.post, notify._BOT_TOKEN = _fake_post, "test-token"
            try:
                client.post(f"/buyback?t={token}", data=dict(form, imei="357000000000012"), files={"photo_0": _jpeg()})
                check("with no «Скупка» topic configured nothing is posted to the group", not sent_calls)
                client.post(f"/store/settings?t={token}", data={"name": "Мастерская", "buyback_topic_id": "77"})
                with get_conn(base) as conn:
                    conn.execute("UPDATE locations SET staff_group_chat_id = -100123 WHERE id = 1")
                    check("Кабинет магазина saves the topic number", store_settings.get_settings(conn, 1)["buyback_topic_id"] == 77)
                client.post(f"/buyback?t={token}", data=dict(form, imei="357000000000013"), files={"photo_0": _jpeg(), "photo_1": _jpeg()})
                check("with the topic set, the покупка's card goes there as ONE album with the card as caption",
                      [c["method"] for c in sent_calls] == ["sendMediaGroup"] and str(sent_calls[0]["data"]["message_thread_id"]) == "77"
                      and sent_calls[0]["data"]["chat_id"] == -100123 and len(sent_calls[0]["files"]) == 2
                      and "357000000000013" in sent_calls[0]["data"]["media"] and "Samsung A25" in json.loads(sent_calls[0]["data"]["media"])[0]["caption"])
                client.post(f"/store/settings?t={token}", data={"name": "Мастерская", "buyback_topic_id": "нет"})
                with get_conn(base) as conn:
                    check("a non-number switches the posting off again", store_settings.get_settings(conn, 1)["buyback_topic_id"] is None)
            finally:
                httpx.post, notify._BOT_TOKEN = orig_post, orig_token
            with get_conn(base) as conn:
                doc_id = documents.get_for(conn, "buyback", order_id)["id"]
            client.post(f"/journal/{doc_id}/cancel?t={token}", data={"reason": "ошибка"})
            check("after cancelling from the journal the card says the phone is no longer on stock",
                  "не числится" in client.get(f"/buyback/{order_id}?t={token}").text)


def scenario_purchase_chat() -> None:
    """«🛒 Покупка» in the bot — the twelve steps of the макет driven
    through the real handlers against a real base."""
    print("scenario: покупка телефона в боте — 12 шагов, сплит-оплата, карточка")
    import asyncio
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import buyback_flow as bf
    from bot import quick_actions as qa

    check("the price step understands гривня by default and named currencies",
          bf.parse_price("5 000") == (5000, "UAH") and bf.parse_price("120 usd") == (120, "USD")
          and bf.parse_price("$99,5") == (99.5, "USD") and bf.parse_price("250 USDT") == (250, "USDT")
          and bf.parse_price("100 евро") == (100, "EUR") and bf.parse_price("дорого") is None)

    TG, CHAT_ID = 882001, 882001
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "purchase-chat.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            keeper = auth.create_staff(conn, "pc-keeper", "pass", "Виталий", "storekeeper", location_id=1)
            auth.link_staff_telegram(conn, "pc-keeper", TG)
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
            cash.record_adjustment(conn, 20000, "старт", keeper, location_id=1)

        photo_bytes = io.BytesIO()
        Image.new("RGB", (40, 40), "white").save(photo_bytes, format="JPEG")
        albums: list[list] = []

        class _Chat:
            def __init__(self):
                self.log, self._next = [], 500

            def add(self, author, text, markup=None, kind="text"):
                self._next += 1
                self.log.append({"id": self._next, "author": author, "text": text, "markup": markup, "kind": kind})
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

            async def get_file(self, file_id):
                return SimpleNamespace(file_path="x")

            async def download_file(self, file_path):
                return io.BytesIO(photo_bytes.getvalue())

        class _Msg:
            def __init__(self, chat, bot, text=None, photo=False):
                self._log, self.bot, self.text = chat, bot, text
                self.photo = [SimpleNamespace(file_id="f")] if photo else None
                self.chat = SimpleNamespace(id=CHAT_ID, type="private")
                self.from_user = SimpleNamespace(id=TG, full_name="Виталий")
                self.message_id = chat.add("staff", text or "[фото]")

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

        class _CbMessage:
            def __init__(self, chat, message_id):
                self._log, self.message_id = chat, message_id
                self.chat = SimpleNamespace(id=CHAT_ID, type="private")

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

            async def answer_photo(self, photo, caption=None, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", caption, reply_markup, kind="photo"))

            async def answer_media_group(self, media):
                albums.append(media)
                self._log.add("bot", media[0].caption, None, kind="album")

        class _Cb:
            def __init__(self, chat, bot, data, message_id):
                self.data, self.bot = data, bot
                self.from_user = SimpleNamespace(id=TG)
                self.message = _CbMessage(chat, message_id)
                self.answered = []

            async def answer(self, text=None, show_alert=False):
                self.answered.append((text, show_alert))

        def _buttons(markup) -> list[str]:
            return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] if markup else []

        async def run() -> None:
            chat = _Chat()
            bot = _Bot(chat)
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT_ID, user_id=TG))
            say = lambda text: _Msg(chat, bot, text)
            tap = lambda data: _Cb(chat, bot, data, chat.log[-1]["id"])
            screen = lambda: [m for m in chat.log if m["author"] == "bot"][-1]

            await bf.buy_start(say(qa.BTN_BUYBACK), state)
            check("with no shift open «Покупка» shows «сверить остатки» first", "Сверьте остатки" in screen()["text"])
            await qa.shift_open_ok(tap("shift_open_ok"), state)
            await bf.buy_start(say(qa.BTN_BUYBACK), state)
            check("step 1 asks for the seller's phone number", "номер телефона продавца" in screen()["text"] and "шаг 1 из 6" in screen()["text"])
            await bf.buy_got_phone(say("привет"), state)
            check("something that isn't a number keeps the question up", "Не похоже на номер" in screen()["text"])
            await bf.buy_got_phone(say("097 007 30 90"), state)
            check("step 2 asks for six photos, starting with the front", "6 фотографий" in screen()["text"] and "Сейчас: Передняя" in screen()["text"])
            await bf.buy_photo_fallback(say("вот"), state)
            check("text instead of a photo is nudged", "Пришлите фото" in screen()["text"])
            # five one by one…
            for index in range(5):
                await bf.buy_got_photo(_Msg(chat, bot, photo=True), state)
            check("after five it shows the count and names the last side", "Загружено 5 из 6" in screen()["text"] and "Сейчас: Левая" in screen()["text"])
            await bf.buy_got_photo(_Msg(chat, bot, photo=True), state)
            check("the sixth moves on to the model", "Укажите модель телефона" in screen()["text"] and "шаг 3 из 6" in screen()["text"])
            check("an extra photo arriving late is swallowed without breaking the step",
                  (await bf.buy_got_photo(_Msg(chat, bot, photo=True), state)) is None and "Укажите модель" in screen()["text"])
            await bf.buy_got_model(say("iPhone 13 · 128 GB · Black"), state)
            await bf.buy_got_imei(say("12"), state)
            check("a bad IMEI is refused with a hint", "Проверьте IMEI" in screen()["text"])
            await bf.buy_got_imei(say("356000000000001"), state)
            check("step 5 asks for поломки, with a skip button", "поломки" in screen()["text"] and "buy_comment_skip" in _buttons(screen()["markup"]))
            await bf.buy_got_comment(say("Разбит дисплей. Изношен аккумулятор."), state)
            check("step 6 asks for the price", "цену покупки" in screen()["text"] and "шаг 6 из 6" in screen()["text"])
            await bf.buy_got_price(say("много"), state)
            check("a non-number price is nudged", "Введите цену числом" in screen()["text"])
            await bf.buy_got_price(say("5000"), state)
            check("then the sources: every account of the точка with its balance",
                  "Выберите источник оплаты" in screen()["text"] and f"buy_src:{acc[('cash', 'UAH')]}" in _buttons(screen()["markup"])
                  and f"buy_src:{acc[('crypto', 'USDT')]}" in _buttons(screen()["markup"])
                  and any("20000" in b.text for row in screen()["markup"].inline_keyboard for b in row))

            await bf.buy_pick_source(tap(f"buy_src:{acc[('cash', 'UAH')]}"), state)
            check("picking a source asks how much, offering «всё оставшееся»",
                  "Сколько платим" in screen()["text"] and "buy_amt_all" in _buttons(screen()["markup"]))
            await bf.buy_got_amount(say("9000"), state)
            check("more than is left to pay is refused", "только 5000" in screen()["text"])
            await bf.buy_got_amount(say("2000"), state)
            check("the distribution screen lists the part and what is left, with «Добавить оплату»",
                  "Наличные — 2000 грн" in screen()["text"] and "Осталось: 3000 грн" in screen()["text"]
                  and "buy_pay_add" in _buttons(screen()["markup"]) and "buy_pay_done" not in _buttons(screen()["markup"]))
            await bf.buy_pay_more(tap("buy_pay_add"), state)
            await bf.buy_pick_source(tap(f"buy_src:{acc[('fop', 'UAH')]}"), state)
            await bf.buy_got_amount(say("1500"), state)
            await bf.buy_pay_more(tap("buy_pay_add"), state)
            await bf.buy_pick_source(tap(f"buy_src:{acc[('card', 'UAH')]}"), state)
            await bf.buy_got_amount(say("1000"), state)
            await bf.buy_pay_more(tap("buy_pay_add"), state)
            await bf.buy_pick_source(tap(f"buy_src:{acc[('crypto', 'USDT')]}"), state)
            check("a foreign-currency source asks for the amount in that currency, no «всё оставшееся»",
                  "Сколько USDT" in screen()["text"] and not screen()["markup"])
            await bf.buy_got_amount(say("12,5"), state)
            check("then for its rate", "сколько гривен за 1 USDT" in screen()["text"])
            await bf.buy_got_pay_rate(say("50"), state)
            check("a rate that overshoots what is left is refused", "больше, чем осталось" in screen()["text"])
            await bf.buy_got_pay_rate(say("40"), state)
            text = screen()["text"]
            check("the distribution matches the макет: four sources, total 5000, nothing left, «Далее»",
                  all(x in text for x in ("Наличные — 2000 грн", "Счёт ФОП — 1500 грн", "Карта — 1000 грн", "Криптокошелёк USDT — 12.5 USDT = 500 грн",
                                          "Всего: 5000 грн · Осталось: 0 грн"))
                  and "buy_pay_done" in _buttons(screen()["markup"]))
            check("a source with less money than is taken from it is flagged, not blocked", "⚠ на счёте сейчас 0" in text)

            await bf.buy_to_confirm(tap("buy_pay_done"), state)
            card = screen()
            check("the final card is a PHOTO with model, photo count, IMEI, price, склад, seller and the split",
                  card["kind"] == "photo" and all(x in card["text"] for x in (
                      "iPhone 13 · 128 GB · Black", "Фото: 6", "356000000000001", "Цена: 5000 грн", "Склад: Мастерская", "+380970073090"))
                  and {"buy_confirm", "buy_cancel"} <= set(_buttons(card["markup"])))
            check("up to here the chat holds only the «смена открыта» line and the current screen — nothing the employee sent",
                  [m["author"] for m in chat.log] == ["bot", "bot"] and "Смена открыта" in chat.log[0]["text"])
            await bf.buy_confirm_fallback(say("ок"), state)
            check("a stray message on the card doesn't replace it — just a pointer back", chat.log[-1]["text"].startswith("Нажмите «✅ Купить»"))

            buy_tap = _Cb(chat, bot, "buy_confirm", card["id"])
            await bf.buy_confirm(buy_tap, state)
            with get_conn(db_path) as conn:
                orders = buyback.list_buyback_orders(conn)
                check("«Купить» creates exactly one покупка with everything entered",
                      len(orders) == 1 and orders[0]["imei"] == "356000000000001" and orders[0]["purchase_price"] == 5000
                      and orders[0]["condition_note"].startswith("Разбит дисплей") and len(buyback.get_photos(conn, orders[0]["id"])) == 6)
                check("the money left the four sources and the phone is on stock",
                      accounts.balance(conn, acc[("cash", "UAH")]) == 18000 and accounts.balance(conn, acc[("crypto", "USDT")]) == -12.5
                      and inventory.find_unit_by_imei(conn, "356000000000001") is not None)
            check("the chat ends with the card as a six-photo album and a line with the «карточка и этикетка» button",
                  len(albums) == 1 and len(albums[0]) == 6 and "ПК-001 · iPhone 13" in albums[0][0].caption
                  and [m["kind"] for m in chat.log][-2:] == ["album", "text"] and len(chat.log) == 3
                  and "ПК-001 — куплен" in chat.log[-1]["text"])
            await bf.buy_confirm(_Cb(chat, bot, "buy_confirm", card["id"]), state)
            with get_conn(db_path) as conn:
                check("a second tap on «Купить» buys nothing twice", len(buyback.list_buyback_orders(conn)) == 1)

            # ---- второй заход: валюта цены, выбор модели кнопкой, «без поломок», «всё оставшееся», «Изменить»
            await bf.buy_start(say(qa.BTN_BUYBACK), state)
            await bf.buy_got_phone(say("0970073090"), state)
            for _ in range(6):
                await bf.buy_got_photo(_Msg(chat, bot, photo=True), state)
            check("the model step now offers what was bought before as a button", "buy_model:0" in _buttons(screen()["markup"]))
            await bf.buy_pick_model(tap("buy_model:0"), state)
            await bf.buy_got_imei(say("356000000000001"), state)
            check("the same IMEI can't be bought while that phone is still on stock", "уже числится" in screen()["text"])
            await bf.buy_got_imei(say("356000000000002"), state)
            await bf.buy_skip_comment(tap("buy_comment_skip"), state)
            await bf.buy_got_price(say("100 usd"), state)
            check("a USD price asks for the rate", "сколько гривен за 1 USD" in screen()["text"])
            await bf.buy_got_rate(say("41"), state)
            check("and shows the гривня value to pay", "100 USD = 4100 грн" in screen()["text"] and "Осталось оплатить: 4100 грн" in screen()["text"])
            await bf.buy_pick_source(tap(f"buy_src:{acc[('card', 'UAH')]}"), state)
            await bf.buy_got_amount(say("100"), state)
            await bf.buy_pay_more(tap("buy_pay_reset"), state)
            check("«Изменить» clears the split and starts the sources over", "Осталось оплатить: 4100 грн" in screen()["text"] and "Карта —" not in screen()["text"])
            await bf.buy_pick_source(tap(f"buy_src:{acc[('cash', 'UAH')]}"), state)
            await bf.buy_amount_all(tap("buy_amt_all"), state)
            check("«всё оставшееся» fills the whole amount in one tap", "Наличные — 4100 грн" in screen()["text"] and "Осталось: 0 грн" in screen()["text"])
            await bf.buy_to_confirm(tap("buy_pay_done"), state)
            await bf.buy_confirm(_Cb(chat, bot, "buy_confirm", screen()["id"]), state)
            with get_conn(db_path) as conn:
                orders = buyback.list_buyback_orders(conn)
                check("the USD покупка is on record at its гривня value, the same product card now has two units",
                      len(orders) == 2 and orders[0]["currency"] == "USD" and orders[0]["purchase_price_uah"] == 4100
                      and orders[0]["product_id"] == orders[1]["product_id"] and accounts.balance(conn, acc[("cash", "UAH")]) == 13900)

        asyncio.run(run())


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


def scenario_service_center() -> None:
    """Заход 5 core: резерв, запчасть клиентского ремонта со склада
    мастера, прибыль и начисление, производство по макету (5000 + 2700 +
    500 + 1000 = 9200)."""
    print("scenario: сервисный центр — резерв, запчасть со склада мастера, прибыль, производство")
    from core import production, stock_transfers, warehouses

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "sc.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "sc-owner", "pass", "Владелец", "owner")
            edik = auth.create_master(conn, "Эдик", None, "percent", 33)
            sergey = auth.create_master(conn, "Сергей", None, "fixed", 400, master_kind="outsource", skills="замена дисплея, АКБ")
            check("a master's type and skills are on record",
                  auth.get_master(conn, sergey)["master_kind"] == "outsource" and auth.get_master(conn, sergey)["skills"] == "замена дисплея, АКБ"
                  and auth.get_master(conn, edik)["master_kind"] == "staff")
            sup_a = purchases.create_supplier(conn, "Поставщик А", None)
            sup_b = purchases.create_supplier(conn, "Поставщик Б", None)
            sup_c = purchases.create_supplier(conn, "Поставщик В", None)
            c1 = inventory.create_cell(conn, "SC-1", None, None, location_id=1)
            c2 = inventory.create_cell(conn, "SC-2", None, None, location_id=2)
            display = inventory.create_product(conn, "Дисплей iPhone 13", "SC-DSP", None, "шт", True, True, 0, 3500)
            akb = inventory.create_product(conn, "АКБ iPhone 13", "SC-AKB", None, "шт", True, True, 0, 1200)
            cable = inventory.create_product(conn, "Кабель", "SC-CBL", None, "шт", False, True, 0, 300)
            r_a = purchases.create_receipt(conn, sup_a, "125", owner, [(display, c1, 4, 2000)], location_id=1)
            purchases.create_receipt(conn, sup_b, "138", owner, [(display, c2, 1, 2200)], location_id=2)
            purchases.create_receipt(conn, sup_c, "142", owner, [(akb, c1, 3, 700), (cable, c1, 5, 100)], location_id=1)
            point_1 = warehouses.point_warehouse(conn, 1)["id"]
            edik_wh = warehouses.master_warehouse(conn, edik)["id"]
            sergey_wh = warehouses.master_warehouse(conn, sergey)["id"]
            batch_a = purchases.get_receipt_batches(conn, r_a)[0]["id"]

            # ---- резерв: доступно = остаток − резерв
            hold = inventory.reserve(conn, batch_a, c1, 3, "test", 1)
            check("a reservation doesn't change what is physically there, only what is free",
                  inventory.product_total_qty(conn, display, 1) == 4 and inventory.available_qty(conn, batch_a, c1) == 1
                  and inventory.reserved_qty(conn, batch_a, c1) == 3)
            for attempt, label in (
                (lambda: inventory.reserve(conn, batch_a, c1, 2, "test", 2), "reserving more than is free is refused"),
                (lambda: inventory.record_movement(conn, display, 2, "adjustment", owner, from_cell_id=c1), "nothing else can take reserved stock (oldest-first)"),
                (lambda: inventory.record_movement(conn, display, 2, "adjustment", owner, from_cell_id=c1, batch_id=batch_a), "…nor by naming the партия"),
                (lambda: sales.create_sale(conn, None, "offline", owner, [(display, 2, 3500)], location_id=1), "a sale can't sell what is reserved"),
            ):
                try:
                    attempt()
                    check(label, False)
                except inventory.InsufficientStockError:
                    check(label, True)
            check("none of the refusals moved anything", inventory.product_total_qty(conn, display, 1) == 4 and _stock_is_consistent(conn))
            inventory.record_movement(conn, display, 1, "adjustment", owner, from_cell_id=c1, reserved_for=("test", 1), comment="из своего резерва")
            check("the document a reservation is for CAN take it, and its hold shrinks by what it took",
                  inventory.product_total_qty(conn, display, 1) == 3 and inventory.reserved_qty(conn, batch_a, c1) == 2)
            inventory.release(conn, "test", 1)
            check("releasing frees everything that was held", inventory.available_qty(conn, batch_a, c1) == 3)
            conn.execute(
                "INSERT INTO stock_reservations (batch_id, cell_id, qty, ref_type, ref_id, expires_at) VALUES (?, ?, 3, 'old', 9, datetime('now', '-1 hour'))",
                (batch_a, c1))
            check("an expired reservation holds nothing", inventory.available_qty(conn, batch_a, c1) == 3)

            # ---- клиентский ремонт: запчасть только со склада мастера
            sent = stock_transfers.send(conn, point_1, edik_wh, [(batch_a, c1, 1)], owner)
            stock_transfers.receive(conn, sent, edik)
            client_id = clients.get_or_create_by_phone(conn, "Клиент", "+380671234567", source="offline")
            repair_id = repairs.create_repair(
                conn, client_id, "Смартфон", "Apple", "iPhone 13", None, "Замена дисплея", "offline", edik, 3000, owner, location_id=1)
            check("the master's own claim takes it into work", repairs.claim_repair(conn, repair_id, edik))
            repair = repairs.get_repair(conn, repair_id)
            text, keyboard = repairs.card(conn, repair_id)
            check("the group card reads like the макет and flags the missing part",
                  f"РК-{repair_id:03d} • В работе" in text and "Клиент: +380671234567" in text and "Работа: Замена дисплея" in text
                  and "Ответственный: Эдик" in text and "Запчасть: <b>не указана</b>" in text and "Цена: 3000 грн" in text)
            check("its buttons are «Указать запчасть» and «Готово»",
                  [b["callback_data"] for b in keyboard["inline_keyboard"][0]] == [f"repair_part:{repair_id}", f"repair_done:{repair_id}"])
            check("«нужна запчасть» while in work with nothing said", repairs.needs_part(conn, repair))
            lines = repairs.available_parts(conn, repair_id)
            check("the parts on offer are ONLY what is on the master's own склад (1 display), not the точка's",
                  [(l["product_id"], l["warehouse_id"], l["free"]) for l in lines] == [(display, edik_wh, 1)])
            for args, needle, label in (
                ((batch_a, c1, 1), "нет на складе исполнителя", "a part lying at the точка can't be written off to the master's repair"),
                ((lines[0]["batch_id"], lines[0]["cell_id"], 2), "свободно 1", "nor more than he holds"),
            ):
                try:
                    repairs.use_part(conn, repair_id, *args, edik)
                    check(label, False)
                except repairs.RepairPartError as exc:
                    check(label, needle in str(exc))
            movement = repairs.use_part(conn, repair_id, lines[0]["batch_id"], lines[0]["cell_id"], 1, edik)
            row = conn.execute("SELECT * FROM stock_movements WHERE id = ?", (movement,)).fetchone()
            check("the part is written off from that exact партия, at that партия's cost",
                  row["batch_id"] == batch_a and row["unit_cost"] == 2000 and row["reason"] == "repair_use"
                  and warehouses.total_qty(conn, edik_wh) == 0)
            text, _kb = repairs.card(conn, repair_id)
            check("the card now names the part and its партия",
                  f"Запчасть: Дисплей iPhone 13 × 1 (партия {batch_a})" in text and not repairs.needs_part(conn, repairs.get_repair(conn, repair_id)))

            # ---- прибыль: 3000 − 2000 = 1000 → 33% = 330 → фирме 670
            numbers = repairs.finance(conn, repair_id)
            check("before a final price the estimate is used and marked as such",
                  numbers["price"] == 3000 and not numbers["is_final"] and numbers["base"] == 1000)
            repairs.set_price(conn, repair_id, 3000, 3200)
            repairs.complete_repair(conn, repair_id, edik)
            check("nothing is accrued until the repair is выдан", masters.accrued_total(conn, edik) == 0)
            repairs.update_status(conn, repair_id, "issued", owner)
            numbers = repairs.finance(conn, repair_id)
            check("выдан: клиент 3200 − запчасть 2000 = база 1200 → мастеру 33% = 396 → фирме 804",
                  (numbers["price"], numbers["parts_cost"], numbers["base"], numbers["master_share"], numbers["firm_profit"])
                  == (3200, 2000, 1200, 396, 804))
            check("the master's share is accrued once and the document carries the firm's profit",
                  masters.accrued_total(conn, edik) == 396 and masters.accrued_for(conn, "repair_order", repair_id) == 396
                  and documents.get_for(conn, "repair", repair_id)["profit"] == 804)
            repairs.update_status(conn, repair_id, "ready", owner)
            check("taking it back out of «Выдан» undoes both", masters.accrued_total(conn, edik) == 0
                  and documents.get_for(conn, "repair", repair_id)["profit"] is None)
            repairs.update_status(conn, repair_id, "issued", owner)
            repairs.update_status(conn, repair_id, "issued", owner, "повторно")
            check("re-issuing accrues it again — exactly once", masters.accrued_total(conn, edik) == 396
                  and len(masters.list_accruals(conn, edik)) == 1)
            try:
                repairs.use_part(conn, repair_id, batch_a, c1, 1, edik)
                check("a closed repair takes no more parts", False)
            except repairs.RepairPartError:
                check("a closed repair takes no more parts", True)

            cleaning = repairs.create_repair(conn, client_id, "Смартфон", None, "A54", None, "Чистка", "offline", sergey, 500, owner, location_id=1)
            repairs.claim_repair(conn, cleaning, sergey)
            repairs.declare_no_parts(conn, cleaning)
            check("«без запчасти» is a statement, not an omission",
                  "Запчасть: без запчасти" in repairs.card(conn, cleaning)[0] and not repairs.needs_part(conn, repairs.get_repair(conn, cleaning)))
            repairs.set_price(conn, cleaning, 500, 500)
            repairs.update_status(conn, cleaning, "issued", owner)
            check("a fixed-rate master gets his rate per repair (400), the firm the rest (100)",
                  masters.accrued_total(conn, sergey) == 400 and documents.get_for(conn, "repair", cleaning)["profit"] == 100)
            counter = repairs.create_repair(conn, client_id, "Смартфон", None, "A12", None, "Стекло", "offline", owner, 300, owner, location_id=1)
            check("a repair done by a non-master takes parts from the точка's склад",
                  repairs.parts_warehouse(conn, repairs.get_repair(conn, counter))["id"] == point_1
                  and {l["product_id"] for l in repairs.available_parts(conn, counter)} == {display, akb})

            # ---- производство: макет «Карточка и производство»
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            purchase = buyback.create_purchase(
                conn, seller_phone="0970073090", model="iPhone 13 · 128 GB · Black", imei="356000000000001",
                comment="дисплей, АКБ, зарядка", price="5000", staff_id=owner, location_id=1)
            unit = inventory.find_unit_by_imei(conn, "356000000000001")
            for imei, needle, label in (("999000", "не числится", "an IMEI we don't have can't start an order"),):
                try:
                    production.create_order(conn, imei=imei, staff_id=owner, location_id=1)
                    check(label, False)
                except production.ProductionError as exc:
                    check(label, needle in str(exc))
            order_id = production.create_order(conn, imei="356 000 000 000 001", staff_id=owner, location_id=1, task="дисплей, АКБ")
            order = production.get_order(conn, order_id)
            check("the order starts as a draft tied to the phone and its покупка, at its purchase cost",
                  order["status"] == "draft" and order["batch_id"] == unit["id"] and order["buyback_order_id"] == purchase
                  and order["cost_before"] == 5000 and documents.doc_label(documents.get_for(conn, "production", order_id)) == f"ПР-{order_id:03d}")
            try:
                production.create_order(conn, imei="356000000000001", staff_id=owner, location_id=1)
                check("the same phone can't be in two orders", False)
            except production.ProductionError as exc:
                check("the same phone can't be in two orders", f"ПР-{order_id:03d}" in str(exc))
            try:
                sales.create_sale(conn, None, "offline", owner, [(unit["product_id"], 1, 9000, unit["id"])], location_id=1)
                check("a phone held for производство can't be sold", False)
            except inventory.InsufficientStockError:
                check("a phone held for производство can't be sold", True)

            picks = production.available_parts(conn, order_id)
            check("«детали со всех складов»: both точки, each line with supplier, приход and cost; non-parts are not offered",
                  {(l["product_name"], l["supplier_name"], l["location_id"], l["free"], l["unit_cost_uah"]) for l in picks}
                  == {("Дисплей iPhone 13", "Поставщик А", 1, 2, 2000), ("Дисплей iPhone 13", "Поставщик Б", 2, 1, 2200),
                      ("АКБ iPhone 13", "Поставщик В", 1, 3, 700)})
            check("the list can be narrowed by name", {l["product_name"] for l in production.available_parts(conn, order_id, "акб")} == {"АКБ iPhone 13"})
            line = {(l["product_id"], l["location_id"]): l for l in picks}
            d1, d2, a1 = line[(display, 1)], line[(display, 2)], line[(akb, 1)]
            try:
                production.set_parts(conn, order_id, [(d1["batch_id"], d1["cell_id"], 5)], owner)
                check("reserving more than is free is refused", False)
            except production.ProductionError:
                check("reserving more than is free is refused", True)
            production.set_parts(conn, order_id, [(d2["batch_id"], d2["cell_id"], 1)], owner)
            production.set_parts(conn, order_id, [(d1["batch_id"], d1["cell_id"], 1), (a1["batch_id"], a1["cell_id"], 1)], owner)
            check("«Зарезервировать» replaces the selection: the first pick is free again, the new one is held",
                  inventory.available_qty(conn, d2["batch_id"], d2["cell_id"]) == 1 and inventory.available_qty(conn, d1["batch_id"], d1["cell_id"]) == 1
                  and inventory.available_qty(conn, a1["batch_id"], a1["cell_id"]) == 2
                  and [(p["product_name"], p["qty"]) for p in production.get_parts(conn, order_id)] == [("Дисплей iPhone 13", 1), ("АКБ iPhone 13", 1)])
            check("held parts are still on the точка's shelf but not on offer to a client repair",
                  {l["product_id"]: l["free"] for l in repairs.available_parts(conn, counter)} == {display: 1, akb: 2})
            try:
                production.hand_over(conn, order_id, owner)
                check("handover needs a master", False)
            except production.ProductionError as exc:
                check("handover needs a master", "выберите мастера" in str(exc))
            try:
                production.assign_master(conn, order_id, owner)
                check("only an active master can be assigned", False)
            except production.ProductionError:
                check("only an active master can be assigned", True)
            production.assign_master(conn, order_id, sergey, "1000")
            transfers = production.hand_over(conn, order_id, owner)
            order = production.get_order(conn, order_id)
            check("«Передать в производство»: one перемещение from the точка, already received — phone and parts are on the master's склад",
                  order["status"] == "in_work" and len(transfers) == 1
                  and stock_transfers.get_transfer(conn, transfers[0])["status"] == "received"
                  and warehouses.total_qty(conn, sergey_wh) == 3 and inventory.product_total_qty(conn, akb, 1) == 2)
            check("«передача ≠ расход»: nothing is written off yet, and the goods stay held for the order at the master's",
                  inventory.stock_value(conn) == 5000 + 2 * 2000 + 2200 + 3 * 700 + 5 * 100
                  and all(l["qty"] - l["reserved"] == 0 for l in inventory.stock_lines(conn, warehouse_id=sergey_wh)))
            other = repairs.create_repair(conn, client_id, "Смартфон", None, "iPhone 13", None, "Дисплей", "offline", sergey, 3000, owner, location_id=1)
            check("the master can't put the order's parts into a client's repair", repairs.available_parts(conn, other) == [])

            parts = {p["product_id"]: p for p in production.get_parts(conn, order_id)}
            for kwargs, needle, label in (
                ({"used": {parts[display]["id"]: 3}}, "нельзя отчитаться", "reporting more used than was handed over is refused"),
                ({"extras": [("Шлейф", "")]}, "название и сумма", "a master's own part needs both a name and an amount"),
            ):
                try:
                    production.submit_report(conn, order_id, sergey, **kwargs)
                    check(label, False)
                except production.ProductionError as exc:
                    check(label, needle in str(exc))
            try:
                production.accept(conn, order_id, owner)
                check("a result can't be accepted before the report", False)
            except production.ProductionError:
                check("a result can't be accepted before the report", True)
            production.submit_report(conn, order_id, sergey, extras=[("Шлейф мастера", "400"), ("Проклейка мастера", "100"), ("", "")])
            numbers = production.preview(conn, order_id)
            check("the report adds up as on the макет: покупка 5000 + наши детали 2700 + детали мастера 500 + работа 1000 = 9200; мастеру 1500",
                  (numbers["before"], numbers["parts"], numbers["extras"], numbers["work"], numbers["total"], numbers["to_master"])
                  == (5000, 2700, 500, 1000, 9200, 1500) and production.get_order(conn, order_id)["status"] == "reported")
            owed_before = masters.accrued_total(conn, sergey)
            back = production.accept(conn, order_id, owner)
            order = production.get_order(conn, order_id)
            check("«Принять результат»: the phone's cost is now 9200, fixed on the order",
                  order["status"] == "done" and order["cost_after"] == 9200 and order["cost_parts"] == 2700
                  and inventory.get_batch(conn, unit["id"])["unit_cost_uah"] == 9200
                  and documents.get_for(conn, "production", order_id)["amount"] == 9200)
            check("the used parts are written off for good (each from its партия), nothing else",
                  inventory.product_total_qty(conn, display) == 1 + 1 and inventory.product_total_qty(conn, akb) == 2
                  and [(m["qty"], m["unit_cost"]) for m in conn.execute(
                      "SELECT qty, unit_cost FROM stock_movements WHERE ref_type = 'production_order' AND ref_id = ? ORDER BY id", (order_id,))]
                  == [(1, 2000), (1, 700)])
            check("the master is owed работа + his own parts (1500) for this order",
                  masters.accrued_total(conn, sergey) == owed_before + 1500 and masters.accrued_for(conn, "production_order", order_id) == 1500)
            returning = stock_transfers.get_transfer(conn, back)
            check("the finished phone goes back «Мастер → В пути → Точка» and waits to be received",
                  returning["status"] == "sent" and returning["to_warehouse_id"] == point_1
                  and warehouses.total_qty(conn, sergey_wh) == 0 and inventory.find_unit_by_imei(conn, "356000000000001") is not None)
            stock_transfers.receive(conn, back, owner)
            sold = sales.create_sale(conn, None, "offline", owner, [(unit["product_id"], 1, 12000, unit["id"])], location_id=1)
            check("received at the точка it sells at its real cost: 12 000 − 9 200",
                  sales.get_sale_items(conn, sold)[0]["unit_cost"] == 9200)
            check("no hold of the order is left anywhere",
                  conn.execute("SELECT COUNT(*) AS n FROM stock_reservations WHERE ref_type = 'production' AND ref_id = ? AND released_at IS NULL", (order_id,)).fetchone()["n"] == 0
                  and _stock_is_consistent(conn))

            # ---- неиспользованное возвращается; черновик можно отменить
            second = buyback.create_purchase(conn, seller_phone="0970073090", model="iPhone 13 · 128 GB · Black",
                                             imei="356000000000002", comment=None, price="4000", staff_id=owner, location_id=1)
            order_2 = production.create_order(conn, imei="356000000000002", staff_id=owner, location_id=1)
            free_akb = next(l for l in production.available_parts(conn, order_2) if l["product_id"] == akb)
            production.set_parts(conn, order_2, [(free_akb["batch_id"], free_akb["cell_id"], 2)], owner)
            production.assign_master(conn, order_2, sergey, "600")
            production.hand_over(conn, order_2, owner)
            part_id = production.get_parts(conn, order_2)[0]["id"]
            production.submit_report(conn, order_2, sergey, used={part_id: 1}, work_price="800")
            back_2 = production.accept(conn, order_2, owner)
            check("only what the master reported as used is spent; the price of the work can be corrected in the report",
                  production.get_order(conn, order_2)["cost_after"] == 4000 + 700 + 800)
            check("the unused part travels back with the phone",
                  sorted((i["product_id"], i["qty"]) for i in stock_transfers.get_items(conn, back_2))
                  == sorted([(unit["product_id"], 1), (akb, 1)]))
            stock_transfers.receive(conn, back_2, owner)
            third = buyback.create_purchase(conn, seller_phone="0970073090", model="Samsung A54", imei="356000000000003",
                                            comment=None, price="3000", staff_id=owner, location_id=1)
            draft = production.create_order(conn, imei="356000000000003", staff_id=owner, location_id=1)
            a_line = next(l for l in production.available_parts(conn, draft) if l["product_id"] == akb)
            production.set_parts(conn, draft, [(a_line["batch_id"], a_line["cell_id"], 1)], owner)
            production.cancel(conn, draft, owner)
            check("cancelling a draft frees the phone and the parts, and the document stays, cancelled",
                  production.get_order(conn, draft)["status"] == "cancelled"
                  and inventory.available_qty(conn, a_line["batch_id"], a_line["cell_id"]) == 1
                  and documents.get_for(conn, "production", draft)["status"] == "cancelled"
                  and production.active_order_for_batch(conn, inventory.find_unit_by_imei(conn, "356000000000003")["id"]) is None)
            try:
                production.cancel(conn, order_2, owner)
                check("an accepted order can't be cancelled — its parts are spent", False)
            except production.ProductionError:
                check("an accepted order can't be cancelled — its parts are spent", True)

            # ---- отмена после передачи мастеру: всё едет обратно, ничего не списано
            fourth_unit = inventory.find_unit_by_imei(conn, "356000000000003")
            value_before, owed_before = inventory.stock_value(conn), masters.accrued_total(conn, sergey)
            called_off = production.create_order(conn, imei="356000000000003", staff_id=owner, location_id=1)
            a_line = next(l for l in production.available_parts(conn, called_off) if l["product_id"] == akb)
            production.set_parts(conn, called_off, [(a_line["batch_id"], a_line["cell_id"], 1)], owner)
            production.assign_master(conn, called_off, sergey, "500")
            production.hand_over(conn, called_off, owner)
            production.submit_report(conn, called_off, sergey, extras=[("Шлейф", "200")])
            returning = production.cancel(conn, called_off, owner)
            cancelled = production.get_order(conn, called_off)
            check("cancelling after the handover sends the phone and EVERY part back in one перемещение",
                  cancelled["status"] == "cancelled" and cancelled["return_transfer_id"] == returning
                  and stock_transfers.get_transfer(conn, returning)["status"] == "sent"
                  and sorted((i["product_id"], i["qty"]) for i in stock_transfers.get_items(conn, returning))
                  == sorted([(fourth_unit["product_id"], 1), (akb, 1)]) and warehouses.total_qty(conn, sergey_wh) == 0)
            check("nothing was written off, the phone's cost is unchanged, the master is owed nothing for it, no hold is left",
                  inventory.stock_value(conn) == value_before and masters.accrued_total(conn, sergey) == owed_before
                  and inventory.get_batch(conn, fourth_unit["id"])["unit_cost_uah"] == 3000
                  and conn.execute("SELECT COUNT(*) AS n FROM stock_reservations WHERE ref_type = 'production' AND ref_id = ? AND released_at IS NULL", (called_off,)).fetchone()["n"] == 0
                  and documents.get_for(conn, "production", called_off)["status"] == "cancelled" and _stock_is_consistent(conn))
            stock_transfers.receive(conn, returning, owner)
            via_journal = production.create_order(conn, imei="356000000000003", staff_id=owner, location_id=1)
            production.assign_master(conn, via_journal, sergey, "500")
            production.hand_over(conn, via_journal, owner)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "production", via_journal)["id"], owner, "передумали")
            check("the same from the journal: the order is cancelled and the phone is on its way back",
                  production.get_order(conn, via_journal)["status"] == "cancelled"
                  and production.get_order(conn, via_journal)["return_transfer_id"] is not None)
            try:
                doc_cancel.cancel_document(conn, documents.get_for(conn, "production", order_2)["id"], owner, "поздно")
                check("an accepted order can't be cancelled from the journal either", False)
            except documents.DocumentError:
                check("an accepted order can't be cancelled from the journal either", True)
            check("orders can be listed by status and by master",
                  [o["id"] for o in production.list_orders(conn, statuses=("done",), master_id=sergey)] == [order_2, order_id])


def scenario_service_center_http() -> None:
    print("scenario: сервисный центр over HTTP — карточка ремонта, запчасть, прибыль, производство, мастера")
    from core import notify, production, stock_transfers, warehouses

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "sc-http.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "sh5-owner", "pass", "Владелец", "owner")
            keeper = auth.create_staff(conn, "sh5-keeper", "pass", "Кладовщик", "storekeeper", location_id=1)
            master = auth.create_master(conn, "Сергей", 990011, "percent", 33)
            other_master = auth.create_master(conn, "Чужой Мастер", None, None, None)
            supplier = purchases.create_supplier(conn, "Поставщик А", None)
            cell = inventory.create_cell(conn, "S5-1", None, None, location_id=1)
            display = inventory.create_product(conn, "Дисплей iPhone 13", "S5-DSP", None, "шт", True, True, 0, 3500)
            akb = inventory.create_product(conn, "АКБ iPhone 13", "S5-AKB", None, "шт", True, True, 0, 1200)
            receipt = purchases.create_receipt(conn, supplier, "125", owner, [(display, cell, 3, 2000), (akb, cell, 2, 700)], location_id=1)
            batch = {b["product_id"]: b["id"] for b in purchases.get_receipt_batches(conn, receipt)}
            point = warehouses.point_warehouse(conn, 1)["id"]
            master_wh = warehouses.master_warehouse(conn, master)["id"]
            master_cell = warehouses.cells(conn, master_wh)[0]["id"]
            sent = stock_transfers.send(conn, point, master_wh, [(batch[display], cell, 1)], owner)
            stock_transfers.receive(conn, sent, master)
            client_id = clients.get_or_create_by_phone(conn, "Клиент", "+380671234500", source="offline")
            repair_id = repairs.create_repair(conn, client_id, "Смартфон", "Apple", "iPhone 13", None, "Замена дисплея", "offline", master, 3000, owner, location_id=1)
            repairs.claim_repair(conn, repair_id, master)
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            purchase = buyback.create_purchase(conn, seller_phone="0970073090", model="iPhone 13 · 128 GB", imei="356000000000555",
                                               comment="дисплей, АКБ", price="5000", staff_id=owner, location_id=1)
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
        token, keeper_token = make_token(owner, "1"), make_token(keeper, "1")
        master_token, other_token = make_token(master, "1"), make_token(other_master, "1")

        import webapp.main

        with TestClient(webapp.main.app) as client:
            # ---- ремонт клиента
            page = client.get(f"/repairs/{repair_id}?t={token}").text
            check("the repair page flags the missing part and offers the master's склад lines (партия, supplier, free qty)",
                  "Нужна запчасть" in page and "Мастер: Сергей" in page and f'value="{batch[display]}:{master_cell}"' in page
                  and "Поставщик А" in page and f"/repairs/{repair_id}/no-parts" in page)
            check("the owner sees the repair's profit block; the master doesn't",
                  "Прибыль ремонта" in page and "Прибыль ремонта" not in client.get(f"/repairs/{repair_id}?t={master_token}").text)
            check("the «Ремонты» page has the service-centre tabs", "/production" in client.get(f"/repairs?t={token}").text)
            wrong = client.post(f"/repairs/{repair_id}/parts?t={master_token}", data={"line": f"{batch[display]}:{cell}", "qty": "1"})
            check("a part from the точка's shelf is a friendly error, not a write-off", "нет на складе исполнителя" in wrong.text)
            ok = client.post(f"/repairs/{repair_id}/parts?t={master_token}", data={"line": f"{batch[display]}:{master_cell}", "qty": "1"}, follow_redirects=False)
            check("the right line is written off", ok.status_code == 303)
            page = client.get(f"/repairs/{repair_id}?t={token}").text
            check("the part shows with its партия, supplier and cost; the flag is gone; the profit follows (3000 − 2000 → 33% = 330)",
                  f"№{batch[display]}" in page and "2000 грн" in page and "Нужна запчасть" not in page and ">330 грн<" in page and ">670 грн<" in page)
            client.post(f"/repairs/{repair_id}/price?t={token}", data={"price_estimate": "3000", "price_final": "3000"})
            client.post(f"/repairs/{repair_id}/status?t={token}", data={"status": "issued", f"pay_{acc[('cash', 'UAH')]}": "3000"})
            with get_conn(db_path) as conn:
                check("выдача accrues the master's 330 and gives the document its profit",
                      masters.accrued_total(conn, master) == 330 and documents.get_for(conn, "repair", repair_id)["profit"] == 670)
            second = None
            with get_conn(db_path) as conn:
                second = repairs.create_repair(conn, client_id, "Смартфон", None, "A54", None, "Чистка", "offline", master, 400, owner, location_id=1)
                repairs.claim_repair(conn, second, master)
            client.post(f"/repairs/{second}/no-parts?t={master_token}")
            page = client.get(f"/repairs/{second}?t={token}").text
            check("«Без запчасти» is recorded and shown", "ремонт без запчасти" in page and "Нужна запчасть" not in page)

            # ---- мастера: тип, навыки, начисления
            client.post(f"/masters/{master}/edit?t={token}", data={"name": "Сергей", "telegram_id": "990011", "pay_type": "percent", "pay_value": "33",
                                                                  "master_kind": "outsource", "skills": "замена дисплея, АКБ, разъёмы"})
            mpage = client.get(f"/masters/{master}?t={token}").text
            check("the master's card keeps type and skills and shows what he has been accrued",
                  "замена дисплея, АКБ, разъёмы" in mpage and "Начисления" in mpage and ">330 грн<" in mpage and f"РК-{repair_id:03d}" in mpage)
            with get_conn(db_path) as conn:
                check("saved as аутсорс", auth.get_master(conn, master)["master_kind"] == "outsource")

            # ---- производство
            card = client.get(f"/buyback/{purchase}?t={token}").text
            check("the покупка's card offers «Подобрать детали»", "Подобрать детали" in card and 'name="imei" value="356000000000555"' in card)
            check("a master can't start an order", client.post(f"/production?t={master_token}", data={"imei": "356000000000555"}, follow_redirects=False).headers["location"].startswith("/production?"))
            bad = client.post(f"/production?t={keeper_token}", data={"imei": "000"})
            check("an unknown IMEI is a friendly error", bad.status_code == 200 and "не числится" in bad.text)
            made = client.post(f"/production?t={keeper_token}", data={"imei": "356000000000555", "task": "дисплей, АКБ"}, follow_redirects=False)
            order_id = int(made.headers["location"].split("/production/")[1].split("?")[0])
            check("the покупка's card now links to its order instead", f"/production/{order_id}" in client.get(f"/buyback/{purchase}?t={token}").text)
            detail = client.get(f"/production/{order_id}?t={keeper_token}").text
            check("the draft shows «детали со всех складов» with supplier, закупка and cost, and the steps of the макет",
                  all(x in detail for x in ("Детали со всех складов", "Поставщик А", f"qty_{batch[display]}_{cell}", f"qty_{batch[akb]}_{cell}",
                                            "Зарезервировать", "Передать в производство", "Аутсорс")))
            check("the list of masters can be narrowed to аутсорс (only Сергей)",
                  "Сергей" in client.get(f"/production/{order_id}?t={keeper_token}&kind=outsource").text.split('name="master_id"')[1].split("</select>")[0]
                  and "Чужой Мастер" not in client.get(f"/production/{order_id}?t={keeper_token}&kind=outsource").text.split('name="master_id"')[1].split("</select>")[0])
            over = client.post(f"/production/{order_id}/parts?t={keeper_token}", data={f"qty_{batch[display]}_{cell}": "9"})
            check("reserving more than is free is a friendly error", "нельзя зарезервировать" in over.text)
            client.post(f"/production/{order_id}/parts?t={keeper_token}", data={f"qty_{batch[display]}_{cell}": "1", f"qty_{batch[akb]}_{cell}": "1"})
            no_master = client.post(f"/production/{order_id}/handover?t={keeper_token}")
            check("handover without a master is a friendly error", "выберите мастера" in no_master.text)
            client.post(f"/production/{order_id}/master?t={keeper_token}", data={"master_id": str(master), "work_price": "1000"})

            calls = []

            class _Resp:
                status_code, text = 200, "ok"

                def json(self):
                    return {"result": {"message_id": 1}}

            def _fake_post(url, **kwargs):
                calls.append({"method": url.rsplit("/", 1)[1], "json": kwargs.get("json")})
                return _Resp()

            orig_post, orig_token, orig_url = httpx.post, notify._BOT_TOKEN, os.environ.get("CRM_MINIAPP_URL")
            httpx.post, notify._BOT_TOKEN = _fake_post, "test-token"
            os.environ["CRM_MINIAPP_URL"] = "https://crm.example/miniapp"
            try:
                handed = client.post(f"/production/{order_id}/handover?t={keeper_token}", follow_redirects=False)
            finally:
                httpx.post, notify._BOT_TOKEN = orig_post, orig_token
                if orig_url is None:
                    os.environ.pop("CRM_MINIAPP_URL", None)
                else:
                    os.environ["CRM_MINIAPP_URL"] = orig_url
            check("handover goes through", handed.status_code == 303)
            note = next((c["json"] for c in calls if c["method"] == "sendMessage" and c["json"]["chat_id"] == 990011), None)
            check("the master is told in his DM, with the list and a button straight to the report",
                  note is not None and f"ПР-{order_id:03d}" in note["text"] and "Дисплей iPhone 13" in note["text"]
                  and note["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"].startswith(f"https://crm.example/production/{order_id}?t="))
            with get_conn(db_path) as conn:
                check("the phone and both parts are now on the master's склад", warehouses.total_qty(conn, master_wh) == 3)
            check("the master's own list shows the order; another master's doesn't",
                  f"/production/{order_id}" in client.get(f"/production?t={master_token}").text
                  and f"/production/{order_id}" not in client.get(f"/production?t={other_token}").text)
            report_page = client.get(f"/production/{order_id}?t={master_token}").text
            check("the master sees the report form (used parts, his own parts, work) but no «Принять результат»",
                  f"/production/{order_id}/report" in report_page and "extra_title_0" in report_page and f"/production/{order_id}/accept" not in report_page)
            denied = client.post(f"/production/{order_id}/report?t={other_token}", data={})
            check("another master can't file the report", "мастер этого заказа" in denied.text)
            with get_conn(db_path) as conn:
                parts = {p["product_id"]: p["id"] for p in production.get_parts(conn, order_id)}
            client.post(f"/production/{order_id}/report?t={master_token}", data={
                f"used_{parts[display]}": "1", f"used_{parts[akb]}": "1", "extra_title_0": "Шлейф мастера", "extra_amount_0": "400",
                "extra_title_1": "Проклейка мастера", "extra_amount_1": "100", "work_price": "1000", "note": "всё ок"})
            reported = client.get(f"/production/{order_id}?t={keeper_token}").text
            check("after the report the keeper sees the макет's numbers and «Принять результат»",
                  all(x in reported for x in (">5000 грн<", ">2700 грн<", ">500 грн<", ">1000 грн<", ">9200 грн<", ">1500 грн<"))
                  and f"/production/{order_id}/accept" in reported)
            check("the master can't accept his own result",
                  "кладовщик, админ или владелец" in client.post(f"/production/{order_id}/accept?t={master_token}").text)
            client.post(f"/production/{order_id}/accept?t={keeper_token}")
            done = client.get(f"/production/{order_id}?t={token}").text
            check("the result card: «Готов к продаже», себестоимость 9200, the return transfer, what the master is owed",
                  "Готов к продаже" in done and ">9200 грн<" in done and "/transfers/" in done and "начислено мастеру 1500" in done
                  and "Шлейф мастера" in done)
            with get_conn(db_path) as conn:
                check("the phone's cost is 9200 and the master's accruals are 330 + 1500",
                      inventory.find_unit_by_imei(conn, "356000000000555")["unit_cost_uah"] == 9200 and masters.accrued_total(conn, master) == 1830)
            check("the finished order moved to «Производство: готовые»",
                  f"/production/{order_id}" in client.get(f"/production?show=done&t={token}").text
                  and f"/production/{order_id}" not in client.get(f"/production?t={token}").text)
            journal = client.get(f"/journal?t={token}").text
            check("the journal has the production document", f"ПР-{order_id:03d}" in journal and "Производство" in journal)
            check("an unknown order id goes back to the list", client.get(f"/production/99999?t={token}", follow_redirects=False).status_code == 303)


def scenario_repair_part_chat() -> None:
    """The bot side of a client repair in Заход 5: приём с выбором мастера
    на карточке, «Указать запчасть» в личке, «Готово» не закрывает ремонт,
    пока не сказано, что установили."""
    print("scenario: ремонт клиента в боте — мастер на карточке, «Указать запчасть», «Готово»")
    import asyncio
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa
    from bot import repair_actions as ra
    from core import stock_transfers, warehouses

    OWNER_TG, MASTER_TG, GROUP = 883001, 883002, -100883

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "repair-chat.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ? WHERE id = 1", (GROUP,))
            owner = auth.create_staff(conn, "rc-owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "rc-owner", OWNER_TG)
            master = auth.create_master(conn, "Эдик", MASTER_TG, "percent", 33)
            cell = inventory.create_cell(conn, "RC-1", None, None, location_id=1)
            display = inventory.create_product(conn, "Дисплей", "RC-DSP", None, "шт", True, True, 0, 3000)
            receipt = purchases.create_receipt(conn, None, None, owner, [(display, cell, 3, 1200)], location_id=1)
            batch_id = purchases.get_receipt_batches(conn, receipt)[0]["id"]
            master_wh = warehouses.master_warehouse(conn, master)["id"]
            sent = stock_transfers.send(conn, warehouses.point_warehouse(conn, 1)["id"], master_wh, [(batch_id, cell, 2)], owner)
            stock_transfers.receive(conn, sent, master)
            clients.get_or_create_by_phone(conn, "Постоянный Клиент", "+380671110099", source="offline")
            shifts.open_shift(conn, owner, 1)

        photo_bytes = io.BytesIO()
        Image.new("RGB", (40, 40), "white").save(photo_bytes, format="JPEG")
        dms: list[dict] = []

        class _Chat:
            def __init__(self):
                self.log, self._next = [], 700

            def add(self, author, text, markup=None, kind="text"):
                self._next += 1
                self.log.append({"id": self._next, "author": author, "text": text, "markup": markup, "kind": kind})
                return self._next

            def delete(self, message_id):
                self.log = [m for m in self.log if m["id"] != message_id]

            def edit(self, message_id, text=None, markup=None, keep_text=False):
                for m in self.log:
                    if m["id"] == message_id:
                        if not keep_text:
                            m["text"] = text
                        m["markup"] = markup

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

            async def get_file(self, file_id):
                return SimpleNamespace(file_path="x")

            async def download_file(self, file_path):
                return io.BytesIO(photo_bytes.getvalue())

            async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
                dms.append({"to": chat_id, "text": text, "markup": reply_markup})

        class _Msg:
            def __init__(self, chat, bot, text=None, photo=False):
                self._log, self.bot, self.text = chat, bot, text
                self.photo = [SimpleNamespace(file_id="f")] if photo else None
                self.chat = SimpleNamespace(id=OWNER_TG, type="private")
                self.from_user = SimpleNamespace(id=OWNER_TG, full_name="Владелец")
                self.message_id = chat.add("staff", text or "[фото]")

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

            async def answer_photo(self, photo, caption=None, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", caption, reply_markup, kind="photo"))

        class _CbMessage:
            def __init__(self, chat, message_id, chat_id):
                self._log, self.message_id = chat, message_id
                self.chat = SimpleNamespace(id=chat_id, type="private" if chat_id > 0 else "supergroup")

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

            async def edit_reply_markup(self, reply_markup=None):
                self._log.edit(self.message_id, markup=reply_markup, keep_text=True)

            async def edit_caption(self, caption=None, reply_markup=None):
                self._log.edit(self.message_id, caption, reply_markup)

            async def edit_text(self, text, reply_markup=None):
                self._log.edit(self.message_id, text, reply_markup)

        class _Cb:
            def __init__(self, chat, bot, data, message_id, user_id, chat_id):
                self.data, self.bot = data, bot
                self.from_user = SimpleNamespace(id=user_id)
                self.message = _CbMessage(chat, message_id, chat_id)
                self.answered = []

            async def answer(self, text=None, show_alert=False):
                self.answered.append((text, show_alert))

        def _buttons(markup) -> list[str]:
            return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] if markup else []

        async def run() -> None:
            chat = _Chat()
            bot = _Bot(chat)
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=OWNER_TG, user_id=OWNER_TG))
            say = lambda text: _Msg(chat, bot, text)
            screen = lambda: [m for m in chat.log if m["author"] == "bot"][-1]
            tap = lambda data: _Cb(chat, bot, data, screen()["id"], OWNER_TG, OWNER_TG)

            # ---- приём: знакомый клиент — имя не спрашиваем; мастер выбирается на карточке
            await qa.repair_start(say(qa.BTN_REPAIR), state)
            await qa.repair_got_photo(_Msg(chat, bot, photo=True), state)
            await qa.repair_got_defect(say("Замена дисплея"), state)
            await qa.repair_got_model(say("iPhone 13"), state)
            await qa.repair_got_price(say("3000"), state)
            await qa.repair_got_phone(say("067 111 00 99"), state)
            card = screen()
            check("a returning client's name is not asked again — straight to the card with it filled in",
                  card["kind"] == "photo" and "Имя: Постоянный Клиент" in card["text"] and "Мастер: не назначен" in card["text"])
            check("the card offers «Создать ремонт», «Мастер» and «Отмена»",
                  set(_buttons(card["markup"])) == {"quick_repair_confirm", "rm_menu", "quick_repair_cancel"})
            await qa.repair_master_menu(tap("rm_menu"), state)
            check("«Мастер» turns the buttons into the list of masters", f"rm_set:{master}" in _buttons(screen()["markup"]) and "rm_set:0" in _buttons(screen()["markup"]))
            await qa.repair_master_set(tap(f"rm_set:{master}"), state)
            check("picking one rewrites the «Мастер» line and brings the buttons back",
                  "Мастер: Эдик" in screen()["text"] and "quick_repair_confirm" in _buttons(screen()["markup"]))
            await qa.repair_confirm(tap("quick_repair_confirm"), state)
            with get_conn(db_path) as conn:
                repair = repairs.list_repairs(conn)[0]
                check("the repair is created for that client, with that master and price",
                      repair["master_id"] == master and repair["price_estimate"] == 3000 and repair["client_name"] == "Постоянный Клиент")
                repair_id = repair["id"]
            check("the confirmation names the document", f"РК-{repair_id:03d} создан" in chat.log[-1]["text"])

            # ---- группа: «Взять в работу» → «Готово» без запчасти не проходит
            group_cb = lambda data, user: _Cb(chat, bot, data, 1, user, GROUP)
            await ra.repair_take(group_cb(f"repair_take:{repair_id}", MASTER_TG))
            done = group_cb(f"repair_done:{repair_id}", MASTER_TG)
            await ra.repair_done(done)
            with get_conn(db_path) as conn:
                check("«Готово» with no part named does NOT close the repair",
                      repairs.get_repair(conn, repair_id)["status"] == "in_progress" and done.answered[-1][1] is True
                      and "укажите запчасть" in done.answered[-1][0])
            picker = dms[-1]
            check("instead the master gets the picker in his DM: what is on HIS склад, one button per партия, plus «Без запчасти»",
                  picker["to"] == MASTER_TG and f"РК-{repair_id:03d}" in picker["text"]
                  and _buttons(picker["markup"])[0].startswith(f"rp_pick:{repair_id}:{batch_id}:") and f"rp_none:{repair_id}" in _buttons(picker["markup"])
                  and "2 шт" in picker["markup"].inline_keyboard[0][0].text)
            stranger = group_cb(f"repair_part:{repair_id}", 4242)
            await ra.repair_part(stranger)
            check("someone who isn't staff can't open the picker", stranger.answered[-1][1] is True)

            dm_id = chat.add("bot", picker["text"], picker["markup"])
            dm_cb = lambda data, user=MASTER_TG: _Cb(chat, bot, data, dm_id, user, user)
            pick_data = _buttons(picker["markup"])[0]
            await ra.repair_part_pick(dm_cb(pick_data))
            await asyncio.sleep(0.05)
            with get_conn(db_path) as conn:
                used = repairs.get_used_parts(conn, repair_id)
                check("one tap writes off one piece of that партия from the master's склад",
                      [(p["batch_id"], p["qty"], p["unit_cost"]) for p in used] == [(batch_id, 1, 1200)] and warehouses.total_qty(conn, master_wh) == 1)
            dm = next(m for m in chat.log if m["id"] == dm_id)
            check("the picker now shows what was written off and offers «Готово»",
                  "Списано: Дисплей × 1" in dm["text"] and f"rp_done:{repair_id}" in _buttons(dm["markup"]))
            other = dm_cb(pick_data, OWNER_TG)
            await ra.repair_part_pick(other)
            with get_conn(db_path) as conn:
                check("an owner may add a part on the master's behalf (from the master's склад)", len(repairs.get_used_parts(conn, repair_id)) == 2)
            await ra.repair_part_done(dm_cb(f"rp_done:{repair_id}"))
            await asyncio.sleep(0.05)
            with get_conn(db_path) as conn:
                check("«Готово» from the picker closes the repair as готов к выдаче",
                      repairs.get_repair(conn, repair_id)["status"] == "ready")
            check("and the picker turns into a final line with no buttons",
                  "Готов к выдаче" in next(m for m in chat.log if m["id"] == dm_id)["text"]
                  and not next(m for m in chat.log if m["id"] == dm_id)["markup"])

            # ---- «Без запчасти»
            with get_conn(db_path) as conn:
                client_id = clients.get_by_phone(conn, "+380671110099")["id"]
                second = repairs.create_repair(conn, client_id, "Смартфон", None, "A54", None, "Чистка", "offline", master, 400, owner, location_id=1)
                repairs.claim_repair(conn, second, master)
            await ra.repair_part(group_cb(f"repair_part:{second}", MASTER_TG))
            dm2 = chat.add("bot", dms[-1]["text"], dms[-1]["markup"])
            await ra.repair_part_none(_Cb(chat, bot, f"rp_none:{second}", dm2, MASTER_TG, MASTER_TG))
            await asyncio.sleep(0.05)
            with get_conn(db_path) as conn:
                check("«Без запчасти» is recorded", repairs.get_repair(conn, second)["no_parts"] == 1)
            await ra.repair_done(group_cb(f"repair_done:{second}", MASTER_TG))
            await asyncio.sleep(0.05)
            with get_conn(db_path) as conn:
                check("after that «Готово» in the group goes straight through", repairs.get_repair(conn, second)["status"] == "ready")

            # ---- «Наш ремонт»
            await qa.our_repair_start(say(qa.BTN_OUR_REPAIR), state)
            check("«Наш ремонт» opens the Mini App pages (a web_app button)",
                  "Наши ремонты" in screen()["text"] and screen()["markup"].inline_keyboard[0][0].web_app is not None)

        asyncio.run(run())


def scenario_settlements() -> None:
    """Заход 6 core: взаиморасчёты, продажа в долг, приём/выдача денег,
    сверка, заказ с резервом на 24 часа, выплата мастеру."""
    print("scenario: взаиморасчёты, продажа в долг, заказы с резервом 24 ч, выплата мастеру")
    from core import orders, settlements

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "settle.sqlite3", ("Магазин",)) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "st-owner", "pass", "Владелец", "owner")
            master = auth.create_master(conn, "Эдик", None, "percent", 33)
            cell = inventory.create_cell(conn, "ST-1", None, None, location_id=1)
            cable = inventory.create_product(conn, "Кабель", "ST-CBL", None, "шт", False, True, 0, 300)
            purchases.create_receipt(conn, None, None, owner, [(cable, cell, 10, 100)], location_id=1)
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
            cash_uah, usd = acc[("cash", "UAH")], acc[("cash", "USD")]
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            anna = clients.get_or_create_by_phone(conn, "Анна", "+380671230001", source="offline")

            # ---- обычная продажа: в сверке обе строки, баланс 0, прибыль у документа
            paid_sale = sales.create_sale(conn, anna, "offline", owner, [(cable, 2, 300)], location_id=1)
            check("a sale paid on the spot leaves the client's balance at zero",
                  settlements.balance(conn, anna) == 0 and settlements.client_position(conn, anna) == {"balance": 0, "they_owe": 0, "we_owe": 0})
            check("the sale's document carries its profit: 600 − 2 × 100", documents.get_for(conn, "sale", paid_sale)["profit"] == 400)

            # ---- в долг
            try:
                sales.create_sale(conn, None, "offline", owner, [(cable, 1, 300)], location_id=1, payments=[], allow_debt=True)
                check("a sale «в долг» needs a client", False)
            except sales.SaleError as exc:
                check("a sale «в долг» needs a client", "номером телефона" in str(exc))
            try:
                sales.create_sale(conn, anna, "offline", owner, [(cable, 1, 300)], location_id=1, payments=[(cash_uah, "500", None)], allow_debt=True)
                check("paying MORE than the total is still an error", False)
            except cash.PaymentError:
                check("paying MORE than the total is still an error", True)
            try:
                sales.create_sale(conn, anna, "offline", owner, [(cable, 1, 300)], location_id=1, payments=[(cash_uah, "100", None)])
                check("without «в долг» a short payment is refused as before", False)
            except cash.PaymentError:
                check("without «в долг» a short payment is refused as before", True)
            check("none of the refusals left anything behind",
                  inventory.product_total_qty(conn, cable, 1) == 8 and settlements.balance(conn, anna) == 0 and accounts.balance(conn, cash_uah) == 20600)
            part = sales.create_sale(conn, anna, "offline", owner, [(cable, 2, 300)], location_id=1, payments=[(cash_uah, "200", None)], allow_debt=True)
            check("part paid: 200 into the касса, 400 onto the client's balance («нам должны»)",
                  accounts.balance(conn, cash_uah) == 20800 and settlements.client_position(conn, anna)["they_owe"] == 400
                  and sales.sale_numbers(conn, part) == {"total": 600, "paid": 200, "debt": 400})
            nothing = sales.create_sale(conn, anna, "offline", owner, [(cable, 1, 300)], location_id=1, payments=[], allow_debt=True)
            check("nothing paid: the whole sale is debt, and it is marked so",
                  settlements.balance(conn, anna) == 700 and sales.get_sale(conn, nothing)["payment_method"] == "debt"
                  and accounts.balance(conn, cash_uah) == 20800)

            # ---- «Принять деньги» / «Выдать деньги»
            for call, label in (
                (lambda: settlements.receive_money(conn, anna, [], staff_id=owner, location_id=1), "«Принять деньги» with no amount is refused"),
                (lambda: settlements.receive_money(conn, anna, [(usd, "10", None)], staff_id=owner, location_id=1), "a foreign-currency part needs a rate"),
            ):
                try:
                    call()
                    check(label, False)
                except (settlements.SettlementError, cash.PaymentError):
                    check(label, True)
            entry = settlements.receive_money(conn, anna, [(cash_uah, "300", None), (usd, "5", "40")], staff_id=owner, location_id=1, comment="за кабели")
            doc_in = conn.execute("SELECT * FROM documents WHERE doc_type = 'cash_in' AND ref_id = ?", (entry,)).fetchone()
            check("«Принять деньги»: 300 грн + 5 $ × 40 = 500 off the debt, each onto its account, one ПКО",
                  settlements.balance(conn, anna) == 200 and accounts.balance(conn, cash_uah) == 21100 and accounts.balance(conn, usd) == 5
                  and documents.doc_label(doc_in) == "ПКО-001" and doc_in["amount"] == 500 and doc_in["client_id"] == anna)
            doc_cancel.cancel_document(conn, doc_in["id"], owner, "ошиблись клиентом")
            check("cancelling the ПКО puts both the money and the debt back",
                  settlements.balance(conn, anna) == 700 and accounts.balance(conn, cash_uah) == 20800 and accounts.balance(conn, usd) == 0)
            settlements.receive_money(conn, anna, [(cash_uah, "1000", None)], staff_id=owner, location_id=1)
            check("paying more than the debt makes it an аванс («мы должны» 300)",
                  settlements.client_position(conn, anna) == {"balance": -300, "they_owe": 0, "we_owe": 300})
            out = settlements.pay_out_money(conn, anna, [(cash_uah, "300", None)], staff_id=owner, location_id=1, comment="возврат аванса")
            doc_out = conn.execute("SELECT * FROM documents WHERE doc_type = 'cash_out' AND ref_table = 'client_ledger' AND ref_id = ?", (out,)).fetchone()
            check("«Выдать деньги» is an РКО that brings the balance back to zero",
                  settlements.balance(conn, anna) == 0 and accounts.balance(conn, cash_uah) == 21500 and doc_out is not None
                  and documents.doc_label(doc_out).startswith("РКО-"))
            doc_cancel.cancel_document(conn, doc_out["id"], owner, "не выдали")
            check("…and cancelling that РКО restores both", settlements.balance(conn, anna) == -300 and accounts.balance(conn, cash_uah) == 21800)

            # ---- сверка
            today = timefmt.kyiv_today()
            st = settlements.statement(conn, anna, today, today)
            check("«Сверка за период»: opening 0, every live row with a running balance, closing = the balance",
                  st["opening"]["balance"] == 0 and st["closing"]["balance"] == -300 and st["rows"][-1]["balance"] == -300
                  and st["charged"] == 600 + 600 + 300 and st["paid"] == 600 + 200 + 1000 and len(st["rows"]) == 6)
            check("cancelled rows are not in it", all(r["comment"] != "за кабели" for r in st["rows"]))
            later = settlements.statement(conn, anna, "2099-01-01", "2099-01-02")
            check("a later period opens with today's balance and has no rows", later["opening"]["balance"] == -300 and later["rows"] == [])
            check("the list of non-zero balances has her", [(d["id"], d["balance"]) for d in settlements.debtors(conn)] == [(anna, -300)])
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", nothing)["id"], owner, "вернул")
            check("cancelling a sale takes its charge off the balance too", settlements.balance(conn, anna) == -600)

            # ---- покупка у клиента: в сверке видна, баланс не меняет
            seller = buyback.create_purchase(conn, seller_phone="0671230002", model="iPhone 12", imei="357000000000001",
                                             comment=None, price="4000", staff_id=owner, location_id=1)
            seller_id = buyback.get_buyback_order(conn, seller)["client_id"]
            st2 = settlements.statement(conn, seller_id, today, today)
            check("a покупка shows in the seller's сверка as owed and paid, net zero",
                  settlements.balance(conn, seller_id) == 0 and (st2["charged"], st2["paid"]) == (4000, 4000))

            # ---- перенос старого: продажи и покупки до появления сверки
            old_client = clients.get_or_create_by_phone(conn, "Давний", "+380671230009", source="offline")
            case = inventory.create_product(conn, "Чехол", "ST-CASE", None, "шт", False, True, 0, 300)
            purchases.create_receipt(conn, None, None, owner, [(case, cell, 2, 50)], location_id=1)
            old_sale = sales.create_sale(conn, old_client, "offline", owner, [(case, 1, 300)], location_id=1)
            old_cancelled = sales.create_sale(conn, old_client, "offline", owner, [(case, 1, 300)], location_id=1)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", old_cancelled)["id"], owner, "ошибка")
            conn.execute("DELETE FROM client_ledger WHERE client_id IN (?, ?)", (old_client, seller_id))
            conn.execute("UPDATE sales_orders SET created_at = '2026-08-18 10:00:00' WHERE id = ?", (old_sale,))
            check("before the transfer those documents are not in the сверка at all",
                  settlements.statement(conn, old_client, "2026-08-01", today)["rows"] == [])
            settlements.backfill(conn)
            settlements.backfill(conn)
            old_statement = settlements.statement(conn, old_client, "2026-08-18", "2026-08-18")
            check("the old sale is now in the сверка on its own date — charged and paid, once, balance untouched",
                  (old_statement["charged"], old_statement["paid"], len(old_statement["rows"])) == (300, 300, 2)
                  and settlements.balance(conn, old_client) == 0 and old_statement["rows"][0]["source"] == f"ПД-{old_sale:03d}")
            check("a cancelled sale is not carried over",
                  conn.execute("SELECT COUNT(*) AS n FROM client_ledger WHERE ref_type = 'sales_order' AND ref_id = ?", (old_cancelled,)).fetchone()["n"] == 0)
            carried = settlements.statement(conn, seller_id, today, today)
            check("the old покупка is carried over too: we owed 4000 and paid 4000",
                  (carried["charged"], carried["paid"]) == (4000, 4000) and settlements.balance(conn, seller_id) == 0)
            check("a document that already has rows is left alone", settlements.balance(conn, anna) == -600
                  and len(settlements.statement(conn, anna, today, today)["rows"]) == 5)

            # ---- заказ: резерв 24 ч, «Оплатить» и «Выдать» отдельно
            boris = clients.get_or_create_by_phone(conn, "Борис", "+380671230003", source="offline")
            for items, needle, label in (
                ([], "хотя бы один", "an empty заказ is refused"),
                ([(cable, 99, 300)], "свободно только", "more than is free is refused"),
            ):
                try:
                    orders.create_order(conn, boris, items, owner, location_id=1)
                    check(label, False)
                except orders.OrderError as exc:
                    check(label, needle in str(exc))
            stock_before = inventory.product_total_qty(conn, cable, 1)
            order_id = orders.create_order(conn, boris, [(cable, 3, 300)], owner, location_id=1)
            order = orders.get_order(conn, order_id)
            check("the заказ holds its goods for 24 hours and has its own document",
                  order["status"] == "reserved" and order["total"] == 900
                  and documents.doc_label(documents.get_for(conn, "client_order", order_id)) == f"ЗК-{order_id:03d}"
                  and conn.execute("SELECT (julianday(?) - julianday('now')) * 24 AS h", (order["reserved_until"],)).fetchone()["h"] > 23.9)
            check("held goods are still on the shelf, but only the rest can be sold",
                  inventory.product_total_qty(conn, cable, 1) == stock_before == 6)
            try:
                sales.create_sale(conn, None, "offline", owner, [(cable, 4, 300)], location_id=1)
                check("a sale can't take what the заказ holds", False)
            except inventory.InsufficientStockError:
                check("a sale can't take what the заказ holds", True)
            try:
                orders.pay(conn, order_id, [(cash_uah, "1000", None)], owner)
                check("paying more than the заказ costs is refused", False)
            except orders.OrderError as exc:
                check("paying more than the заказ costs is refused", "осталось 900" in str(exc))
            orders.pay(conn, order_id, [(cash_uah, "400", None)], owner)
            check("«Оплатить»: 400 is an аванс on the client's balance, counted towards the заказ",
                  orders.numbers(conn, order_id) == {"total": 900, "paid": 400, "left": 500}
                  and settlements.client_position(conn, boris)["we_owe"] == 400 and inventory.product_total_qty(conn, cable, 1) == 6)
            try:
                orders.issue(conn, order_id, owner, payments=[(cash_uah, "600", None)])
                check("at «Выдать» the client can't pay more than is left", False)
            except orders.OrderError:
                check("at «Выдать» the client can't pay more than is left", True)
            sale_id = orders.issue(conn, order_id, owner, payments=[(cash_uah, "300", None)])
            order = orders.get_order(conn, order_id)
            check("«Выдать»: the заказ is now a sale of exactly what it held",
                  order["status"] == "issued" and order["sale_id"] == sale_id and inventory.product_total_qty(conn, cable, 1) == 3
                  and [(i["qty"], i["price"]) for i in sales.get_sale_items(conn, sale_id)] == [(3, 300)])
            check("900 charged − 400 prepaid − 300 at handover = 200 «нам должны»; nothing is held any more",
                  settlements.client_position(conn, boris)["they_owe"] == 200
                  and conn.execute("SELECT COUNT(*) AS n FROM stock_reservations WHERE ref_type = 'client_order' AND released_at IS NULL").fetchone()["n"] == 0)
            try:
                orders.issue(conn, order_id, owner)
                check("it can't be выдан twice", False)
            except orders.OrderError:
                check("it can't be выдан twice", True)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", sale_id)["id"], owner, "вернул всё")
            check("cancelling that sale returns the goods and the 300 taken at handover, closes the заказ; the 400 предоплата stays his аванс",
                  inventory.product_total_qty(conn, cable, 1) == 6 and orders.get_order(conn, order_id)["status"] == "cancelled"
                  and settlements.client_position(conn, boris)["we_owe"] == 400 and _stock_is_consistent(conn))

            # ---- серийный товар, автоснятие и продление
            unit = inventory.find_unit_by_imei(conn, "357000000000001")
            try:
                orders.create_order(conn, boris, [(unit["product_id"], 1, 6000)], owner, location_id=1)
                check("a serial product in a заказ must name its IMEI", False)
            except orders.OrderError as exc:
                check("a serial product in a заказ must name its IMEI", "IMEI" in str(exc))
            phone_order = orders.create_order(conn, boris, [(unit["product_id"], 1, 6000, unit["id"])], owner, location_id=1)
            try:
                sales.create_sale(conn, None, "offline", owner, [(unit["product_id"], 1, 6000, unit["id"])], location_id=1)
                check("a phone held for a заказ can't be sold to someone else", False)
            except inventory.InsufficientStockError:
                check("a phone held for a заказ can't be sold to someone else", True)
            orders.pay(conn, phone_order, [(cash_uah, "1000", None)], owner)

            def _age(order: int) -> None:
                conn.execute("UPDATE client_orders SET reserved_until = datetime('now', '-1 minute') WHERE id = ?", (order,))
                conn.execute("UPDATE stock_reservations SET expires_at = datetime('now', '-1 minute') WHERE ref_type = 'client_order' AND ref_id = ?", (order,))

            _age(phone_order)
            check("24 hours on, the hold no longer counts even before anyone looks",
                  inventory.available_qty(conn, unit["id"], unit["cell_id"]) == 1)
            check("«автоснятие»: the заказ becomes «Резерв истёк»",
                  orders.expire_due(conn) == [phone_order] and orders.get_order(conn, phone_order)["status"] == "expired" and orders.expire_due(conn) == [])
            try:
                orders.issue(conn, phone_order, owner)
                check("an expired заказ can't be выдан as is", False)
            except orders.OrderError as exc:
                check("an expired заказ can't be выдан as is", "Резерв истёк" in str(exc))
            orders.extend(conn, phone_order, owner)
            check("«Продлить резерв» brings it back for another 24 hours while the phone is still free",
                  orders.get_order(conn, phone_order)["status"] == "reserved" and inventory.available_qty(conn, unit["id"], unit["cell_id"]) == 0
                  and orders.numbers(conn, phone_order)["paid"] == 1000)
            _age(phone_order)
            orders.expire_due(conn)
            sales.create_sale(conn, None, "offline", owner, [(unit["product_id"], 1, 6500, unit["id"])], location_id=1)
            try:
                orders.extend(conn, phone_order, owner)
                check("once the phone is sold to someone else the заказ can't be revived", False)
            except orders.OrderError as exc:
                check("once the phone is sold to someone else the заказ can't be revived", "уже нет" in str(exc))
            doc_cancel.cancel_document(conn, documents.get_for(conn, "client_order", phone_order)["id"], owner, "товар ушёл")
            check("the заказ can be cancelled from the journal; its предоплата stays the client's аванс",
                  orders.get_order(conn, phone_order)["status"] == "cancelled" and documents.get_for(conn, "client_order", phone_order)["status"] == "cancelled"
                  and settlements.client_position(conn, boris)["we_owe"] == 1400)
            direct = orders.create_order(conn, boris, [(cable, 1, 300)], owner, location_id=1)
            orders.cancel(conn, direct, owner)
            check("«Отменить заказ» frees the goods and marks the document",
                  orders.get_order(conn, direct)["status"] == "cancelled" and documents.get_for(conn, "client_order", direct)["status"] == "cancelled"
                  and [o["id"] for o in orders.list_orders(conn, statuses=("reserved",))] == []
                  and len(orders.list_orders(conn, client_id=boris)) == 3)

            # ---- выплата мастеру
            masters.accrue(conn, master, "repair", "repair_order", 1, 1000, comment="РК-001")
            check("начислено 1000, выплачено 0 — мы должны 1000", masters.owed(conn, master) == 1000 and masters.paid_total(conn, master) == 0)
            for call, label in (
                (lambda: masters.pay_out(conn, master, [], paid_by=owner, location_id=1), "a payout with no amount is refused"),
                (lambda: masters.pay_out(conn, owner, [(cash_uah, "10", None)], paid_by=owner, location_id=1), "only a master can be paid out"),
            ):
                try:
                    call()
                    check(label, False)
                except masters.PayoutError:
                    check(label, True)
            before = accounts.balance(conn, cash_uah)
            payout = masters.pay_out(conn, master, [(cash_uah, "600", None)], paid_by=owner, location_id=1, comment="за неделю")
            payout_doc = conn.execute("SELECT * FROM documents WHERE ref_table = 'master_payouts' AND ref_id = ?", (payout,)).fetchone()
            check("«Выплата мастеру»: 600 leaves the касса as an РКО, we owe him 400",
                  masters.owed(conn, master) == 400 and masters.paid_total(conn, master) == 600 and accounts.balance(conn, cash_uah) == before - 600
                  and payout_doc["doc_type"] == "cash_out" and "Эдик" in payout_doc["title"]
                  and [p["amount"] for p in masters.list_payouts(conn, master)] == [600])
            doc_cancel.cancel_document(conn, payout_doc["id"], owner, "не выдали")
            check("cancelling it puts the money back and the debt to him back to 1000",
                  masters.owed(conn, master) == 1000 and accounts.balance(conn, cash_uah) == before)


def scenario_settlements_http() -> None:
    print("scenario: взаиморасчёты over HTTP — продажа в долг, заказ, карточка клиента, сверка, выплата мастеру")
    from core import orders, settlements

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "settle-http.sqlite3", ("Магазин",)) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "sh6-owner", "pass", "Владелец", "owner")
            seller = auth.create_staff(conn, "sh6-seller", "pass", "Продавец", "storekeeper", location_id=1)
            master = auth.create_master(conn, "Эдик", None, "percent", 33)
            cell = inventory.create_cell(conn, "S6-1", None, None, location_id=1)
            cable = inventory.create_product(conn, "Кабель", "S6-CBL", None, "шт", False, True, 0, 300)
            purchases.create_receipt(conn, None, None, owner, [(cable, cell, 10, 100)], location_id=1)
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
            cash_uah = acc[("cash", "UAH")]
            masters.accrue(conn, master, "repair", "repair_order", 1, 1000, comment="РК-001")
        token, seller_token = make_token(owner, "1"), make_token(seller, "1")

        import webapp.main

        def sale_form(**extra) -> dict:
            return {"row_count": "1", "product_id_0": str(cable), "product_name_0": "Кабель", "qty_0": "2", "price_0": "300",
                    "channel": "offline", **extra}

        with TestClient(webapp.main.app) as client:
            page = client.get(f"/sales?t={token}").text
            check("the sale form has «В долг», «Отложить на 24 часа» and the Продажи/Заказы tabs",
                  'name="allow_debt"' in page and "Отложить на 24 часа" in page and "/orders" in page)
            no_client = client.post(f"/sales?t={token}", data=sale_form(allow_debt="1", client_phone="+380"))
            check("«в долг» without a phone is a friendly error", no_client.status_code == 200 and "укажите его номер" in no_client.text)
            made = client.post(f"/sales?t={token}", data=sale_form(
                allow_debt="1", client_name="Анна", client_phone="0671230010", **{f"pay_{cash_uah}": "200"}), follow_redirects=False)
            sale_id = int(made.headers["location"].split("/sales/")[1].split("?")[0])
            with get_conn(db_path) as conn:
                anna = clients.get_by_phone(conn, "+380671230010")["id"]
                check("the sale went through with 200 paid and 400 on the client's balance",
                      settlements.client_position(conn, anna)["they_owe"] == 400 and accounts.balance(conn, cash_uah) == 20200)
            detail = client.get(f"/sales/{sale_id}?t={token}").text
            check("the sale's page says what was paid and what went into debt, and links to the client",
                  "Оплачено при продаже: 200 грн из 600" in detail and "400 грн" in detail and f"/clients/{anna}" in detail)

            # ---- карточка клиента
            card = client.get(f"/clients/{anna}?t={token}").text
            check("the client's card shows the balance, the документ with its profit, and the макет's actions",
                  "нам должны" in card and ">400 грн<" in card and f"ПД-{sale_id:03d}" in card and "Прибыль" in card
                  and all(x in card for x in ("Купить", "Продать", "Принять деньги", "Выдать деньги", "Сверка за период"))
                  and "/buyback?phone=%2B380671230010" in card and "/sales?phone=%2B380671230010" in card)
            seller_card = client.get(f"/clients/{anna}?t={seller_token}").text
            check("a non-owner sees the balance but neither profit nor «Выдать деньги»",
                  "нам должны" in seller_card and "прибыль по документам" not in seller_card and "/money-out" not in seller_card)
            check("«Продать» / «Купить» open their forms with the client filled in",
                  'name="client_phone" value="+380671230010"' in client.get(f"/sales?phone=%2B380671230010&name=Анна&t={token}").text
                  and 'name="seller_phone" value="+380671230010"' in client.get(f"/buyback?phone=%2B380671230010&t={token}").text)
            empty = client.post(f"/clients/{anna}/money-in?t={seller_token}", data={})
            check("«Принять деньги» with no amount is a friendly error on the card", empty.status_code == 200 and "Укажите сумму" in empty.text)
            idem = "abc123in"
            for _ in range(2):
                client.post(f"/clients/{anna}/money-in?t={seller_token}", data={f"pay_{cash_uah}": "1000", "comment": "за кабели", "idem": idem})
            with get_conn(db_path) as conn:
                check("«Принять деньги» posts once even when sent twice: debt gone, 600 аванс",
                      settlements.client_position(conn, anna)["we_owe"] == 600 and accounts.balance(conn, cash_uah) == 21200)
            denied = client.post(f"/clients/{anna}/money-out?t={seller_token}", data={f"pay_{cash_uah}": "600"})
            check("a seller can't hand money out", "владелец или админ" in denied.text)
            client.post(f"/clients/{anna}/money-out?t={token}", data={f"pay_{cash_uah}": "600", "comment": "возврат"})
            with get_conn(db_path) as conn:
                check("the owner can: balance back to zero", settlements.balance(conn, anna) == 0 and accounts.balance(conn, cash_uah) == 20600)
            statement = client.get(f"/clients/{anna}/statement?t={token}").text
            check("«Сверка за период» lists the sale, its payment, the ПКО and the РКО with turnovers",
                  all(x in statement for x in (f"ПД-{sale_id:03d}", "Приём денег", "Выдача денег", "Обороты за период", "На конец периода"))
                  and ">1200<" in statement)
            check("a broken date falls back to this month", client.get(f"/clients/{anna}/statement?date_from=zzz&t={token}").status_code == 200)
            journal = client.get(f"/journal?t={token}").text
            check("the journal has the ПКО and the РКО", "ПКО-001" in journal and "Приём денег" in journal)

            # ---- заказ
            no_phone = client.post(f"/orders?t={token}", data=sale_form(client_phone="+380"))
            check("a заказ without a phone is a friendly error on the sale form", "укажите его номер" in no_phone.text)
            too_many = client.post(f"/orders?t={token}", data={**sale_form(client_phone="0671230010"), "qty_0": "99"})
            check("a заказ for more than is free is a friendly error", "свободно только" in too_many.text)
            held = client.post(f"/orders?t={seller_token}", data=sale_form(client_name="Анна", client_phone="0671230010", idem="ord1"), follow_redirects=False)
            order_id = int(held.headers["location"].split("/orders/")[1].split("?")[0])
            again = client.post(f"/orders?t={seller_token}", data=sale_form(client_name="Анна", client_phone="0671230010", idem="ord1"), follow_redirects=False)
            check("a repeated submit lands on the same заказ", again.headers["location"].split("?")[0] == f"/orders/{order_id}")
            opage = client.get(f"/orders/{order_id}?t={seller_token}").text
            check("the заказ's page: «В резерве до …», the numbers, «Оплатить», «Выдать», «Продлить», «Отменить»",
                  "В резерве" in opage and all(x in opage for x in (f"/orders/{order_id}/pay", f"/orders/{order_id}/issue",
                                                                  f"/orders/{order_id}/extend", f"/orders/{order_id}/cancel")))
            check("it is on the «Заказы» list and on the client's card",
                  f"/orders/{order_id}" in client.get(f"/orders?t={token}").text and f"/orders/{order_id}" in client.get(f"/clients/{anna}?t={token}").text)
            over = client.post(f"/orders/{order_id}/pay?t={seller_token}", data={f"pay_{cash_uah}": "999"})
            check("overpaying a заказ is a friendly error", "осталось 600" in over.text)
            client.post(f"/orders/{order_id}/pay?t={seller_token}", data={f"pay_{cash_uah}": "250", "idem": "p1"})
            with get_conn(db_path) as conn:
                check("«Оплатить» 250: counted towards the заказ", orders.numbers(conn, order_id) == {"total": 600, "paid": 250, "left": 350})
                conn.execute("UPDATE client_orders SET reserved_until = datetime('now', '-1 minute') WHERE id = ?", (order_id,))
                conn.execute("UPDATE stock_reservations SET expires_at = datetime('now', '-1 minute') WHERE ref_type = 'client_order' AND ref_id = ?", (order_id,))
            expired = client.get(f"/orders/{order_id}?t={seller_token}").text
            check("24 hours later the page shows «Резерв истёк» and offers only to renew or cancel",
                  "Резерв истёк" in expired and f"/orders/{order_id}/issue" not in expired and "Возобновить резерв" in expired)
            client.post(f"/orders/{order_id}/extend?t={seller_token}")
            issued = client.post(f"/orders/{order_id}/issue?t={seller_token}", data={f"pay_{cash_uah}": "350", "idem": "i1"}, follow_redirects=False)
            check("renewed and «Выдать» with the rest paid → the sale's page", "/sales/" in issued.headers["location"])
            with get_conn(db_path) as conn:
                check("the goods left, the заказ is выдан, the client owes nothing",
                      inventory.product_total_qty(conn, cable, 1) == 6 and orders.get_order(conn, order_id)["status"] == "issued"
                      and settlements.balance(conn, anna) == 0)
            check("an unknown заказ goes back to the list", client.get(f"/orders/9999?t={token}", follow_redirects=False).status_code == 303)

            # ---- мастер
            mpage = client.get(f"/masters/{master}?t={token}").text
            check("the master's card: начислено 1000 / выплачено 0 / мы должны 1000 and «Выплатить мастеру»",
                  "выплачено" in mpage and "мы должны" in mpage and f"/masters/{master}/payout" in mpage)
            bad = client.post(f"/masters/{master}/payout?t={token}", data={})
            check("a payout with no amount is a friendly error", bad.status_code == 200 and "Укажите сумму выплаты" in bad.text)
            client.post(f"/masters/{master}/payout?t={token}", data={f"pay_{cash_uah}": "600", "comment": "за неделю", "idem": "mp1"})
            client.post(f"/masters/{master}/payout?t={token}", data={f"pay_{cash_uah}": "600", "comment": "за неделю", "idem": "mp1"})
            with get_conn(db_path) as conn:
                check("the payout posts once: we owe him 400", masters.owed(conn, master) == 400)
            check("and it is listed on his card", "за неделю" in client.get(f"/masters/{master}?t={token}").text)


def scenario_sale_chat() -> None:
    """«🛍 Продажа» in the bot: товар → цена → телефон → оплата → карточка."""
    print("scenario: продажа в боте — IMEI/название, клиент обязателен, оплата одним тапом или в долг")
    import asyncio
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from bot import quick_actions as qa
    from bot import sale_flow as sf
    from core import channel_posts, notify, settlements

    OWNER_TG, GROUP = 884001, -100884

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "sale-chat.sqlite3", ("Магазин",)) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ? WHERE id = 1", (GROUP,))
            owner = auth.create_staff(conn, "sc-owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "sc-owner", OWNER_TG)
            cell = inventory.create_cell(conn, "SL-1", None, None, location_id=1)
            cable = inventory.create_product(conn, "Кабель Lightning", "SL-CBL", None, "шт", False, True, 0, 300)
            purchases.create_receipt(conn, None, None, owner, [(cable, cell, 5, 100)], location_id=1)
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            buyback.create_purchase(conn, seller_phone="0671110001", model="iPhone 12", imei="358000000000001",
                                    comment=None, price="4000", staff_id=owner, location_id=1)
            buyback.create_purchase(conn, seller_phone="0671110001", model="iPhone 12", imei="358000000000002",
                                    comment=None, price="4200", staff_id=owner, location_id=1)
            clients.get_or_create_by_phone(conn, "Постоянный", "+380671110077", source="offline")
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
            cash_uah = acc[("cash", "UAH")]
            shifts.open_shift(conn, owner, 1)
            till = accounts.balance(conn, cash_uah)

        class _Chat:
            def __init__(self):
                self.log, self._next = [], 900

            def add(self, author, text, markup=None):
                self._next += 1
                self.log.append({"id": self._next, "author": author, "text": text, "markup": markup})
                return self._next

            def delete(self, message_id):
                self.log = [m for m in self.log if m["id"] != message_id]

        class _Bot:
            def __init__(self, chat):
                self.chat = chat

            async def delete_message(self, chat_id, message_id):
                self.chat.delete(message_id)

            async def delete_messages(self, chat_id, message_ids):
                for mid in message_ids:
                    self.chat.delete(mid)

            async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
                for m in self.chat.log:
                    if m["id"] == message_id:
                        m["text"], m["markup"] = text, reply_markup

        class _Msg:
            def __init__(self, chat, bot, text):
                self._log, self.bot, self.text, self.photo = chat, bot, text, None
                self.chat = SimpleNamespace(id=OWNER_TG, type="private")
                self.from_user = SimpleNamespace(id=OWNER_TG, full_name="Владелец")
                self.message_id = chat.add("staff", text)

            async def answer(self, text, reply_markup=None):
                return SimpleNamespace(message_id=self._log.add("bot", text, reply_markup))

        class _Cb:
            def __init__(self, chat, bot, data, message_id):
                self.data, self.bot = data, bot
                self.from_user = SimpleNamespace(id=OWNER_TG)
                self.answered = []
                log = chat

                class _M:
                    chat = SimpleNamespace(id=OWNER_TG, type="private")

                    async def answer(self_inner, text, reply_markup=None):
                        return SimpleNamespace(message_id=log.add("bot", text, reply_markup))

                self.message = _M()
                self.message.message_id = message_id

            async def answer(self, text=None, show_alert=False):
                self.answered.append((text, show_alert))

        def _buttons(markup) -> list[str]:
            return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data] if markup else []

        posted: list[dict] = []
        orig_notify, orig_sync = notify.notify_staff_group, channel_posts.sync_products
        notify.notify_staff_group = lambda text, **kw: posted.append({"text": text, **kw})
        channel_posts.sync_products = lambda *a, **kw: None

        async def run() -> None:
            chat = _Chat()
            bot = _Bot(chat)
            state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=OWNER_TG, user_id=OWNER_TG))
            say = lambda text: _Msg(chat, bot, text)
            screen = lambda: [m for m in chat.log if m["author"] == "bot"][-1]
            tap = lambda data: _Cb(chat, bot, data, screen()["id"])

            check("«Продажа» is on the keyboard", any(b.text == qa.BTN_SALE for row in qa.QUICK_ACTIONS_KEYBOARD.keyboard for b in row))

            # ---- телефон по IMEI, знакомый клиент, наличные
            await sf.sale_start(say(qa.BTN_SALE), state)
            check("step 1 asks what is being sold", "шаг 1 из" in screen()["text"] and "IMEI или часть названия" in screen()["text"])
            await sf.sale_got_item(say("нет такого"), state)
            check("nothing found is a nudge that keeps the question", "ничего не нашлось" in screen()["text"] and "IMEI или часть названия" in screen()["text"])
            await sf.sale_got_item(say("iphone"), state)
            check("two matching units → a pick list, one button per IMEI",
                  len([b for b in _buttons(screen()["markup"]) if b.startswith("sl_pick:u:")]) == 2)
            await sf.sale_got_item(say("358000000000001"), state)
            check("an exact IMEI goes straight to the price", "Цена продажи" in screen()["text"] and "IMEI 358000000000001" in screen()["text"])
            await sf.sale_got_price(say("дорого"), state)
            check("a price that isn't a number is a nudge", "Введите цену числом" in screen()["text"])
            await sf.sale_got_price(say("6500"), state)
            check("the priced item lands «В чеке» with a total and the choice: ещё товар or дальше",
                  "В чеке" in screen()["text"] and "Итого: <b>6500 грн</b>" in screen()["text"]
                  and _buttons(screen()["markup"]) == ["sl_more", "sl_next", "sl_cancel"])
            await sf.sale_next(tap("sl_next"), state)
            check("then the client's phone — mandatory", "Номер телефона клиента" in screen()["text"] and "Обязательно" in screen()["text"])
            await sf.sale_got_phone(say("абв"), state)
            check("not a phone number is a nudge", "Не похоже на номер" in screen()["text"])
            await sf.sale_got_phone(say("067 111 00 77"), state)
            pay = screen()
            check("a known client's name isn't asked: straight to «куда оплата» with the точка's гривневые счета, «В долг» and a link to the full form",
                  "Куда оплата — 6500 грн" in pay["text"] and "Постоянный" in pay["text"] and f"sl_pay:{cash_uah}" in _buttons(pay["markup"])
                  and "sl_pay:debt" in _buttons(pay["markup"]) and f"sl_pay:{acc[('cash', 'USD')]}" not in _buttons(pay["markup"])
                  and any(b.web_app and "/sales?phone=" in b.web_app.url for row in pay["markup"].inline_keyboard for b in row))
            await sf.sale_pick_pay(tap(f"sl_pay:{cash_uah}"), state)
            check("the confirm card sums it up", all(x in screen()["text"] for x in ("Проверьте и продайте", "IMEI 358000000000001", "6500 грн", "+380671110077"))
                  and _buttons(screen()["markup"]) == ["sl_confirm", "sl_cancel"])
            confirm = tap("sl_confirm")
            await sf.sale_confirm(confirm, state)
            await asyncio.sleep(0.1)
            with get_conn(db_path) as conn:
                sale = sales.list_sales(conn)[0]
                check("the sale is made: that unit is gone, 6500 in the касса, the client on it, profit 2500 on the document",
                      inventory.find_unit_by_imei(conn, "358000000000001") is None and accounts.balance(conn, cash_uah) == till + 6500
                      and sale["client_name"] == "Постоянный" and sale["total"] == 6500
                      and documents.get_for(conn, "sale", sale["id"])["profit"] == 2500)
                first_sale = sale["id"]
            check("the chat keeps one card with the document's number", f"ПД-{first_sale:03d}" in chat.log[-1]["text"] and "Продано" in chat.log[-1]["text"]
                  and len([m for m in chat.log if m["author"] == "staff"]) == 0)
            check("the same card went to the staff group", len(posted) == 1 and f"ПД-{first_sale:03d}" in posted[0]["text"]
                  and posted[0]["staff_group_chat_id"] == GROUP)
            again = _Cb(chat, bot, "sl_confirm", confirm.message.message_id)
            await state.set_state(qa.SaleFlow.confirm)
            await state.update_data(store_id="1")
            await sf.sale_confirm(again, state)
            with get_conn(db_path) as conn:
                check("a second tap on the same card doesn't sell twice", len(sales.list_sales(conn)) == 1 and "Уже проведено" in again.answered[-1][0])
            await state.clear()

            # ---- товар по названию, новый клиент, в долг
            await sf.sale_start(say(qa.BTN_SALE), state)
            await sf.sale_got_item(say("кабель"), state)
            check("a non-serial product is one option with its free quantity and the card price on a button",
                  "Кабель Lightning" in screen()["text"] and _buttons(screen()["markup"]) == ["sl_price_default"])
            await sf.sale_price_default(tap("sl_price_default"), state)
            await sf.sale_next(tap("sl_next"), state)
            await sf.sale_got_phone(say("0671110088"), state)
            check("an unknown number → the name is asked", "Как его зовут" in screen()["text"] and "шаг 4 из 5" in screen()["text"])
            await sf.sale_got_name(say("Новый Клиент"), state)
            await sf.sale_pick_pay(tap("sl_pay:debt"), state)
            check("«В долг» is spelled out on the confirm card", "в долг — на баланс клиента" in screen()["text"])
            await sf.sale_confirm(tap("sl_confirm"), state)
            await asyncio.sleep(0.1)
            with get_conn(db_path) as conn:
                newcomer = clients.get_by_phone(conn, "+380671110088")
                check("the client is created, the cable is sold, nothing went into the касса, he owes 300",
                      newcomer["name"] == "Новый Клиент" and inventory.product_total_qty(conn, cable, 1) == 4
                      and accounts.balance(conn, cash_uah) == till + 6500 and settlements.client_position(conn, newcomer["id"])["they_owe"] == 300)
                text, keyboard = await qa._client_card(stores.get_store("1"), newcomer["id"], owner)
            check("the bot's client card shows the debt and links to the card and the сверка",
                  "Нам должны: 300 грн" in text and len(keyboard.inline_keyboard) == 2 and "/statement" in keyboard.inline_keyboard[1][0].web_app.url)

            await qa.summary_view(say(qa.BTN_SUMMARY))
            summary = chat.log[-1]["text"]
            check("«Сводка» gives the owner today's net profit (2500 + 200) and the debts",
                  "Чистая прибыль сегодня: 2700 грн" in summary and "Нам должны: 300 грн" in summary)

            # ---- старая клавиатура: кнопка с прежней подписью не должна молчать
            from bot import fallback
            await fallback.unknown_text(say("🔧 Ремонт"))
            check("a tap on a button of an old keyboard gets the current keyboard instead of silence",
                  "Кнопки меню обновились" in chat.log[-1]["text"] and chat.log[-1]["markup"] is qa.QUICK_ACTIONS_KEYBOARD)
            stranger = say("привет")
            stranger.from_user = SimpleNamespace(id=555000111, full_name="Кто-то")
            before = len(chat.log)
            await fallback.unknown_text(stranger)
            check("someone who isn't staff gets nothing from it", len(chat.log) == before)

            # ---- несколько товаров в одном чеке
            await sf.sale_start(say(qa.BTN_SALE), state)
            await sf.sale_got_item(say("кабель"), state)
            await sf.sale_price_default(tap("sl_price_default"), state)
            await sf.sale_more(tap("sl_more"), state)
            check("«➕ Ещё товар» asks for the next one and keeps the чек in view",
                  "Ещё товар" in screen()["text"] and "В чеке: Кабель Lightning — 300 грн" in screen()["text"])
            await sf.sale_got_item(say("кабель"), state)
            await sf.sale_price_default(tap("sl_price_default"), state)
            check("the same product at the same price is one line × 2", "Кабель Lightning × 2 — 600 грн" in screen()["text"])
            await sf.sale_more(tap("sl_more"), state)
            await sf.sale_got_item(say("358000000000002"), state)
            await sf.sale_got_price(say("7000"), state)
            check("a phone joins the same чек: total 7600", "Итого: <b>7600 грн</b>" in screen()["text"] and "IMEI 358000000000002" in screen()["text"])
            await sf.sale_more(tap("sl_more"), state)
            await sf.sale_got_item(say("358000000000002"), state)
            check("the same phone can't be added twice", "уже в чеке" in screen()["text"])
            await sf.sale_got_item(say("кабель"), state)
            await sf.sale_price_default(tap("sl_price_default"), state)
            await sf.sale_more(tap("sl_more"), state)
            await sf.sale_got_item(say("кабель"), state)
            await sf.sale_price_default(tap("sl_price_default"), state)
            await sf.sale_more(tap("sl_more"), state)
            await sf.sale_got_item(say("кабель"), state)
            check("…and no more of a product than is free (4 left on the shelf)", "уже в чеке" in screen()["text"])
            with get_conn(db_path) as conn:
                conn.execute("UPDATE stock SET qty = qty WHERE 1 = 0")  # nothing sold yet — the чек is only a draft
                check("nothing has left the shelf while the чек is being put together",
                      inventory.product_total_qty(conn, cable, 1) == 4 and inventory.find_unit_by_imei(conn, "358000000000002") is not None)
            # back out of «ещё товар» the only way there is: finish the чек from a fresh pick
            await sf.sale_got_item(say("нет такого"), state)
            await state.set_state(qa.SaleFlow.cart)
            await sf.sale_next(tap("sl_next"), state)
            await sf.sale_got_phone(say("0671110077"), state)
            check("the payment step asks for the whole total", "Куда оплата — 8200 грн" in screen()["text"])
            await sf.sale_pick_pay(tap(f"sl_pay:{cash_uah}"), state)
            check("the confirm card lists every line", "Кабель Lightning × 4 — 1200 грн" in screen()["text"] and "Итого: <b>8200 грн</b>" in screen()["text"])
            posted.clear()
            await sf.sale_confirm(tap("sl_confirm"), state)
            await asyncio.sleep(0.1)
            with get_conn(db_path) as conn:
                multi = sales.list_sales(conn)[0]
                check("one sale, two lines: 4 cables and the phone, 8200 into the касса",
                      sorted((i["qty"], i["price"]) for i in sales.get_sale_items(conn, multi["id"])) == [(1, 7000), (4, 300)]
                      and inventory.product_total_qty(conn, cable, 1) == 0 and inventory.find_unit_by_imei(conn, "358000000000002") is None
                      and accounts.balance(conn, cash_uah) == till + 6500 + 8200)
            check("the group card lists both lines", len(posted) == 1 and "Кабель Lightning × 4" in posted[0]["text"] and "Итого: 8200 грн" in posted[0]["text"])

            # ---- отмена с карточки
            with get_conn(db_path) as conn:
                buyback.create_purchase(conn, seller_phone="0671110001", model="iPhone 12", imei="358000000000003",
                                        comment=None, price="4000", staff_id=owner, location_id=1)
            await sf.sale_start(say(qa.BTN_SALE), state)
            await sf.sale_got_item(say("358000000000003"), state)
            await sf.sale_got_price(say("7000"), state)
            await sf.sale_next(tap("sl_next"), state)
            await sf.sale_got_phone(say("0671110077"), state)
            await qa.quick_cancel_callback(tap("sl_cancel"), state)
            with get_conn(db_path) as conn:
                check("❌ Отмена on the payment step sells nothing and leaves the chat clean",
                      inventory.find_unit_by_imei(conn, "358000000000003") is not None and await state.get_state() is None
                      and not [m for m in chat.log if "Куда оплата" in (m["text"] or "")])

        try:
            os.environ["CRM_MINIAPP_URL"] = os.environ.get("CRM_MINIAPP_URL") or "https://crm.example/miniapp"
            asyncio.run(run())
        finally:
            notify.notify_staff_group, channel_posts.sync_products = orig_notify, orig_sync


def scenario_overview() -> None:
    """Заход 7: чистая прибыль, «Проблемы», Главная с фильтрами, помощник,
    разбор накладной через Claude."""
    print("scenario: главная, чистая прибыль, «Проблемы», помощник")
    from datetime import datetime

    from core import assistant, notify, orders, overview, production, settlements, stock_transfers, vision_ocr, warehouses

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "overview.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = -100771 WHERE id = 1")
            owner = auth.create_staff(conn, "ov-owner", "pass", "Владелец", "owner")
            keeper = auth.create_staff(conn, "ov-keeper", "pass", "Кладовщик", "storekeeper", location_id=1)
            master = auth.create_master(conn, "Эдик", None, "percent", 50)
            c1 = inventory.create_cell(conn, "OV-1", None, None, location_id=1)
            c2 = inventory.create_cell(conn, "OV-2", None, None, location_id=2)
            cable = inventory.create_product(conn, "Кабель", "OV-CBL", None, "шт", False, True, 0, 300)
            display = inventory.create_product(conn, "Дисплей", "OV-DSP", None, "шт", True, True, 0, 3000)
            purchases.create_receipt(conn, None, None, owner, [(cable, c1, 10, 100), (display, c1, 3, 1000)], location_id=1)
            purchases.create_receipt(conn, None, None, owner, [(cable, c2, 4, 100)], location_id=2)
            cash.record_adjustment(conn, 50000, "старт", owner, location_id=1)
            cash.record_adjustment(conn, 10000, "старт", owner, location_id=2)
            today = timefmt.kyiv_today()
            start, end = timefmt.kyiv_date_range_utc(today, today)
            empty = overview.profit(conn, start, end)
            check("with nothing sold the profit is zero across the board", empty["net"] == 0 and empty["gross"] == 0 and empty["sales_count"] == 0)
            check("and there are no problems", overview.problems(conn) == [])

            # ---- прибыль: продажи + ремонты − расходы − списания
            anna = clients.get_or_create_by_phone(conn, "Анна", "+380671239001", source="offline")
            sales.create_sale(conn, anna, "offline", owner, [(cable, 2, 300)], location_id=1)                       # +400
            sales.create_sale(conn, None, "offline", owner, [(cable, 1, 300)], location_id=2)                       # +200 (точка 2)
            cancelled = sales.create_sale(conn, None, "offline", owner, [(cable, 1, 999)], location_id=1)
            doc_cancel.cancel_document(conn, documents.get_for(conn, "sale", cancelled)["id"], owner, "ошибка")
            point_1, master_wh = warehouses.point_warehouse(conn, 1)["id"], warehouses.master_warehouse(conn, master)["id"]
            batch = conn.execute("SELECT id FROM batches WHERE product_id = ?", (display,)).fetchone()["id"]
            sent = stock_transfers.send(conn, point_1, master_wh, [(batch, c1, 1)], owner)
            stock_transfers.receive(conn, sent, master)
            repair_id = repairs.create_repair(conn, anna, "Смартфон", None, "iPhone 13", None, "Дисплей", "offline", master, 3000, owner, location_id=1)
            repairs.claim_repair(conn, repair_id, master)
            line = repairs.available_parts(conn, repair_id)[0]
            repairs.use_part(conn, repair_id, line["batch_id"], line["cell_id"], 1, master)
            repairs.set_price(conn, repair_id, 3000, 3000)
            repairs.complete_repair(conn, repair_id, master)
            repairs.update_status(conn, repair_id, "issued", owner)                                                 # 3000 − 1000 = 2000 → 50% → +1000
            cash.record_expense(conn, "cash", 700, "rent", "аренда", owner, location_id=1)
            cash.record_expense(conn, "cash", 300, "other", "вода", owner, location_id=1)
            cash.record_expense(conn, "cash", 5000, "supplies", "закупка", owner, location_id=1)
            inventory.write_off_stock(conn, cable, c1, 2, owner, comment="брак")                                    # −200
            masters.pay_out(conn, master, [(next(a["id"] for a in accounts.list_accounts(conn, 1) if a["kind"] == "cash" and a["currency"] == "UAH"), "400", None)],
                            paid_by=owner, location_id=1)
            numbers = overview.profit(conn, start, end, 1)
            check("точка 1: продажи +400, ремонт +1000 фирме, расходы 1000 (аренда + прочее), списание 200 → чистая 200",
                  (numbers["sales_profit"], numbers["repairs_profit"], numbers["gross"], numbers["expenses_total"], numbers["writeoffs"], numbers["net"])
                  == (400, 1000, 1400, 1000, 200, 200))
            check("a cancelled sale counts nowhere; revenue is what was really sold and выдано",
                  numbers["sales_count"] == 1 and numbers["sales_revenue"] == 600 and numbers["repairs_revenue"] == 3000)
            check("закупка товара and выплата мастеру are not expenses", numbers["expenses"] == {"rent": 700, "salary": 0, "other": 300})
            conn.execute("UPDATE documents SET profit = NULL WHERE doc_type = 'repair' AND ref_id = ?", (repair_id,))
            check("a repair выданный before profit was kept on the document still counts — worked out on the fly",
                  overview.profit(conn, start, end, 1)["repairs_profit"] == 1000)
            documents.set_profit(conn, "repair", repair_id, 1000)
            whole = overview.profit(conn, start, end)
            check("the whole business adds точка 2's sale: чистая 400", whole["net"] == 400 and whole["sales_count"] == 2)
            check("another period is empty", overview.profit(conn, *timefmt.kyiv_date_range_utc("2020-01-01", "2020-01-31"))["net"] == 0)
            act = overview.activity(conn, start, end, 1)
            check("the period's counters: 1 ремонт принят и выдан, 1 продажа",
                  (act["repairs_accepted"], act["repairs_issued"], act["sales"], act["orders_new"]) == (1, 1, 1, 0))

            # ---- сейчас: деньги, товар, долги
            sales.create_sale(conn, anna, "offline", owner, [(cable, 1, 300)], location_id=1, payments=[], allow_debt=True)
            boris = clients.get_or_create_by_phone(conn, "Борис", "+380671239002", source="offline")
            settlements.receive_money(conn, boris, [(next(a["id"] for a in accounts.list_accounts(conn, 1) if a["kind"] == "cash" and a["currency"] == "UAH"), "150", None)],
                                      staff_id=owner, location_id=1)
            stand = overview.standing(conn, 1)
            check("«нам должны» 300 (Анна), «мы должны» 150 (аванс Бориса), мастеру 1000 − 400 = 600",
                  (stand["they_owe"], stand["we_owe"], stand["masters_owed"]) == (300, 150, 600)
                  and [d["name"] for d in stand["debtors"]] == ["Анна"] and [d["name"] for d in stand["creditors"]] == ["Борис"]
                  and stand["masters"] == [{"id": master, "name": "Эдик", "owed": 600}])
            check("«в товаре» is this точка's stock at cost", stand["stock_value"] == inventory.stock_value(conn, 1))

            # ---- «Проблемы»
            stuck = repairs.create_repair(conn, anna, "Смартфон", None, "A54", None, "Чистка", "offline", master, 500, owner, location_id=1)
            repairs.claim_repair(conn, stuck, master)
            check("a repair just taken into work is not a problem yet", overview.problems(conn, 1) == [])
            conn.execute("UPDATE repair_orders SET started_at = datetime('now', '-3 hours') WHERE id = ?", (stuck,))
            old_ready = repairs.create_repair(conn, anna, "Смартфон", None, "A12", None, "Стекло", "offline", master, 400, owner, location_id=1)
            repairs.claim_repair(conn, old_ready, master)
            repairs.declare_no_parts(conn, old_ready)
            repairs.complete_repair(conn, old_ready, master)
            conn.execute("UPDATE repair_orders SET completed_at = datetime('now', '-8 days') WHERE id = ?", (old_ready,))
            cable_batch = conn.execute("SELECT batch_id FROM batch_stock WHERE cell_id = ? AND qty > 0", (c1,)).fetchone()["batch_id"]
            late = stock_transfers.send(conn, point_1, warehouses.point_warehouse(conn, 2)["id"], [(cable_batch, c1, 1)], owner)
            conn.execute("UPDATE stock_transfers SET sent_at = datetime('now', '-30 hours') WHERE id = ?", (late,))
            order_id = orders.create_order(conn, anna, [(cable, 1, 300)], owner, location_id=1)
            conn.execute("UPDATE client_orders SET reserved_until = datetime('now', '-1 minute') WHERE id = ?", (order_id,))
            texts_1 = {p["text"]: p["count"] for p in overview.problems(conn, 1)}
            texts_2 = {p["text"]: p["count"] for p in overview.problems(conn, 2)}
            check("точка 1: ремонт без запчасти, готовый ремонт не забирают, заказ с истёкшим резервом",
                  texts_1 == {"ремонтов в работе без указанной запчасти": 1, "готовых ремонтов не забирают больше 7 дней": 1,
                              "заказов с истёкшим резервом — продлить или отменить": 1})
            check("the transfer nobody received is the RECEIVING точка's problem",
                  texts_2 == {"перемещений товара в пути дольше 24 ч — не приняты": 1})
            inventory.create_product(conn, "Стекло", "OV-GLS", None, "шт", False, True, 2, 100)
            check("a product below a minimum somebody set is a problem; «0 при минимуме 0» is not",
                  {p["text"]: p["count"] for p in overview.problems(conn, 2)}.get("товаров с остатком ниже минимума") == 1)
            conn.execute("UPDATE products SET min_qty = 0 WHERE sku = 'OV-GLS'")
            check("every problem says where to go", all(p["path"].startswith("/") for p in overview.problems(conn)))
            check("the whole business sees all four", len(overview.problems(conn)) == 4)

            # ---- помощник
            location = locations.get_location(conn, 1)
            text = assistant.digest_text(conn, location)
            check("the digest names the точка and lists its problems", "Помощник · Мастерская" in text and "• 1 — ремонтов в работе без указанной запчасти" in text)
            check("a точка with nothing waiting has no digest", assistant.digest_text(conn, {"id": 99, "name": "Пусто"}) is None)

        sent: list[dict] = []
        orig_notify = notify.notify_staff_group
        notify.notify_staff_group = lambda text, **kw: (sent.append({"text": text, **kw}) or 555)
        noon = datetime(2026, 10, 6, 12, 0, tzinfo=timefmt.KYIV)
        try:
            check("off by default: nothing is posted", assistant.run_once(db_path, noon) == 0 and sent == [])
            with get_conn(db_path) as conn:
                store_settings.set_assistant_digest(conn, True, location_id=1)
                store_settings.set_assistant_digest(conn, True, location_id=2)
            check("before 10:00 it keeps quiet", assistant.run_once(db_path, noon.replace(hour=9)) == 0 and sent == [])
            check("after 10:00 the точка with a group gets its digest — once (точка 2 has no group)",
                  assistant.run_once(db_path, noon) == 1 and len(sent) == 1 and sent[0]["staff_group_chat_id"] == -100771
                  and assistant.run_once(db_path, noon.replace(hour=15)) == 0 and len(sent) == 1)
            check("the next day it goes out again", assistant.run_once(db_path, noon.replace(day=7)) == 1)
            notify.notify_staff_group = lambda text, **kw: None
            check("if Telegram didn't take it, the day isn't marked — it will retry",
                  assistant.run_once(db_path, noon.replace(day=8)) == 0)
            notify.notify_staff_group = lambda text, **kw: (sent.append({"text": text, **kw}) or 555)
            check("…and does", assistant.run_once(db_path, noon.replace(day=8)) == 1)
        finally:
            notify.notify_staff_group = orig_notify

        # ---- Главная и «Чистая прибыль» over HTTP
        token, keeper_token = make_token(owner, "1"), make_token(keeper, "1")
        import webapp.main

        with TestClient(webapp.main.app) as client:
            home = client.get(f"/?t={token}").text
            check("the owner's Главная: фильтры периода и точки, деньги, «Проблемы», разделы, долги, журнал",
                  all(x in home for x in ("Сегодня", "7 дней", "Месяц", "Все точки", "Магазин", "чистая прибыль", "нам должны",
                                          "Проблемы", "ремонтов в работе без указанной запчасти", "Разделы", "Сервисный центр",
                                          "Чистая прибыль", "Долги", "Анна", "Борис", "Эдик", "Последние документы")))
            check("problems link to where they are fixed", "/orders?" in home and "/repairs?" in home)
            all_points = client.get(f"/?period=month&point=all&t={token}").text
            check("«Все точки» adds the other точка's problem", "перемещений товара в пути" in all_points and "перемещений товара в пути" not in home)
            keeper_home = client.get(f"/?point=all&t={keeper_token}").text
            check("a non-owner sees problems and sections of their точка, but no money, no долги and no точка switch",
                  "ремонтов в работе без указанной запчасти" in keeper_home and "чистая прибыль" not in keeper_home
                  and "Долги" not in keeper_home and "Все точки" not in keeper_home and "перемещений товара в пути" not in keeper_home
                  and "/profit" not in keeper_home)
            check("garbage in the filters falls back to defaults", client.get(f"/?period=zzz&point=zzz&t={token}").status_code == 200)
            profit_page = client.get(f"/profit?period=day&point=1&t={token}").text
            check("«Чистая прибыль» shows the breakdown for the точка",
                  all(x in profit_page for x in ("Прибыль продаж", "Прибыль ремонтов", "Валовая прибыль", "Аренда", "Списания товара", "Сейчас в бизнесе"))
                  and "+1000" in profit_page and "−700" in profit_page)
            check("a custom date range works and an empty one is all zeros",
                  ">0 грн<" in client.get(f"/profit?date_from=2020-01-01&date_to=2020-01-31&t={token}").text.replace("\n", "").replace("  ", ""))
            check("«Чистая прибыль» is owner/admin only", client.get(f"/profit?t={keeper_token}", follow_redirects=False).status_code in (303, 403))
            more = client.get(f"/more?t={token}").text
            check("«Ещё» has «Чистая прибыль» and «Заказы»", "/profit" in more and "/orders" in more)
            settings_page = client.get(f"/store/settings?t={token}").text
            check("«Кабинет магазина» has the помощник switch, on", 'name="assistant_digest"' in settings_page and "checked" in settings_page.split('name="assistant_digest"')[1][:60])
            client.post(f"/store/settings?t={token}", data={"name": "Мастерская", "address": "", "phone": "", "working_hours": "", "sales_channel": "", "buyback_topic_id": ""})
            with get_conn(db_path) as conn:
                check("saving the form with the box unticked switches it off", locations.get_location(conn, 1)["assistant_digest"] == 0)

    # ---- накладная через Claude, когда задан ключ
    class _ClaudeResponse:
        status_code, text = 200, "ok"

        def json(self):
            return {"content": [{"type": "text", "text": 'Вот:\n```json\n{"items": [{"name": "Дисплей", "qty": 3, "unit_cost": 1000}]}\n```'}]}

    calls: list[dict] = []

    def _fake_post(url, headers, json, timeout):
        calls.append({"url": url, "headers": headers, "json": json})
        return _ClaudeResponse()

    orig_post, orig_key = httpx.post, vision_ocr._ANTHROPIC_KEY
    httpx.post, vision_ocr._ANTHROPIC_KEY = _fake_post, "sk-ant-test"
    try:
        items = vision_ocr.extract_invoice_items(b"fake-bytes")
        check("with ANTHROPIC_API_KEY set the накладная is read by Claude (JSON taken out of its reply)",
              items == [{"name": "Дисплей", "qty": 3, "unit_cost": 1000}] and calls[0]["url"] == "https://api.anthropic.com/v1/messages"
              and calls[0]["headers"]["x-api-key"] == "sk-ant-test" and calls[0]["json"]["messages"][0]["content"][0]["type"] == "image")
        _ClaudeResponse.status_code = 529
        try:
            vision_ocr.extract_invoice_items(b"fake-bytes")
            check("a Claude error is a VisionOcrError, never an empty result", False)
        except vision_ocr.VisionOcrError:
            check("a Claude error is a VisionOcrError, never an empty result", True)
    finally:
        httpx.post, vision_ocr._ANTHROPIC_KEY = orig_post, orig_key


_dispatcher_singleton = None


def _the_dispatcher():
    """bot.bot.build_dispatcher(), once per run — aiogram lets a router be
    attached to a dispatcher only once."""
    global _dispatcher_singleton
    if _dispatcher_singleton is None:
        import bot.bot as bot_module

        _dispatcher_singleton = bot_module.build_dispatcher()
    return _dispatcher_singleton


def scenario_bot_dispatch() -> None:
    """Every keyboard button through the REAL dispatcher — routers in
    their order, filters, middleware. The other bot scenarios call handler
    functions directly, which skips exactly the layer where a button can
    be dead: on 06.10 «🛠 Наш ремонт» and «🛍 Продажа» answered nothing in
    the real client (Telegram appends U+FE0F to those emoji) while every
    test was green."""
    print("scenario: бот целиком — каждая кнопка клавиатуры доходит до обработчика")
    import asyncio
    import datetime

    from aiogram import Bot
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import Chat, Message, Update, User

    import bot.bot as bot_module
    from bot import quick_actions as qa

    TG = 885001

    check("the variation selector is the only thing normalised away",
          qa.canonical_button_text("🛠️ Наш ремонт") == qa.BTN_OUR_REPAIR and qa.canonical_button_text(" 🛍️ Продажа ") == qa.BTN_SALE
          and qa.canonical_button_text(qa.BTN_CASH) == qa.BTN_CASH and qa.canonical_button_text("Наш ремонт") == "Наш ремонт"
          and qa.canonical_button_text("0501234567") == "0501234567" and qa.canonical_button_text(None) is None)

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "dispatch.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            owner = auth.create_staff(conn, "bd-owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "bd-owner", TG)
            shifts.open_shift(conn, owner, 1)

        sent: list[str] = []

        class _FakeBot(Bot):
            async def __call__(self, method, request_timeout=None):
                name = type(method).__name__
                if name.startswith("Send"):
                    sent.append(getattr(method, "text", None) or getattr(method, "caption", None) or "")
                    return Message(message_id=7000 + len(sent), date=datetime.datetime.now(),
                                   chat=Chat(id=method.chat_id, type="private"), text="x")
                return True

        async def run() -> None:
            bot = _FakeBot(token="123456:test-bot-token-not-real")
            dp = _the_dispatcher()
            counter = 0

            async def tap(text: str, user_id: int = TG) -> tuple[bool, list[str]]:
                nonlocal counter
                counter += 1
                await dp.storage.set_state(key=StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id), state=None)
                sent.clear()
                update = Update(update_id=counter, message=Message(
                    message_id=counter, date=datetime.datetime.now(), chat=Chat(id=user_id, type="private"),
                    from_user=User(id=user_id, is_bot=False, first_name="T"), text=text))
                result = await dp.feed_update(bot, update)
                return result is not UNHANDLED, list(sent)

            labels = [b.text for row in qa.QUICK_ACTIONS_KEYBOARD.keyboard for b in row]
            check("every button on the keyboard is a known entry button", set(labels) <= qa._ENTRY_BUTTONS)
            dead = []
            for label in labels:
                emoji, rest = label.split(" ", 1)
                for variant in (label, f"{emoji}️ {rest}"):
                    handled, _replies = await tap(variant)
                    if not handled:
                        dead.append(variant)
            check("every keyboard button is handled — as written and as Telegram sends it (with U+FE0F)", dead == [])
            for label, expect in ((qa.BTN_OUR_REPAIR, "Наши ремонты"), (qa.BTN_SALE, "Что продаём"), (qa.BTN_REPAIR, "Пришлите фото устройства")):
                emoji, rest = label.split(" ", 1)
                _handled, replies = await tap(f"{emoji}️ {rest}")
                check(f"«{rest}» with the selector opens its own screen", any(expect in r for r in replies))
            handled, replies = await tap(qa.BTN_REPAIR_LEGACY)
            check("the pre-Заход-5 «🔧 Ремонт» still starts the intake and brings the new keyboard",
                  handled and any("Кнопки меню обновились" in r for r in replies) and any("Пришлите фото устройства" in r for r in replies))
            handled, replies = await tap("👤 Контакт")
            check("any other stale button gets the current keyboard instead of silence",
                  handled and replies == ["Кнопки меню обновились — вот актуальные. Нажмите нужную ещё раз."])
            handled, replies = await tap("/start")
            check("/start answers", handled and "Быстрые действия:" in replies)
            _handled, replies = await tap("что угодно", user_id=885999)
            check("a stranger's text gets nothing", replies == [])

        asyncio.run(run())


def scenario_repair_notes() -> None:
    """Заметки по ремонту: ответ на карточку в рабочей группе — текстом или
    голосом — остаётся в ремонте, коротко, с оригиналом рядом."""
    print("scenario: заметки по ремонту из чата — текст и голос в ответ на карточку")
    import asyncio
    import datetime

    from aiogram import Bot
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.types import CallbackQuery, Chat, File, Message, Update, User, Voice

    from core import ai_notes, notify, repair_notes

    GROUP, CARD_MESSAGE, MASTER_TG, OUTSIDER_TG = -100886, 4101, 886002, 886003
    LONG = ("Клиент звонил, сказал что заберёт только в пятницу после шести вечера, просил ещё поменять "
            "заднюю крышку если будет в наличии, согласен доплатить до восьмисот гривен, предоплату 500 уже внёс")

    # ---- the model helpers on their own
    calls: list[dict] = []

    class _Resp:
        def __init__(self, payload, status=200):
            self.status_code, self._payload, self.text = status, payload, "x"

        def json(self):
            return self._payload

    def _openai(content: str):
        return _Resp({"choices": [{"message": {"content": content}}]})

    answer = {"value": '{"summary": "Заберёт в пятницу после 18:00; задняя крышка до 800 грн; предоплата 500 внесена", "stage": "ждёт клиента до пятницы", "status": null}'}

    def _post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if "audio/transcriptions" in url:
            return _Resp({"text": "  Экран заменил, осталось проклеить  "})
        if "anthropic" in url:
            return _Resp({"content": [{"type": "text", "text": "Вот:\n```json\n" + answer["value"] + "\n```"}]})
        return _openai(answer["value"])

    orig_post = httpx.post
    saved_env = {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    httpx.post = _post
    os.environ["OPENAI_API_KEY"] = "sk-test"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        read = ai_notes.analyze(LONG, "В работе")
        check("a long message is read by the model (OpenAI when there is no Claude key): one short line and the stage; the whole text and the current status go to it",
              read == {"summary": "Заберёт в пятницу после 18:00; задняя крышка до 800 грн; предоплата 500 внесена",
                       "stage": "ждёт клиента до пятницы", "status": None, "price": None, "payment": None}
              and calls[-1]["url"].endswith("/chat/completions") and calls[-1]["json"]["messages"][1]["content"] == LONG
              and "«В работе»" in calls[-1]["json"]["messages"][0]["content"] and "цена для клиента: не известна" in calls[-1]["json"]["messages"][0]["content"])
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-test"
        check("with a Claude key the reading goes to Claude; the JSON is taken out of whatever surrounds it",
              ai_notes.analyze(LONG)["stage"] == "ждёт клиента до пятницы" and "anthropic" in calls[-1]["url"])
        os.environ.pop("ANTHROPIC_API_KEY", None)
        answer["value"] = '{"summary": "Ремонт завершён, устройство готово к выдаче клиенту", "stage": "готов, ждёт клиента", "status": "ready"}'
        check("a short message stays its own short version — only the stage and the status are taken from the model",
              ai_notes.analyze("  готово,   можно выдавать ") == {"summary": "готово, можно выдавать", "stage": "готов, ждёт клиента", "status": "ready", "price": None, "payment": None})
        answer["value"] = '{"summary": "' + LONG + ' и ещё много лишних слов сверху", "stage": "", "status": "done"}'
        check("a «summary» longer than the message is dropped, an empty stage is no stage, an unknown status is no status",
              ai_notes.analyze(LONG) == {"summary": LONG if len(LONG) <= ai_notes.MAX_SUMMARY else LONG[: ai_notes.MAX_SUMMARY - 1] + "…", "stage": None, "status": None, "price": None, "payment": None})
        for raw, expected in (('"card"', "card"), ('"cash"', "cash"), ('"bitcoin"', None), ("null", None)):
            answer["value"] = '{"summary": "x", "stage": null, "status": "issued", "price": null, "payment": ' + raw + "}"
            check(f"payment: {raw} -> {expected}", ai_notes.analyze("Отдал клиенту, оплатил", "Готов к выдаче", 2500)["payment"] == expected)
        for raw, current, expected, label in (
            ("3000", 2500, 3000, "a new price comes back as a number"),
            ("3000.0", 2500, 3000, "…a whole one as an int"),
            ("2500", 2500, None, "the price it already is is no change"),
            ("0", 2500, None, "zero is not a price"), ("-100", 2500, None, "nor a negative"),
            ('"три тысячи"', 2500, None, "nor words"), ("true", 2500, None, "nor a boolean"), ("99999999", 2500, None, "nor an absurd figure"),
        ):
            answer["value"] = '{"summary": "x", "stage": null, "status": null, "price": ' + raw + "}"
            check(f"price: {label}", ai_notes.analyze("Нашли ещё поломку, выйдет дороже", "В работе", current)["price"] == expected)
        check("the model is told the current price (it needs it for «плюс 500»)", "цена для клиента: 2500 грн" in calls[-1]["json"]["messages"][0]["content"])
        check("a voice message is transcribed (as an audio file upload)",
              ai_notes.transcribe(b"ogg-bytes") == "Экран заменил, осталось проклеить"
              and calls[-1]["files"]["file"][1] == b"ogg-bytes")
        for bad, label in ((lambda url, **kw: _Resp({}, 500), "a model error"), (lambda url, **kw: _Resp({"choices": []}), "a malformed answer"),
                           (lambda url, **kw: _openai("не json"), "an answer that isn't JSON"), (lambda url, **kw: _openai("[1, 2]"), "JSON that isn't an object")):
            httpx.post = bad
            try:
                ai_notes.analyze(LONG)
                check(f"{label} raises NoteAiError", False)
            except ai_notes.NoteAiError:
                check(f"{label} raises NoteAiError", True)
        httpx.post = lambda url, **kw: _Resp({"text": ""})
        try:
            ai_notes.transcribe(b"x")
            check("silence in a voice message raises NoteAiError", False)
        except ai_notes.NoteAiError:
            check("silence in a voice message raises NoteAiError", True)
        os.environ.pop("OPENAI_API_KEY", None)
        try:
            ai_notes.transcribe(b"x")
            check("no key raises NoteAiError", False)
        except ai_notes.NoteAiError:
            check("no key raises NoteAiError", True)
    finally:
        httpx.post = orig_post
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- the whole path through the real dispatcher
    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "notes.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ? WHERE id = 1", (GROUP,))
            owner = auth.create_staff(conn, "rn-owner", "pass", "Владелец", "owner")
            master = auth.create_master(conn, "Эдик", MASTER_TG, None, None)
            client_id = clients.get_or_create_by_phone(conn, "Клиент", "+380671234999", source="offline")
            repair_id = repairs.create_repair(conn, client_id, "Смартфон", "Apple", "iPhone 13", None, "Замена дисплея", "offline", master, 3000, owner, location_id=1)
            other_repair = repairs.create_repair(conn, client_id, "Смартфон", None, "A54", None, "Чистка", "offline", master, 400, owner, location_id=1)
            conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'staff')", (repair_id, str(GROUP), CARD_MESSAGE))
            check("a repair with no notes has no notes block on its card", "Заметки" not in repairs.card(conn, repair_id)[0])

        sent: list[dict] = []
        synced: list[str] = []

        class _FakeBot(Bot):
            async def __call__(self, method, request_timeout=None):
                name = type(method).__name__
                if name == "SendMessage":
                    markup = getattr(method, "reply_markup", None)
                    sent.append({"kind": "message", "text": method.text, "reply_to": getattr(method.reply_parameters, "message_id", None),
                                 "buttons": [b.callback_data for row in getattr(markup, "inline_keyboard", None) or [] for b in row]})
                    return Message(message_id=9000 + len(sent), date=datetime.datetime.now(), chat=Chat(id=method.chat_id, type="supergroup"), text="x")
                if name == "SetMessageReaction":
                    sent.append({"kind": "reaction", "emoji": method.reaction[0].emoji, "message_id": method.message_id})
                if name == "EditMessageText":
                    sent.append({"kind": "edit", "text": method.text, "message_id": method.message_id})
                if name == "DeleteMessage":
                    sent.append({"kind": "delete", "message_id": method.message_id})
                if name == "AnswerCallbackQuery":
                    sent.append({"kind": "answer", "text": method.text, "alert": bool(method.show_alert)})
                if name == "GetFile":
                    return File(file_id="v", file_unique_id="u", file_path="voice/file.oga")
                return True

            async def download_file(self, file_path, destination=None, **kwargs):
                return io.BytesIO(b"ogg-bytes")

        orig = (ai_notes.analyze, ai_notes.transcribe, notify.sync_repair_cards)
        read_statuses: list[str] = []

        def _read(text, status_label="", price=None):
            read_statuses.append((status_label, price))
            summary = text if len(text) <= ai_notes.SHORT_ENOUGH else "Заберёт в пятницу после 18:00; крышка до 800 грн; предоплата 500"
            lowered = text.lower()
            if "готово" in lowered:
                return {"summary": summary, "stage": "готов, ждёт клиента", "status": "ready", "price": None}
            if "жду запчасть" in lowered:
                return {"summary": summary, "stage": "ждём запчасть до понедельника", "status": None, "price": None}
            if "отдал клиенту" in lowered:
                return {"summary": summary, "stage": None, "status": "issued", "price": None,
                        "payment": "card" if "картой" in lowered else None}
            if "выйдет" in lowered:
                return {"summary": summary, "stage": "нашли ещё поломку", "status": None, "price": 4200}
            return {"summary": summary, "stage": None, "status": None, "price": None}

        ai_notes.analyze = _read
        ai_notes.transcribe = lambda audio, filename="voice.ogg": "Экран заменил, осталось проклеить и проверить сенсор, к вечеру отдам на выдачу, клиенту можно звонить"
        notify.sync_repair_cards = lambda messages, text, keyboard=None: synced.append(text)

        async def run() -> None:
            bot = _FakeBot(token="123456:test-bot-token-not-real")
            dp = _the_dispatcher()
            counter = 100

            async def post(user_id: int, name: str, reply_to: int | None, **content) -> tuple[bool, int]:
                nonlocal counter
                counter += 1
                sent.clear()
                group = Chat(id=GROUP, type="supergroup")
                replied = Message(message_id=reply_to, date=datetime.datetime.now(), chat=group, text="card") if reply_to else None
                update = Update(update_id=counter, message=Message(
                    message_id=counter, date=datetime.datetime.now(), chat=group,
                    from_user=User(id=user_id, is_bot=False, first_name=name), reply_to_message=replied, **content))
                result = await dp.feed_update(bot, update)
                await asyncio.sleep(0.05)
                return result is not UNHANDLED, counter

            # текст — коротко как есть
            handled, short_msg = await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Запчасть приехала, начинаю")
            with get_conn(db_path) as conn:
                notes = repair_notes.list_notes(conn, repair_id)
            check("a short text reply to the card is saved as it is, under the staff member's name, and acknowledged with a reaction — no chat noise",
                  handled and [(n["kind"], n["text"], n["author"], n["original_text"]) for n in notes]
                  == [("text", "Запчасть приехала, начинаю", "Эдик", "Запчасть приехала, начинаю")]
                  and sent == [{"kind": "reaction", "emoji": "✍", "message_id": short_msg}])
            check("the group card is brought up to date with it", bool(synced) and "📝 <b>Заметки (1)</b>" in synced[-1] and "<blockquote>Запчасть приехала, начинаю — <i>Эдик</i></blockquote>" in synced[-1])

            # длинный текст — сокращён, оригинал рядом; автор не из CRM
            _handled, long_msg = await post(OUTSIDER_TG, "Сергей Аутсорс", CARD_MESSAGE, text=LONG)
            with get_conn(db_path) as conn:
                note = repair_notes.list_notes(conn, repair_id)[-1]
            check("a long reply is stored short with the original next to it; someone not linked to a staff card is recorded under his Telegram name",
                  note["text"] == "Заберёт в пятницу после 18:00; крышка до 800 грн; предоплата 500" and note["original_text"] == LONG
                  and note["author"] == "Сергей Аутсорс" and note["staff_id"] is None)

            # голос
            handled, voice_msg = await post(MASTER_TG, "Эдик", CARD_MESSAGE, voice=Voice(file_id="v", file_unique_id="u", duration=12, file_size=4000))
            with get_conn(db_path) as conn:
                note = repair_notes.list_notes(conn, repair_id)[-1]
            check("a voice reply is transcribed, shortened and saved as a voice note with the transcript as its original",
                  handled and note["kind"] == "voice" and note["original_text"].startswith("Экран заменил, осталось проклеить")
                  and note["text"] == "Заберёт в пятницу после 18:00; крышка до 800 грн; предоплата 500")
            check("what was heard is said back in a reply, so the sender can check it",
                  sent[0]["kind"] == "message" and sent[0]["reply_to"] == voice_msg and f"🎤 РК-{repair_id:03d}:" in sent[0]["text"])

            # ответ на ответ — тот же ремонт; повтор — не дубль
            await post(OUTSIDER_TG, "Сергей Аутсорс", long_msg, text="Крышка есть, 650")
            before = counter
            with get_conn(db_path) as conn:
                check("a reply to an earlier note lands in the same repair", [n["text"] for n in repair_notes.list_notes(conn, repair_id)][-1] == "Крышка есть, 650"
                      and repair_notes.list_notes(conn, other_repair) == [])
                check("the same message delivered twice is one note", repair_notes.exists(conn, str(GROUP), before) and len(repair_notes.list_notes(conn, repair_id)) == 4)
            counter = before - 1
            await post(OUTSIDER_TG, "Сергей Аутсорс", long_msg, text="Крышка есть, 650")
            with get_conn(db_path) as conn:
                check("…checked: still four", len(repair_notes.list_notes(conn, repair_id)) == 4)

            # не наше
            handled, _id = await post(MASTER_TG, "Эдик", 777777, text="это ответ на чужое сообщение")
            handled_plain, _id = await post(MASTER_TG, "Эдик", None, text="просто сообщение в группе")
            quiet = sent == []
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="/chatid")
            with get_conn(db_path) as conn:
                check("a reply to something else, a plain message and a command leave no note — and the bot stays silent on the first two",
                      len(repair_notes.list_notes(conn, repair_id)) == 4 and quiet)

            # сбои модели
            def _down(text, status_label="", price=None):
                raise ai_notes.NoteAiError("нет связи")

            ai_notes.analyze = _down
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text=LONG)
            with get_conn(db_path) as conn:
                note = repair_notes.list_notes(conn, repair_id)[-1]
            check("if the model is down the note is saved all the same — in full, with no short version",
                  note["summary"] is None and note["text"] == LONG)
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="перезвонить завтра")
            with get_conn(db_path) as conn:
                check("…and a short one keeps itself as its short version", repair_notes.list_notes(conn, repair_id)[-1]["summary"] == "перезвонить завтра")

            def _deaf(audio, filename="voice.ogg"):
                raise ai_notes.NoteAiError("не разобрать")

            ai_notes.transcribe = _deaf
            _handled, deaf_msg = await post(MASTER_TG, "Эдик", CARD_MESSAGE, voice=Voice(file_id="v", file_unique_id="u", duration=3, file_size=900))
            with get_conn(db_path) as conn:
                check("a voice message that can't be transcribed saves nothing and says so",
                      len(repair_notes.list_notes(conn, repair_id)) == 6 and "Не смог расшифровать" in sent[0]["text"] and sent[0]["reply_to"] == deaf_msg)

            # ---- стадия и статус из сообщения
            ai_notes.analyze = _read
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Жду запчасть до понедельника")
            with get_conn(db_path) as conn:
                check("a message about the course of the repair sets its «Стадия» — on the record and on the group card",
                      repairs.get_repair(conn, repair_id)["stage_note"] == "ждём запчасть до понедельника"
                      and "Стадия: <b>ждём запчасть до понедельника</b>" in synced[-1])
            check("the model is told the repair's current status and price", read_statuses[-1] == ("Новый", 3000))
            check("a stage alone needs no confirmation — just the ✍", [m["kind"] for m in sent] == ["reaction"])
            # press() is used from here on — a tap on a button under one of the bot's messages
            async def press(user_id: int, name: str, data: str, message_id: int) -> None:
                nonlocal counter
                counter += 1
                sent.clear()
                group = Chat(id=GROUP, type="supergroup")
                update = Update(update_id=counter, callback_query=CallbackQuery(
                    id=str(counter), from_user=User(id=user_id, is_bot=False, first_name=name), chat_instance="c", data=data,
                    message=Message(message_id=message_id, date=datetime.datetime.now(), chat=group, text="?")))
                await dp.feed_update(bot, update)
                await asyncio.sleep(0.05)

            _handled, ready_msg = await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Готово, можно выдавать")
            with get_conn(db_path) as conn:
                repair = repairs.get_repair(conn, repair_id)
            told = next((m for m in sent if m["kind"] == "message"), None)
            check("«готово» in reply to the card moves the repair by itself — straight from «Новый» — nobody goes to the CRM",
                  repair["status"] == "ready")
            check("the bot says what it did, warns that no part was named, and leaves «Вернуть» under it",
                  told is not None and told["reply_to"] == ready_msg and f"РК-{repair_id:03d}: Новый → <b>Готов к выдаче</b>" in told["text"]
                  and "Запчасть не указана" in told["text"] and told["buttons"] == [f"rundo:{repair_id}:new"])
            check("the group card follows", "• Готов к выдаче" in synced[-1])
            await press(OUTSIDER_TG, "Сергей Аутсорс", f"rundo:{repair_id}:new", 9400)
            with get_conn(db_path) as conn:
                check("«Вернуть» puts it back — anyone in the group may press it",
                      repairs.get_repair(conn, repair_id)["status"] == "new"
                      and any(m["kind"] == "edit" and "возвращён из «Готов к выдаче» в <b>Новый</b>" in m["text"] for m in sent))
            await press(OUTSIDER_TG, "Сергей Аутсорс", f"rundo:{repair_id}:new", 9400)
            check("a second «Вернуть» has nothing to undo and says so", sent[0]["alert"] is True and "нечего" in sent[0]["text"])
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Жду запчасть до понедельника")
            with get_conn(db_path) as conn:
                repairs.claim_repair(conn, repair_id, master)
                check("taking the repair into work clears the old «Стадия»", repairs.get_repair(conn, repair_id)["stage_note"] is None)
            await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Жду запчасть до понедельника")
            check("a message that names no new status changes none", [m["kind"] for m in sent] == ["reaction"])

            # ---- «выдан»: с оплатой, без похода в CRM
            with get_conn(db_path) as conn:
                acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
                paid_repair = repairs.create_repair(conn, client_id, "Смартфон", None, "Redmi 12", None, "Экран", "offline", master, 2000, owner, location_id=1)
                conn.execute("UPDATE staff SET pay_type = 'percent', pay_value = 50 WHERE id = ?", (master,))
                repairs.claim_repair(conn, paid_repair, master)
                repairs.declare_no_parts(conn, paid_repair)
                conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'staff')", (paid_repair, str(GROUP), 4102))
                card_before, cash_before = accounts.balance(conn, acc[("card", "UAH")]), accounts.balance(conn, acc[("cash", "UAH")])
            _handled, out_msg = await post(MASTER_TG, "Эдик", 4102, text="Отдал клиенту, оплатил картой")
            told = next((m for m in sent if m["kind"] == "message"), None)
            with get_conn(db_path) as conn:
                repair = repairs.get_repair(conn, paid_repair)
                check("«отдал клиенту, оплатил картой»: выдан, the price is on the card account, the estimate became the final price",
                      repair["status"] == "issued" and repair["price_final"] == 2000
                      and accounts.balance(conn, acc[("card", "UAH")]) == card_before + 2000)
                check("…the master's share is accrued and the document has its profit, exactly as from the CRM",
                      masters.accrued_for(conn, "repair_order", paid_repair) == 1000 and documents.get_for(conn, "repair", paid_repair)["profit"] == 1000)
            check("the bot reports it in one line with «Вернуть»",
                  told is not None and f"РК-{paid_repair:03d} выдан · 2000 грн →" in told["text"] and told["buttons"] == [f"rundo:{paid_repair}:in_progress"])
            await press(MASTER_TG, "Эдик", f"rundo:{paid_repair}:in_progress", 9401)
            with get_conn(db_path) as conn:
                check("«Вернуть» after «выдан» takes the money back out, and the accrual and profit with it",
                      repairs.get_repair(conn, paid_repair)["status"] == "in_progress" and accounts.balance(conn, acc[("card", "UAH")]) == card_before
                      and masters.accrued_for(conn, "repair_order", paid_repair) == 0 and documents.get_for(conn, "repair", paid_repair)["profit"] is None
                      and any("Оплата снята с кассы" in (m.get("text") or "") for m in sent))
            await post(MASTER_TG, "Эдик", 4102, text="Отдал клиенту")
            asked = next((m for m in sent if m["kind"] == "message"), None)
            with get_conn(db_path) as conn:
                check("«отдал клиенту» with no word on how he paid: nothing moves yet — the bot asks the one thing it can't know, with a button per гривневый счёт",
                      repairs.get_repair(conn, paid_repair)["status"] == "in_progress" and asked is not None and "Как оплатили <b>2000 грн</b>" in asked["text"]
                      and f"rissue:{paid_repair}:{acc[('cash', 'UAH')]}" in asked["buttons"] and f"rissue:{paid_repair}:{acc[('cash', 'USD')]}" not in asked["buttons"])
            await press(OUTSIDER_TG, "Сергей Аутсорс", f"rissue:{paid_repair}:{acc[('cash', 'UAH')]}", 9402)
            with get_conn(db_path) as conn:
                check("one tap on the account finishes it: выдан, 2000 in the cash drawer",
                      repairs.get_repair(conn, paid_repair)["status"] == "issued" and accounts.balance(conn, acc[("cash", "UAH")]) == cash_before + 2000
                      and any(m["kind"] == "edit" and "выдан · 2000 грн" in m["text"] for m in sent))
            await press(OUTSIDER_TG, "Сергей Аутсорс", f"rissue:{paid_repair}:{acc[('cash', 'UAH')]}", 9402)
            with get_conn(db_path) as conn:
                check("a second tap doesn't take the money twice", accounts.balance(conn, acc[("cash", "UAH")]) == cash_before + 2000 and sent[0]["alert"] is True)
                free_repair = repairs.create_repair(conn, client_id, "Смартфон", None, "Nokia", None, "Чистка", "offline", master, None, owner, location_id=1)
                conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'staff')", (free_repair, str(GROUP), 4103))
            await post(MASTER_TG, "Эдик", 4103, text="Отдал клиенту")
            with get_conn(db_path) as conn:
                check("a repair with no price is handed over at once, with no payment and no question",
                      repairs.get_repair(conn, free_repair)["status"] == "issued" and any("без оплаты" in (m.get("text") or "") for m in sent))
            await post(MASTER_TG, "Эдик", 4103, text="Готово, можно выдавать")
            with get_conn(db_path) as conn:
                check("a message never drags a repair backwards", repairs.get_repair(conn, free_repair)["status"] == "issued"
                      and [m["kind"] for m in sent] == ["reaction"])
            with get_conn(db_path) as conn:
                for bad_from, bad_to in (("issued", "ready"), ("ready", "in_progress"), ("cancelled", "ready"), ("new", "new"), ("new", None)):
                    if repairs.chat_move_allowed(bad_from, bad_to):
                        check(f"{bad_from} -> {bad_to} is not a chat move", False)
                check("only forward moves are chat moves",
                      repairs.chat_move_allowed("new", "issued") and repairs.chat_move_allowed("ready", "cancelled") and not repairs.chat_move_allowed("issued", "cancelled"))

            # ---- цена из разговора
            _handled, price_msg = await post(OUTSIDER_TG, "Сергей Аутсорс", CARD_MESSAGE, text="Нашли ещё поломку, выйдет 4200")
            question = next((m for m in sent if m["kind"] == "message"), None)
            with get_conn(db_path) as conn:
                check("a message naming a new price changes NOTHING by itself — it is only recorded on the note",
                      repairs.current_price(repairs.get_repair(conn, repair_id)) == 3000
                      and repair_notes.list_notes(conn, repair_id)[-1]["suggested_price"] == 4200)
            check("the bot asks in the chat: «сменить на 4200 грн?» — да / нет, naming the price as it stands",
                  question is not None and question["reply_to"] == price_msg and "сейчас 3000 грн" in question["text"] and "<b>4200 грн</b>" in question["text"]
                  and question["buttons"] == [f"rprice:{repair_id}:4200", f"rprice_no:{repair_id}"])
            await press(OUTSIDER_TG, "Сергей Аутсорс", f"rprice:{repair_id}:4200", 9500)
            with get_conn(db_path) as conn:
                check("someone who isn't staff in the CRM can't confirm a price — he is told so and the price stays",
                      repairs.current_price(repairs.get_repair(conn, repair_id)) == 3000
                      and sent == [{"kind": "answer", "text": "Цену меняет сотрудник, подключённый к CRM.", "alert": True}])
            await press(MASTER_TG, "Эдик", f"rprice_no:{repair_id}", 9500)
            with get_conn(db_path) as conn:
                check("«Нет» removes the question and leaves the price alone",
                      repairs.current_price(repairs.get_repair(conn, repair_id)) == 3000 and sent[0] == {"kind": "delete", "message_id": 9500})
            await press(MASTER_TG, "Эдик", f"rprice:{repair_id}:4200", 9501)
            with get_conn(db_path) as conn:
                repair = repairs.get_repair(conn, repair_id)
                trail = repairs.get_status_history(conn, repair_id)[-1]
                notes_now = [n["text"] for n in repair_notes.list_notes(conn, repair_id)]
                check("«Да» from a staff member changes the price — the estimate, and the journal document's amount with it",
                      repair["price_estimate"] == 4200 and repair["price_final"] is None and documents.get_for(conn, "repair", repair_id)["amount"] == 4200)
                check("the change is left in the repair's HISTORY with who made it — not in its notes",
                      trail["comment"] == "Цена изменена: 3000 → 4200 грн" and trail["staff_name"] == "Эдик" and trail["status"] == repair["status"]
                      and not any("Цена изменена" in text for text in notes_now))
            check("the question turns into the record of what was done, and the group card shows the new price",
                  any(m["kind"] == "edit" and "3000 → <b>4200 грн</b> (Эдик)" in m["text"] for m in sent) and "Цена: 4200 грн" in synced[-1])
            with get_conn(db_path) as conn:
                repairs.set_price(conn, repair_id, 4200, 4200)
                repairs.change_price(conn, repair_id, 4500, master)
                check("when a final price was already set it moves too", repairs.get_repair(conn, repair_id)["price_final"] == 4500)
                repairs.declare_no_parts(conn, repair_id)
                repairs.complete_repair(conn, repair_id, master)
                repairs.update_status(conn, repair_id, "issued", owner)
            await press(MASTER_TG, "Эдик", f"rprice:{repair_id}:9000", 9502)
            with get_conn(db_path) as conn:
                check("once the repair is выдан a stale «Да» can't move the price",
                      repairs.current_price(repairs.get_repair(conn, repair_id)) == 4500 and sent[0]["alert"] is True and "Выдан" in sent[0]["text"])
            _handled, _id = await post(MASTER_TG, "Эдик", CARD_MESSAGE, text="Клиент сказал выйдет дорого")
            check("and a closed repair is not asked about its price at all", [m["kind"] for m in sent] == ["reaction"])

            # ---- команды карточке: «сумма 3000», «стоимость 0», «убери заметки»
            from bot import repair_attachments as ra
            check("a price said to the card is recognised only when it is all the message says",
                  [ra.price_said(t) for t in ("сумма 3000", "Сумма: 3 000 грн", "цена 3000", "стоимость 0", "3000 к оплате", "итого 4500 грн.", "ремонт 3000", "сума 2500")]
                  == [3000, 3000, 3000, 0, 3000, 4500, 3000, 2500]
                  and all(ra.price_said(t) is None for t in ("3000", "жду запчасть 3000", "нашли поломку, выйдет 4000", "цена запчасти 1800", "сумма 3000 наличными", None)))
            check("«убери заметки» is recognised in its forms and not in its opposite",
                  all(ra.hide_notes_said(t) for t in ("убери заметки", "удали все заметки", "очисти комментарии"))
                  and not any(ra.hide_notes_said(t) for t in ("заметки не убирай", "убери его", None)))
            with get_conn(db_path) as conn:
                conn.execute("UPDATE staff SET telegram_id = ? WHERE id = ?", (MASTER_TG, master))
                cmd_repair = repairs.create_repair(conn, client_id, "Смартфон", None, "Honor 90", None, "Экран", "offline", master, 1500, owner, location_id=1)
                repairs.claim_repair(conn, cmd_repair, master)
                conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'staff')", (cmd_repair, str(GROUP), 4105))
            ai_notes.analyze = _read
            await post(MASTER_TG, "Эдик", 4105, text="клиент просил позвонить после 18")
            _handled, price_reply = await post(MASTER_TG, "Эдик", 4105, text="сумма 3000")
            with get_conn(db_path) as conn:
                repair = repairs.get_repair(conn, cmd_repair)
                check("«сумма 3000» in reply to the card CHANGES the repair's price right there — it is not filed as a note",
                      repairs.current_price(repair) == 3000 and [n["text"] for n in repair_notes.list_notes(conn, cmd_repair)] == ["клиент просил позвонить после 18"]
                      and documents.get_for(conn, "repair", cmd_repair)["amount"] == 3000)
            told = next((m for m in sent if m["kind"] == "message"), None)
            check("the bot says what changed, with «Вернуть»; the group card shows the new price",
                  told is not None and told["reply_to"] == price_reply and f"РК-{cmd_repair:03d}: цена 1500 → <b>3000 грн</b>" in told["text"]
                  and told["buttons"] == [f"aundo:price:{cmd_repair}:1500"] and "Цена: 3000 грн" in synced[-1])
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="цена 9999")
            with get_conn(db_path) as conn:
                check("from someone who isn't staff in the CRM the same words only raise the question for staff to answer",
                      repairs.current_price(repairs.get_repair(conn, cmd_repair)) == 3000 and sent[0]["buttons"] == [f"rprice:{cmd_repair}:9999", f"rprice_no:{cmd_repair}"])
            await post(MASTER_TG, "Эдик", 4105, text="стоимость 0")
            with get_conn(db_path) as conn:
                repair = repairs.get_repair(conn, cmd_repair)
                check("«стоимость 0» makes it free: zero is a price, and the card says so",
                      repairs.current_price(repair) == 0 and repair["price_estimate"] == 0 and "Цена: 0 грн (бесплатно)" in repairs.card(conn, cmd_repair)[0])
            await post(MASTER_TG, "Эдик", 4105, text="Отдал клиенту")
            with get_conn(db_path) as conn:
                check("…and a free repair is handed over with no payment and no question",
                      repairs.get_repair(conn, cmd_repair)["status"] == "issued" and any("без оплаты" in (m.get("text") or "") for m in sent))
                repairs.undo_chat_status(conn, cmd_repair, "in_progress", None)
                check("the card carries the note as a quote until asked otherwise", "<blockquote>клиент просил позвонить после 18" in repairs.card(conn, cmd_repair)[0])
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="убери заметки")
            with get_conn(db_path) as conn:
                text, _kb = repairs.card(conn, cmd_repair)
                check("«убери заметки» takes the whole notes block off the card — nothing is deleted: the notes are still on the repair's record",
                      "Заметки" not in text and "blockquote" not in text and len(repair_notes.list_notes(conn, cmd_repair)) == 1
                      and "заметки убраны с карточки (1)" in sent[0]["text"] and "Заметки" not in synced[-1])
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="убери заметки")
            check("asked again, it says there is nothing to take off", "и так нет" in sent[0]["text"])
            await post(MASTER_TG, "Эдик", 4105, text="перезвонить завтра")
            with get_conn(db_path) as conn:
                text, _kb = repairs.card(conn, cmd_repair)
                check("a note written afterwards shows on the card again — alone", "📝 <b>Заметки (1)</b>" in text and "перезвонить завтра" in text and "после 18" not in text)
            # «верни заметки» и «удали» одну
            check("«верни заметки» is recognised; a single-note «удали» only as a bare instruction",
                  all(ra.show_notes_said(t) for t in ("Верни все заметки", "верни заметки", "покажи заметки", "восстанови комментарии"))
                  and not any(ra.show_notes_said(t) for t in ("верни телефон клиенту", "убери заметки", None))
                  and all(ra._REMOVE_THIS.match(t) for t in ("удали", "Убери это", "сотри эту заметку")) and not ra._REMOVE_THIS.match("удали ремонт из базы"))
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="Верни все заметки")
            with get_conn(db_path) as conn:
                text, _kb = repairs.card(conn, cmd_repair)
                check("«Верни все заметки» puts back what «убери заметки» took off — and is NOT itself filed as a note (10.10: it was)",
                      "📝 <b>Заметки (2)</b>" in text and "после 18" in text and "перезвонить завтра" in text
                      and len(repair_notes.list_notes(conn, cmd_repair)) == 2 and "возвращены на карточку (1)" in sent[0]["text"])
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="верни заметки")
            check("asked again, there is nothing to bring back", "возвращать нечего" in sent[0]["text"])
            _handled, junk_msg = await post(MASTER_TG, "Эдик", 4105, text="это я не туда написал, не обращайте внимания")
            await post(MASTER_TG, "Эдик", junk_msg, text="удали")
            with get_conn(db_path) as conn:
                text, _kb = repairs.card(conn, cmd_repair)
                check("«удали» in reply to one note's own message takes just that note off the card — the others stay, and it is still on record",
                      "не туда написал" not in text and "📝 <b>Заметки (2)</b>" in text and len(repair_notes.list_notes(conn, cmd_repair)) == 3
                      and "заметка убрана с карточки" in sent[0]["text"])
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="убери заметки")
            await post(OUTSIDER_TG, "Сергей Аутсорс", 4105, text="верни заметки")
            with get_conn(db_path) as conn:
                check("a note removed on its own stays removed through «убери» / «верни»", "не туда написал" not in repairs.card(conn, cmd_repair)[0]
                      and "📝 <b>Заметки (2)</b>" in repairs.card(conn, cmd_repair)[0])
            await post(MASTER_TG, "Эдик", 4105, text="удали")
            with get_conn(db_path) as conn:
                check("«удали» said to the card itself removes nothing and isn't filed as a note — the bot says how to do it",
                      "📝 <b>Заметки (2)</b>" in repairs.card(conn, cmd_repair)[0] and "Что убрать?" in sent[0]["text"])
                base_notes = len(repair_notes.list_notes(conn, cmd_repair))
            ai_notes.transcribe = lambda audio, filename="voice.ogg": "Сумма 2500"
            await post(MASTER_TG, "Эдик", 4105, voice=Voice(file_id="v", file_unique_id="u", duration=2, file_size=500))
            with get_conn(db_path) as conn:
                check("said by voice, «сумма 2500» does the same", repairs.current_price(repairs.get_repair(conn, cmd_repair)) == 2500
                      and len(repair_notes.list_notes(conn, cmd_repair)) == base_notes)

        try:
            asyncio.run(run())
        finally:
            ai_notes.analyze, ai_notes.transcribe, notify.sync_repair_cards = orig

        with get_conn(db_path) as conn:
            text, _keyboard = repairs.card(conn, repair_id)
            total_notes = len(repair_notes.list_notes(conn, repair_id))
            check("the group card shows the count and the latest three, each cut to fit",
                  f"📝 <b>Заметки ({total_notes})</b> — последние:" in text and total_notes > 10 and "Запчасть приехала" not in text
                  and text.count("<blockquote>") == 1 and text.split("<blockquote>")[1].split("</blockquote>")[0].count("\n") == 2
                  and len(text) < 1024)
            conn.execute("UPDATE devices SET defect_description = ? WHERE id = ?", ("Очень подробное описание неисправности. " * 20, repairs.get_repair(conn, repair_id)["device_id"]))
            long_text, _keyboard = repairs.card(conn, repair_id)
            check("a card that would run past Telegram's caption limit shows fewer notes rather than markup cut in half",
                  len(long_text) <= 1024 or "<blockquote>" not in long_text)
            check("…and its quote, if any is left, is whole", long_text.count("<blockquote>") == long_text.count("</blockquote>")
                  and f"Заметки ({total_notes})" in long_text)

        token = make_token(owner, "1")
        import webapp.main

        with TestClient(webapp.main.app) as client:
            page = client.get(f"/repairs/{repair_id}?t={token}").text
            check("the repair's page lists every note: the short text, who and when, the original behind «как было сказано», a mark on voice",
                  "Заметки из чата" in page and "Запчасть приехала, начинаю" in page and "Сергей Аутсорс" in page
                  and "как было сказано" in page and "согласен доплатить до восьмисот гривен" in page and "🎤" in page)
            check("a repair nobody wrote about has no such block", "Заметки из чата" not in client.get(f"/repairs/{other_repair}?t={token}").text)


def scenario_bot_questions() -> None:
    """«Бот, сколько у нас ремонтов?» — the list of repairs still with us,
    each device linking to its card in the chat."""
    print("scenario: вопрос боту по имени — список невыданных ремонтов со ссылками на карточки")
    import asyncio
    import datetime

    from aiogram import Bot
    from aiogram.dispatcher.event.bases import UNHANDLED
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import Chat, File, Message, Update, User, Voice

    from bot import assistant_chat
    from core import ai_notes, repair_digest, repair_notes

    GROUP, MASTERS_GROUP, TOPIC, OWNER_TG, MEMBER_TG = -1004478000111, -1003820000222, 5, 887001, 887002

    check("only a message that starts with the word «бот» is a question to it",
          assistant_chat.question_of("бот сколько у нас ремонтов") == "сколько у нас ремонтов"
          and assistant_chat.question_of("  Бот, какие готовы?") == "какие готовы?" and assistant_chat.question_of("БОТ: ремонты") == "ремонты"
          and assistant_chat.question_of("бот") == "" and assistant_chat.question_of("ботинок порвался") is None
          and assistant_chat.question_of("робот сломался") is None and assistant_chat.question_of("скажи бот сколько") is None
          and assistant_chat.question_of(None) is None)
    check("the assistant's answer becomes safe HTML: everything escaped, only a link to a repair card turned into a clickable name",
          assistant_chat.to_html("**Нашёл** [13 про <мах>](https://t.me/c/4478202416/5/135)  \n[зло](https://evil.example/x) <b>x</b>")
          == 'Нашёл <a href="https://t.me/c/4478202416/5/135">13 про &lt;мах&gt;</a>\n[зло](https://evil.example/x) &lt;b&gt;x&lt;/b&gt;')
    check("the question may narrow the list to a status",
          assistant_chat.statuses_asked("какие готовы") == ("ready",) and assistant_chat.statuses_asked("что в работе") == ("in_progress",)
          and assistant_chat.statuses_asked("новые ремонты") == ("new",) and assistant_chat.statuses_asked("сколько ремонтов") == repair_digest.OPEN_STATUSES)
    check("a link to a card: supergroup, with and without a topic; none for a chat that can't be linked",
          repair_digest.card_link("-1004478000111", 136, 5) == "https://t.me/c/4478000111/5/136"
          and repair_digest.card_link(-1003820000222, 276) == "https://t.me/c/3820000222/276" and repair_digest.card_link("-55512", 7) is None)

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "questions.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ?, masters_group_chat_id = ?, repair_topic_id = ? WHERE id = 1", (GROUP, MASTERS_GROUP, TOPIC))
            owner = auth.create_staff(conn, "bq-owner", "pass", "Владелец", "owner")
            auth.link_staff_telegram(conn, "bq-owner", OWNER_TG)
            master = auth.create_master(conn, "Эдик", None, None, None)
            client_id = clients.get_or_create_by_phone(conn, "Клиент", "+380671235000", source="offline")

            def make(model: str, price, status: str, location_id: int = 1, cards: bool = True) -> int:
                rid = repairs.create_repair(conn, client_id, "Смартфон", "Apple" if "iPhone" in model else None, model, None, "Ремонт", "offline", master, price, owner, location_id=location_id)
                if status != "new":
                    repairs.claim_repair(conn, rid, master)
                if status in ("ready", "issued"):
                    repairs.declare_no_parts(conn, rid)
                    repairs.complete_repair(conn, rid, master)
                if status == "issued":
                    repairs.update_status(conn, rid, "issued", owner)
                if status == "cancelled":
                    repairs.update_status(conn, rid, "cancelled", owner)
                if cards:
                    conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'topic')", (rid, str(GROUP), 100 + rid))
                    conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'masters_group')", (rid, str(MASTERS_GROUP), 200 + rid))
                return rid

            fresh = make("iPhone 13", 3000, "new")
            working = make("Galaxy <A54>", 1500, "in_progress")
            ready = make("Redmi Note 12", None, "ready")
            issued = make("iPhone 11", 2000, "issued")
            cancelled = make("Pixel 7", 900, "cancelled")
            no_card = make("Nokia 3310", 400, "in_progress", cards=False)
            elsewhere = make("iPad", 5000, "new", location_id=2)
            repair_notes.add_note(conn, working, "text", "жду запчасть", "жду запчасть", stage="ждём запчасть до понедельника")

            text = "\n".join(repair_digest.build(conn, 1, prefer_chat_id=GROUP, topics={str(GROUP): TOPIC}))
            check("the list: how many and for how much, grouped by status — готовые first; выданные, отменённые and other точки are not in it",
                  "<b>Ремонтов не выдано: 4</b> · на 4900 грн" in text and text.index("Готов к выдаче — 1") < text.index("В работе — 2") < text.index("Новый — 1")
                  and "iPhone 11" not in text and "Pixel 7" not in text and "iPad" not in text)
            check("each line: the device as a link to its card in THIS chat's topic, the price, the document number; a stage if there is one",
                  f'<a href="https://t.me/c/4478000111/5/{100 + fresh}">Apple iPhone 13</a> — 3000 грн · РК-{fresh:03d}' in text
                  and f'/5/{100 + working}">Galaxy &lt;A54&gt;</a> — 1500 грн · РК-{working:03d} · <i>ждём запчасть до понедельника</i>' in text
                  and f'">Redmi Note 12</a> — цена не указана' in text)
            check("a repair with no card in any chat is listed as plain text", f"• Nokia 3310 — 400 грн · РК-{no_card:03d}" in text)
            masters_text = "\n".join(repair_digest.build(conn, 1, prefer_chat_id=MASTERS_GROUP, topics={str(GROUP): TOPIC}))
            check("asked in the masters' group, the links lead to the cards there", f"https://t.me/c/3820000222/{200 + fresh}" in masters_text)
            check("asked somewhere with no cards (a DM), they lead to the staff group's topic",
                  f"https://t.me/c/4478000111/5/{100 + fresh}" in "\n".join(repair_digest.build(conn, 1, prefer_chat_id=OWNER_TG, topics={str(GROUP): TOPIC})))
            only_ready = "\n".join(repair_digest.build(conn, 1, statuses=("ready",)))
            check("narrowed to one status", "<b>Ремонтов: 1</b>" in only_ready and "Redmi Note 12" in only_ready and "iPhone 13" not in only_ready)
            check("nothing to show says so", repair_digest.build(conn, 3) == ["🔧 Невыданных ремонтов нет."])
            for i in range(60):
                make(f"Очень длинное название модели телефона номер {i} для проверки разбивки", 1000 + i, "new")
            chunks = repair_digest.build(conn, 1, prefer_chat_id=GROUP, topics={str(GROUP): TOPIC})
            check("a long list is split into messages that fit Telegram's limit, losing nothing",
                  len(chunks) > 1 and all(len(c) <= 4096 for c in chunks) and sum(c.count("• ") for c in chunks) == 64)
            conn.execute("DELETE FROM repair_order_messages WHERE order_id > ?", (elsewhere,))
            conn.execute("UPDATE repair_orders SET status = 'cancelled' WHERE id > ?", (elsewhere,))

        # ---- инструменты помощника
        from core import agent_tools, ai_agent

        check("how people type a model is matched to how it is written on the card",
              agent_tools.matches("13 айфон", "Apple iPhone 13") and agent_tools.matches("редми ноут", "Redmi note 13")
              and agent_tools.matches("айфон 13 про макс", "iPhone 13 Pro Max") and agent_tools.matches("poc", "Poco c65")
              and not agent_tools.matches("3", "iPhone 13") and not agent_tools.matches("самсунг", "iPhone 13"))
        ctx = {"location_id": 1, "can_money": False, "chat_id": str(GROUP), "topics": {str(GROUP): TOPIC}, "actions": []}
        with get_conn(db_path) as conn:
            by_model = agent_tools.call(conn, ctx, "find_repairs", {"query": "13 айфон"})
            check("find_repairs: by model in the asker's words, open repairs of this точка only, each with its card link",
                  [r["number"] for r in by_model["repairs"]] == [f"РК-{fresh:03d}"] and by_model["repairs"][0]["status"] == "Новый"
                  and by_model["repairs"][0]["card_link"] == f"https://t.me/c/4478000111/5/{100 + fresh}" and by_model["repairs"][0]["price_uah"] == 3000)
            check("…closed ones only when asked for", agent_tools.call(conn, ctx, "find_repairs", {"query": "айфон 11"})["total"] == 0
                  and agent_tools.call(conn, ctx, "find_repairs", {"query": "айфон 11", "include_closed": True})["repairs"][0]["status"] == "Выдан")
            check("…by document number and by the client's phone",
                  [r["id"] for r in agent_tools.call(conn, ctx, "find_repairs", {"query": f"РК-{ready}"})["repairs"]] == [ready]
                  and agent_tools.call(conn, ctx, "find_repairs", {"query": "067 123 50 00"})["total"] == 4)
            close = agent_tools.call(conn, ctx, "find_repairs", {"query": "редми айфон"})
            check("when nothing has every word, the closest come back marked as not exact",
                  close["total"] == 0 and close["exact_match"] is False and {r["id"] for r in close["closest"]} == {fresh, ready})
            detail = agent_tools.call(conn, ctx, "get_repair", {"repair_id": working})
            check("get_repair: the notes from the chat are there; the money isn't for someone who may not see it",
                  detail["notes"][0]["text"] == "жду запчасть" and detail["stage"] == "ждём запчасть до понедельника" and "finance" not in detail)
            check("a repair of another точка is «not found» for an ordinary employee", agent_tools.call(conn, ctx, "get_repair", {"repair_id": elsewhere}) == {"error": "ремонт не найден"})
            check("find_clients by phone", agent_tools.call(conn, ctx, "find_clients", {"query": "0671235000"})["clients"][0]["repairs"] >= 6)
            check("money tools don't exist for an ordinary employee — neither in the list offered to the model nor when called by name",
                  "cash_state" not in {t["function"]["name"] for t in agent_tools.schemas(False)}
                  and "error" in agent_tools.call(conn, ctx, "profit", {}) and "error" in agent_tools.call(conn, ctx, "debts", {}))
            boss = {**ctx, "can_money": True, "actions": []}
            check("a period goes back to the model the way people read it, not as ГГГГ-ММ-ДД",
                  agent_tools.call(conn, boss, "profit", {"date_from": "2026-10-01", "date_to": "2026-10-09"})["period"] == "01.10.2026 — 09.10.2026"
                  and re.fullmatch(r"\d\d\.\d\d\.\d{4}", agent_tools.call(conn, boss, "cash_state", {})["period"]))
            check("for an owner they do, and he may look at every точка at once",
                  "accounts" in agent_tools.call(conn, boss, "cash_state", {}) and "net" in agent_tools.call(conn, boss, "profit", {"date_from": "2026-01-01"})
                  and agent_tools.call(conn, boss, "find_repairs", {"query": "ipad", "all_points": True})["total"] == 1
                  and agent_tools.call(conn, ctx, "find_repairs", {"query": "ipad", "all_points": True})["total"] == 0
                  and "finance" in agent_tools.call(conn, boss, "get_repair", {"repair_id": working}))
            agent_tools.call(conn, ctx, "send_repair_card", {"repair_id": fresh})
            agent_tools.call(conn, ctx, "send_repair_card", {"repair_id": fresh})
            agent_tools.call(conn, ctx, "show_open_repairs", {"statuses": ["ready", "bogus"]})
            check("«send the card» and «show the list» only ask the bot to post — once each",
                  ctx["actions"] == [("repair_card", fresh), ("open_repairs", ("ready",))]
                  and agent_tools.call(conn, ctx, "send_repair_card", {"repair_id": 99999}) == {"error": "ремонт не найден"})
            check("the model is given no tool that changes anything", not any("status" in name or "price" in name or "change" in name for name in agent_tools.TOOLS))

            # ---- то, чего не хватило на живых вопросах 10.10
            oldest = agent_tools.call(conn, ctx, "find_repairs", {"sort": "oldest", "limit": 1})
            newest = agent_tools.call(conn, ctx, "find_repairs", {"sort": "newest", "limit": 1})
            check("«какой заказ самый старый» / «последний принятый»: repairs sorted by the day they were taken in",
                  [r["id"] for r in oldest["repairs"]] == [fresh] and oldest["total"] == 4 and [r["id"] for r in newest["repairs"]] == [no_card])
            stats = agent_tools.call(conn, boss, "repairs_stats", {})
            check("«сколько ремонтов сделано за всё время и на какую сумму, кроме отменённых»: all time when no dates are given, by status, with sums for the owner",
                  stats["period"] == "за всё время" and stats["total_taken_in"] == 66 and stats["done_and_issued"] == {"count": 1, "sum_uah": 2000}
                  and stats["without_cancelled"] == {"count": 5, "sum_uah": 3000 + 1500 + 2000 + 400}
                  and stats["by_status"]["Отменён"]["count"] == 61 and stats["by_status"]["В работе"] == {"count": 2, "sum_uah": 1900}
                  and stats["first_taken_in"] is not None)
            plain = agent_tools.call(conn, ctx, "repairs_stats", {})
            check("…the counts for anyone, the money only for those who may see it",
                  plain["done_and_issued"] == {"count": 1} and "sum_uah" not in plain["by_status"]["Новый"])
            check("a period narrows it; nothing in it is zeros, not an error",
                  agent_tools.call(conn, boss, "repairs_stats", {"date_from": "2020-01-01", "date_to": "2020-12-31"})["total_taken_in"] == 0
                  and agent_tools.call(conn, boss, "repairs_stats", {"date_from": timefmt.kyiv_today()})["total_taken_in"] == 66)
            totals = agent_tools.call(conn, ctx, "base_totals", {})
            check("«сколько в базе клиентов»: totals of everything", totals["clients"] == 1 and totals["repairs_all_time"] == 67 and totals["repairs_open_now"] == 5
                  and totals["masters"] == 1 and totals["base_kept_since"] is not None)
            conn.execute("UPDATE repair_orders SET created_at = '2026-10-06 08:30:00' WHERE id IN (?, ?)", (fresh, working))   # вторник, 11:30 по Киеву
            conn.execute("UPDATE repair_orders SET created_at = '2026-10-09 14:10:00' WHERE id = ?", (ready,))                    # пятница, 17:10
            pattern = agent_tools.call(conn, ctx, "intake_pattern", {"date_from": "2026-10-05", "date_to": "2026-10-09"})
            check("«в какое время самый большой приток»: repairs taken in by hour and weekday, Kyiv time, busiest first",
                  pattern["repairs_counted"] == 3 and pattern["by_hour_busiest_first"][0] == {"hours": "11:00–12:00", "repairs": 2}
                  and pattern["by_weekday_busiest_first"] == [{"day": "вторник", "repairs": 2}, {"day": "пятница", "repairs": 1}])
            conn.execute("UPDATE repair_orders SET created_at = datetime('now') WHERE id IN (?, ?, ?)", (fresh, working, ready))
            pick = lambda text, status: tuple([r["id"] for r in group] for group in agent_tools.repairs_for_status(conn, 1, text, status))
            check("which repair a status is about is settled without the model: every word that could be a name is on the card, among repairs that can make that move",
                  pick("галакси готов", "ready") == ([working], []) and pick("13 айфон взял в работу", "in_progress") == ([fresh], [])
                  and pick("редми выдан, оплатил наличными", "issued") == ([ready], []) and pick("пиксель готов", "ready") == ([], []))
            check("a near miss is never «the one»: «айфон 11 выдан» must not hand over the only open iPhone, a 13 — it is only shown as the closest",
                  pick("айфон 11 выдан", "issued") == ([], [fresh]))
            check("«РК-…» settles it outright; a number that can't make the move finds nothing",
                  pick(f"РК-{working:03d} готов", "ready") == ([working], []) and pick(f"рк {working} готов", "ready") == ([working], [])
                  and pick(f"РК-{issued} готов", "ready") == ([], []))
            check("a word every repair shares leaves several exact — and none is picked; no name at all finds nothing",
                  len(pick("смартфон выдан", "issued")[0]) > 1 and pick("выдан наличными", "issued") == ([], []))
            check("a request that asks something is never a status report",
                  all(assistant_chat.looks_like_question(q) for q in ("какие готовы", "сколько ремонтов выдано", "найди 13 айфон", "поко готов?", "пришли поко"))
                  and not any(assistant_chat.looks_like_question(q) for q in ("13 про мах выдан", "поко взял в работу", "галакси готов")))
            check("a broken call is an error for the model to read, never an exception",
                  "error" in agent_tools.call(conn, ctx, "no_such_tool", {}) and "error" in agent_tools.call(conn, ctx, "get_repair", {"repair_id": "abc"})
                  and "error" not in agent_tools.call(conn, ctx, "find_products", "not a dict"))
            probe_args = {"repair_id": fresh, "client_id": client_id, "query": "iphone"}
            for name in agent_tools.available(True):
                result = agent_tools.call(conn, {**boss, "actions": []}, name, probe_args)
                if "error" in result:
                    check(f"tool {name} runs on real data", False)
                    break
            else:
                check("every tool runs on real data and returns something JSON can carry",
                      all(json.dumps(agent_tools.call(conn, {**boss, "actions": []}, name, probe_args),
                                     ensure_ascii=False, default=str) for name in agent_tools.available(True)))

            # ---- цикл «модель ↔ инструменты»
            rounds: list[dict] = []
            script = [
                {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "find_repairs", "arguments": '{"query": "13 айфон"}'}}]},
                {"content": None, "tool_calls": [{"id": "c2", "type": "function", "function": {"name": "send_repair_card", "arguments": json.dumps({"repair_id": fresh})}},
                                                 {"id": "c3", "type": "function", "function": {"name": "profit", "arguments": "{}"}}]},
                {"content": "Нашёл РК, карточку прислал."},
            ]

            class _R:
                status_code, text = 200, "ok"

                def __init__(self, message):
                    self._m = message

                def json(self):
                    return {"choices": [{"message": self._m}]}

            def _post(url, **kwargs):
                rounds.append(kwargs["json"])
                return _R(script[len(rounds) - 1])

            orig_post, orig_key = httpx.post, os.environ.get("OPENAI_API_KEY")
            httpx.post, os.environ["OPENAI_API_KEY"] = _post, "sk-test"
            try:
                text, actions, _receipts = ai_agent.ask(conn, "найди заказ по 13 айфону и пришли в чат", location_id=1, point_name="Мастерская",
                                             can_money=False, chat_id=str(GROUP), topics={str(GROUP): TOPIC})
                check("the model asks for tools, gets their results, and its last message is the answer; what it asked to post comes back as actions",
                      text == "Нашёл РК, карточку прислал." and actions == [("repair_card", fresh)] and len(rounds) == 3)
                fed = [m for m in rounds[1]["messages"] if m["role"] == "tool"]
                check("it was given the real row from the base", f"РК-{fresh:03d}" in fed[0]["content"] and "Apple iPhone 13" in fed[0]["content"])
                denied = [m for m in rounds[2]["messages"] if m["role"] == "tool" and m["tool_call_id"] == "c3"]
                check("a money tool it wasn't offered is refused even if it calls it by name", "недоступен" in denied[0]["content"])
                check("the model is told who is asking, the moment in Kyiv time, and to write dates as ДД.ММ.ГГГГ ЧЧ:ММ",
                      "не владелец и не админ" in rounds[0]["messages"][0]["content"] and timefmt.kyiv_today() in rounds[0]["messages"][0]["content"]
                      and re.search(r"Сейчас \d\d\.\d\d\.\d{4} \d\d:\d\d по Киеву", rounds[0]["messages"][0]["content"])
                      and "ДД.ММ.ГГГГ ЧЧ:ММ" in rounds[0]["messages"][0]["content"]
                      and "cash_state" not in json.dumps(rounds[0]["tools"]))
                rounds.clear()
                script[:] = [
                    {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "find_repairs", "arguments": '{"query": "13 айфон"}'}}]},
                    {"content": "Нашёл: Apple iPhone 13, вот ссылка."},
                ]
                _text, actions, _receipts = ai_agent.ask(conn, "найди заказ по 13 айфону и пришли в чат", location_id=1, point_name="М", can_money=False)
                check("asked to SEND and the search came down to one repair — its card goes even if the model only described it",
                      actions == [("repair_card", fresh)])
                rounds.clear()
                _text, actions, _receipts = ai_agent.ask(conn, "что с 13 айфоном", location_id=1, point_name="М", can_money=False)
                check("…but a plain question about it sends nothing", actions == [])
                rounds.clear()
                script[:] = [
                    {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "find_repairs", "arguments": "{}"}}]},
                    {"content": "Вот все четыре."},
                ]
                _text, actions, _receipts = ai_agent.ask(conn, "пришли ремонт", location_id=1, point_name="М", can_money=False)
                check("…nor does «пришли» when several repairs fit — the bot doesn't guess which", actions == [])
                rounds.clear()
                script[:] = [{"content": None, "tool_calls": [{"id": "x", "type": "function", "function": {"name": "problems", "arguments": "{}"}}]}] * 20
                try:
                    ai_agent.ask(conn, "что там", location_id=1, point_name="М", can_money=True)
                    check("a model that never stops calling tools is cut off", False)
                except ai_agent.AgentError:
                    check("a model that never stops calling tools is cut off", len(rounds) == ai_agent.MAX_ROUNDS)
                httpx.post = lambda url, **kw: type("E", (), {"status_code": 500, "text": "boom"})()
                try:
                    ai_agent.ask(conn, "что там", location_id=1, point_name="М", can_money=True)
                    check("a model error is an AgentError", False)
                except ai_agent.AgentError:
                    check("a model error is an AgentError", True)
            finally:
                httpx.post = orig_post
                if orig_key is None:
                    os.environ.pop("OPENAI_API_KEY", None)
                else:
                    os.environ["OPENAI_API_KEY"] = orig_key

        # ---- весь путь через диспетчер
        sent: list[dict] = []

        class _FakeBot(Bot):
            async def __call__(self, method, request_timeout=None):
                name = type(method).__name__
                if name == "SendMessage":
                    markup = getattr(method, "reply_markup", None)
                    buttons = [b.callback_data or ("web_app" if b.web_app else "?") for row in getattr(markup, "inline_keyboard", None) or [] for b in row]
                    sent.append({"chat": method.chat_id, "text": method.text, "reply_to": getattr(method.reply_parameters, "message_id", None),
                                 "preview_off": getattr(method.link_preview_options, "is_disabled", None) is True, "buttons": buttons})
                    return Message(message_id=8000 + len(sent), date=datetime.datetime.now(), chat=Chat(id=method.chat_id, type="private"), text="x")
                if name == "SendPhoto":
                    markup = getattr(method, "reply_markup", None)
                    sent.append({"chat": method.chat_id, "photo": os.path.basename(method.photo.path), "text": method.caption, "reply_to": None,
                                 "buttons": [b.callback_data or ("web_app" if b.web_app else "?") for row in getattr(markup, "inline_keyboard", None) or [] for b in row]})
                    if photo_refused["on"]:
                        raise TelegramBadRequest(method=method, message="PHOTO_INVALID_DIMENSIONS")
                    return Message(message_id=8000 + len(sent), date=datetime.datetime.now(), chat=Chat(id=method.chat_id, type="private"), caption="x")
                if name == "GetFile":
                    return File(file_id="v", file_unique_id="u", file_path="voice/x.oga")
                return True

            async def download_file(self, file_path, destination=None, **kwargs):
                return io.BytesIO(b"ogg")

        photo_refused = {"on": False}

        asked_questions: list[dict] = []
        plan = {"answer": ("", []), "fail": True}

        def _fake_ask(conn, question, **kwargs):
            asked_questions.append({"question": question, **kwargs})
            if plan["fail"]:
                raise ai_agent.AgentError("нет связи")
            return (*plan["answer"], plan.get("receipts", []))

        orig_ask, orig_transcribe, orig_analyze = ai_agent.ask, ai_notes.transcribe, ai_notes.analyze
        ai_agent.ask = _fake_ask
        ai_notes.transcribe = lambda audio, filename="voice.ogg": "Бот, что с тринадцатым айфоном"

        async def run() -> None:
            bot = _FakeBot(token="123456:test-bot-token-not-real")
            dp = _the_dispatcher()
            counter = 500

            async def say(chat_id: int, user_id: int, text: str | None, reply_to: int | None = None, **content) -> tuple[bool, int]:
                nonlocal counter
                counter += 1
                sent.clear()
                chat = Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")
                replied = Message(message_id=reply_to, date=datetime.datetime.now(), chat=chat, text="card") if reply_to else None
                update = Update(update_id=counter, message=Message(
                    message_id=counter, date=datetime.datetime.now(), chat=chat, reply_to_message=replied,
                    from_user=User(id=user_id, is_bot=False, first_name="T"), text=text, **content))
                result = await dp.feed_update(bot, update)
                await asyncio.sleep(0.05)
                return result is not UNHANDLED, counter

            # модель недоступна — бот не молчит
            handled, _id = await say(GROUP, MEMBER_TG, "бот сколько у нас ремонтов")
            check("with the model down, a question about repairs still gets the list with links",
                  handled and len(sent) == 1 and sent[0]["preview_off"] and "Ремонтов не выдано: 4" in sent[0]["text"]
                  and f"https://t.me/c/4478000111/5/{100 + fresh}" in sent[0]["text"])
            await say(GROUP, MEMBER_TG, "Бот, какие готовы?")
            check("…narrowed by status", "Ремонтов: 1" in sent[0]["text"] and "Redmi Note 12" in sent[0]["text"])
            await say(GROUP, MEMBER_TG, "бот, привет")
            check("…and anything else gets «ИИ недоступен» with examples, not silence", len(sent) == 1 and "ИИ сейчас недоступен" in sent[0]["text"])

            # модель отвечает
            plan["fail"] = False
            plan["answer"] = ("Нашёл: РК-001 <Apple iPhone 13>, карточку прислал.", [("repair_card", fresh)])
            handled, asked_id = await say(GROUP, MEMBER_TG, "бот найди заказ по 13 айфону и пришли в чат")
            check("the request goes to the assistant without the word «бот», scoped to this точка and chat; an ordinary member may not see money",
                  asked_questions[-1]["question"] == "найди заказ по 13 айфону и пришли в чат" and asked_questions[-1]["location_id"] == 1
                  and asked_questions[-1]["can_money"] is False and asked_questions[-1]["chat_id"] == str(GROUP))
            check("its answer comes as a reply, safely escaped", sent[0]["reply_to"] == asked_id and "&lt;Apple iPhone 13&gt;" in sent[0]["text"])
            card = sent[1]
            check("and the repair's card is posted into the group as a live card — with its buttons",
                  f"РК-{fresh:03d} • Новый" in card["text"] and card["buttons"][0] == f"repair_take:{fresh}")
            with get_conn(db_path) as conn:
                copies = conn.execute("SELECT message_id FROM repair_order_messages WHERE order_id = ? AND kind = 'copy'", (fresh,)).fetchall()
                check("that copy is registered: it will be kept in sync, and a reply to it is a note on the same repair",
                      len(copies) == 1 and repairs.find_order_by_message(conn, str(GROUP), copies[0]["message_id"]) == fresh)
            # карточка целиком — с фото устройства, как исходная
            with get_conn(db_path) as conn:
                filename = repairs.write_device_photo(repairs.get_repair(conn, working)["device_id"], b"\xff\xd8\xff-fake-jpeg", ".jpg")
                repairs.set_device_photo(conn, repairs.get_repair(conn, working)["device_id"], filename)
            plan["answer"] = ("Вот он.", [("repair_card", working)])
            await say(GROUP, MEMBER_TG, "бот пришли самсунг")
            card = sent[1]
            with get_conn(db_path) as conn:
                copy = conn.execute("SELECT has_photo FROM repair_order_messages WHERE order_id = ? AND kind = 'copy'", (working,)).fetchone()
            check("a repair that has a photo is sent whole: the device photo with the card as its caption and the buttons under it",
                  card.get("photo") == filename and f"РК-{working:03d} • В работе" in card["text"] and len(card["text"]) <= 1024
                  and card["buttons"][0] == f"repair_part:{working}" and len(sent) == 2)
            check("…and is registered as a photo card, so later edits change its caption", copy["has_photo"] == 1)
            await say(OWNER_TG, OWNER_TG, "бот пришли самсунг")
            check("in the DM too — the photo, the card, «Открыть в CRM»", sent[1].get("photo") == filename and sent[1]["buttons"] == ["web_app"])
            photo_refused["on"] = True
            await say(GROUP, MEMBER_TG, "бот пришли самсунг")
            photo_refused["on"] = False
            check("if Telegram refuses the photo the card still comes, as text",
                  [("photo" in m) for m in sent[1:]] == [True, False] and f"РК-{working:03d}" in sent[2]["text"])

            # статус, сказанный боту
            def _status_read(text, status_label="", price=None):
                lowered = text.lower()
                status = "ready" if "готов" in lowered else "issued" if "выдан" in lowered else "in_progress" if "взял" in lowered else None
                return {"summary": text, "stage": None, "status": status, "price": None, "payment": "card" if "картой" in lowered else None}

            ai_notes.analyze = _status_read
            asked_before = len(asked_questions)
            await say(GROUP, MEMBER_TG, "бот галакси готов")
            with get_conn(db_path) as conn:
                check("«бот, галакси готов»: the one repair that fits is moved at once, «Вернуть» under the answer, the assistant isn't even asked",
                      repairs.get_repair(conn, working)["status"] == "ready" and len(sent) == 1 and len(asked_questions) == asked_before
                      and f"РК-{working:03d}: В работе → <b>Готов к выдаче</b>" in sent[0]["text"] and sent[0]["buttons"] == [f"rundo:{working}:in_progress"])
            await say(GROUP, MEMBER_TG, "бот смартфон выдан")
            with get_conn(db_path) as conn:
                check("several repairs fit — the bot lists them and changes nothing",
                      "не угадываю" in sent[0]["text"] and sent[0]["text"].count("• ") >= 2 and repairs.get_repair(conn, fresh)["status"] == "new"
                      and repairs.get_repair(conn, working)["status"] == "ready")
            await say(GROUP, MEMBER_TG, "бот пиксель готов")
            check("no repair fits — it says so", "Не нашёл ремонт" in sent[0]["text"])
            await say(GROUP, MEMBER_TG, "бот редми готов")
            check("a repair that is already there is answered «уже готов», not «не нашёл»", f"РК-{ready:03d} — уже «Готов к выдаче»" in sent[0]["text"])
            await say(GROUP, MEMBER_TG, "бот айфон 11 выдан")
            with get_conn(db_path) as conn:
                check("a near miss is shown, not acted on", "ближайшие, статус не трогаю" in sent[0]["text"] and "iPhone 13" in sent[0]["text"]
                      and repairs.get_repair(conn, fresh)["status"] == "new")
            await say(GROUP, MEMBER_TG, f"бот РК-{working} выдан картой")
            with get_conn(db_path) as conn:
                check("«бот, РК-… выдан картой»: handed over and paid in one message",
                      repairs.get_repair(conn, working)["status"] == "issued" and f"выдан · 1500 грн" in sent[0]["text"])
                repairs.undo_chat_status(conn, working, "in_progress", None)
            await say(GROUP, MEMBER_TG, "бот какие готовы?")
            check("a question with the same word in it goes to the assistant, not to a status change", len(asked_questions) == asked_before + 1)
            ai_notes.analyze = orig_analyze

            plan["answer"] = ("Вот список.", [("open_repairs", ("in_progress",))])
            await say(GROUP, MEMBER_TG, "бот что в работе")
            check("«show the list» posts the ready-made list after the answer", len(sent) == 2 and sent[0]["text"] == "Вот список." and sent[1]["text"].startswith("🔧 <b>Ремонтов") and "В работе — 2" in sent[1]["text"])
            plan["answer"] = ("В кассе 1500 грн.", [("repair_card", fresh)])
            await say(OWNER_TG, OWNER_TG, "бот сколько в кассе")
            check("the owner, in his DM, may ask about money", asked_questions[-1]["can_money"] is True and sent[0]["text"] == "В кассе 1500 грн.")
            check("a card sent to the DM carries «Открыть в CRM» instead of the group's buttons", sent[1]["buttons"] == ["web_app"])
            await say(GROUP, OWNER_TG, "бот сколько в кассе")
            check("the owner may ask about money in the group too", asked_questions[-1]["can_money"] is True)
            before = len(asked_questions)
            await say(887999, 887999, "бот сколько ремонтов")
            await say(-1009990009990, MEMBER_TG, "бот сколько ремонтов")
            await say(GROUP, MEMBER_TG, "ботинок порвался, сколько ремонтов таких было")
            check("a stranger in the DM, a group that isn't ours, a message that merely starts with «бот…» — nothing is asked, nothing is sent",
                  len(asked_questions) == before and sent == [])
            await say(GROUP, MEMBER_TG, "бот")
            check("just «бот» gets examples of what to ask", "Спросите меня" in sent[0]["text"] and len(asked_questions) == before)
            with get_conn(db_path) as conn:
                notes_before = len(repair_notes.list_notes(conn, fresh))
            plan["answer"] = ("Ок.", [])
            await say(GROUP, MEMBER_TG, "бот, сколько ремонтов", reply_to=100 + fresh)
            with get_conn(db_path) as conn:
                check("sent as a reply to a card it is still a request — answered, not filed as a note",
                      sent[0]["text"] == "Ок." and len(repair_notes.list_notes(conn, fresh)) == notes_before)

            # голосом в личке
            await say(OWNER_TG, OWNER_TG, None, voice=Voice(file_id="v", file_unique_id="u", duration=4, file_size=900))
            check("a voice message to the bot in the DM is a request: what was heard is said back, then answered",
                  sent[0]["text"].startswith("🎤 Бот, что с тринадцатым айфоном") and asked_questions[-1]["question"] == "что с тринадцатым айфоном"
                  and sent[1]["text"] == "Ок.")
            before = len(asked_questions)
            await say(GROUP, MEMBER_TG, None, voice=Voice(file_id="v", file_unique_id="u", duration=4, file_size=900))
            check("a voice message in a group that isn't a reply to a card is left alone", len(asked_questions) == before and sent == [])

        try:
            os.environ["CRM_MINIAPP_URL"] = os.environ.get("CRM_MINIAPP_URL") or "https://crm.example/miniapp"
            asyncio.run(run())
        finally:
            ai_agent.ask, ai_notes.transcribe, ai_notes.analyze = orig_ask, orig_transcribe, orig_analyze


def scenario_reminders() -> None:
    """«Бот, напомни завтра в 10:20 …» — set at once, shown back, posted
    into the chat when its time comes."""
    print("scenario: напоминания — поставить фразой, привязать к ремонту, прислать в чат в срок")
    import asyncio
    import datetime as dt

    from aiogram import Bot
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from bot import reminder_flow
    from core import ai_notes, reminders

    GROUP, TOPIC, OWNER_TG, MEMBER_TG, CARD = -1004479000111, 5, 888001, 888002, 4301
    now = dt.datetime(2026, 10, 9, 14, 30, tzinfo=timefmt.KYIV)   # пятница

    # ---- «когда»
    due = lambda d, t: reminders.resolve_due(d, t, now).strftime("%Y-%m-%d %H:%M")
    check("date and time as said", due("2026-10-10", "10:20") == "2026-10-10 10:20")
    check("only a time: today if it is still ahead, tomorrow if it has passed", due(None, "18:00") == "2026-10-09 18:00" and due(None, "09:00") == "2026-10-10 09:00")
    check("only a date: at 10:00", due("2026-10-12", None) == "2026-10-12 10:00")
    for d, t, needle, label in ((None, None, "когда напомнить", "neither date nor time"), ("2026-10-09", "09:00", "уже прошло", "a moment that has passed"),
                                ("2026-10-08", None, "уже прошло", "a day that has passed"), ("2030-01-01", "10:00", "не дальше чем на год", "years ahead"),
                                ("завтра", "10:00", "Не разобрал", "a date that isn't one"), ("2026-10-10", "25:99", "Не разобрал", "a time that isn't one")):
        try:
            reminders.resolve_due(d, t, now)
            check(f"{label} is refused", False)
        except reminders.ReminderError as exc:
            check(f"{label} is refused with a line a person can act on", needle in str(exc))

    # ---- разбор фразы моделью
    sent_prompts: list[dict] = []

    class _Resp:
        status_code, text = 200, "ok"

        def __init__(self, content):
            self._c = content

        def json(self):
            return {"choices": [{"message": {"content": self._c}}]}

    reply = {"value": '{"what": "спросить Андрея про готовность", "date": "2026-10-10", "time": "10:20", "repair": null}'}

    def _post(url, **kwargs):
        sent_prompts.append(kwargs["json"])
        return _Resp(reply["value"])

    orig_post, saved = httpx.post, {k: os.environ.get(k) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    httpx.post, os.environ["OPENAI_API_KEY"] = _post, "sk-test"
    os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        read = ai_notes.parse_reminder("поставь напоминание завтра на 10:20 что нужно спросить андрея про готовность", now)
        check("the model returns what and when; it is told the current moment and weekday (it needs them for «завтра», «в пятницу»)",
              read == {"what": "спросить Андрея про готовность", "date": "2026-10-10", "time": "10:20", "repair": None}
              and "2026-10-09 14:30, пятница" in sent_prompts[-1]["messages"][0]["content"])
        reply["value"] = '{"what": "", "date": null, "time": 15, "repair": "РК-48"}'
        check("blanks and wrong types come back as None", ai_notes.parse_reminder("x", now) == {"what": None, "date": None, "time": None, "repair": "РК-48"})
        reply["value"] = "не json"
        try:
            ai_notes.parse_reminder("x", now)
            check("an answer that isn't JSON raises NoteAiError", False)
        except ai_notes.NoteAiError:
            check("an answer that isn't JSON raises NoteAiError", True)
    finally:
        httpx.post = orig_post
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    check("«напомни …» is a reminder request; «какие напоминания» asks for the list; an ordinary question is neither",
          reminder_flow.is_reminder_request("напомни завтра отдать 13 про мах") and reminder_flow.is_reminder_request("поставь напоминание на 10:20")
          and reminder_flow.is_list_request("какие напоминания") and reminder_flow.is_list_request("покажи напоминания")
          and not reminder_flow.is_list_request("напомни какие ремонты готовы завтра в 9") and not reminder_flow.is_reminder_request("сколько ремонтов"))

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "reminders.sqlite3", ("Мастерская",)) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ?, repair_topic_id = ? WHERE id = 1", (GROUP, TOPIC))
            owner = auth.create_staff(conn, "rm-owner", "pass", "Павел", "owner")
            auth.link_staff_telegram(conn, "rm-owner", OWNER_TG)
            master = auth.create_master(conn, "Эдик", None, None, None)
            client_id = clients.get_or_create_by_phone(conn, "Иван", "+380671236000", source="offline")
            repair_id = repairs.create_repair(conn, client_id, "Смартфон", "Apple", "iPhone 13", None, "Экран", "offline", master, 3000, owner, location_id=1)
            other = repairs.create_repair(conn, client_id, "Смартфон", None, "Poco c65", None, "АКБ", "offline", master, 1400, owner, location_id=1)
            conn.execute("INSERT INTO repair_order_messages (order_id, chat_id, message_id, kind) VALUES (?, ?, ?, 'topic')", (repair_id, str(GROUP), CARD))

            # ---- книга напоминаний
            soon = reminders.create(conn, text="  позвонить   клиенту ", due=now + dt.timedelta(hours=1), chat_id=GROUP, location_id=1, staff_id=owner)
            later = reminders.create(conn, text="заказать дисплей", due=now + dt.timedelta(days=2), chat_id=GROUP, location_id=1, author_name="Сергей")
            check("a reminder is kept with its time in UTC and shown in Kyiv time",
                  reminders.get(conn, soon)["text"] == "позвонить клиенту" and reminders.get(conn, soon)["due_at"] == "2026-10-09 12:30:00"
                  and reminders.kyiv(reminders.get(conn, soon)["due_at"]) == "09.10.2026 15:30")
            check("nothing is due before its time; at its time exactly that one is",
                  reminders.due_now(conn, now) == [] and [r["id"] for r in reminders.due_now(conn, now + dt.timedelta(hours=1))] == [soon]
                  and [r["id"] for r in reminders.pending(conn, 1)] == [soon, later] and reminders.pending(conn, 2) == [])
            check("the author is the staff member, or the Telegram name of someone who isn't linked",
                  reminders.get(conn, soon)["author"] == "Павел" and reminders.get(conn, later)["author"] == "Сергей")
            reminders.mark_sent(conn, soon)
            check("sent means done", reminders.due_now(conn, now + dt.timedelta(days=5)) == [reminders.get(conn, later)] or
                  [r["id"] for r in reminders.due_now(conn, now + dt.timedelta(days=5))] == [later])
            check("«через час» makes a sent one pending again", reminders.snooze(conn, soon, 60, now) == "09.10.2026 15:30" and reminders.get(conn, soon)["status"] == "pending"
                  and reminders.snooze(conn, soon, 60, now) is None)
            check("cancel works once", reminders.cancel(conn, soon) and not reminders.cancel(conn, soon) and reminders.cancel(conn, later))
            failing = reminders.create(conn, text="x", due=now + dt.timedelta(minutes=1), chat_id=GROUP)
            for _ in range(reminders.MAX_ATTEMPTS):
                reminders.mark_failed(conn, failing)
            check("a reminder Telegram never takes is given up on, not retried forever", reminders.get(conn, failing)["status"] == "cancelled")
            try:
                reminders.create(conn, text="   ", due=now + dt.timedelta(hours=1), chat_id=GROUP)
                check("a reminder about nothing is refused", False)
            except reminders.ReminderError:
                check("a reminder about nothing is refused", True)

        # ---- через диспетчер
        sent: list[dict] = []
        refuse = {"on": False}

        class _FakeBot(Bot):
            async def __call__(self, method, request_timeout=None):
                name = type(method).__name__
                if name == "SendMessage":
                    if refuse["on"]:
                        raise TelegramBadRequest(method=method, message="chat not found")
                    markup = getattr(method, "reply_markup", None)
                    sent.append({"chat": method.chat_id, "text": method.text, "thread": method.message_thread_id,
                                 "reply_to": getattr(method.reply_parameters, "message_id", None),
                                 "buttons": [b.callback_data for row in getattr(markup, "inline_keyboard", None) or [] for b in row]})
                    return Message(message_id=8500 + len(sent), date=dt.datetime.now(), chat=Chat(id=method.chat_id, type="supergroup"), text="x")
                if name == "EditMessageText":
                    sent.append({"edit": method.text})
                if name == "AnswerCallbackQuery":
                    sent.append({"answer": method.text})
                return True

        plan = {"read": {"what": "спросить Андрея про готовность", "date": None, "time": None, "repair": None}}
        orig_parse, orig_analyze = ai_notes.parse_reminder, ai_notes.analyze
        ai_notes.parse_reminder = lambda text, now_: plan["read"]

        def _never(*args, **kwargs):
            raise AssertionError("a reminder request must not be read as a status report")

        ai_notes.analyze = _never

        def newest(conn):
            """The reminder set last (pending() is ordered by when it is due, not by when it was set)."""
            return max(reminders.pending(conn, 1), key=lambda r: r["id"])

        async def run() -> None:
            bot = _FakeBot(token="123456:test-bot-token-not-real")
            dp = _the_dispatcher()
            counter = 700
            tomorrow = (dt.datetime.now(timefmt.KYIV) + dt.timedelta(days=1)).strftime("%Y-%m-%d")

            async def say(chat_id: int, user_id: int, name: str, text: str, reply_to: int | None = None, thread: int | None = None) -> int:
                nonlocal counter
                counter += 1
                sent.clear()
                chat = Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup")
                replied = Message(message_id=reply_to, date=dt.datetime.now(), chat=chat, text="card") if reply_to else None
                await dp.feed_update(bot, Update(update_id=counter, message=Message(
                    message_id=counter, date=dt.datetime.now(), chat=chat, reply_to_message=replied, message_thread_id=thread,
                    is_topic_message=bool(thread), from_user=User(id=user_id, is_bot=False, first_name=name), text=text)))
                await asyncio.sleep(0.05)
                return counter

            async def press(data: str, message_id: int, chat_id: int = GROUP) -> None:
                nonlocal counter
                counter += 1
                sent.clear()
                await dp.feed_update(bot, Update(update_id=counter, callback_query=CallbackQuery(
                    id=str(counter), from_user=User(id=MEMBER_TG, is_bot=False, first_name="Сергей"), chat_instance="c", data=data,
                    message=Message(message_id=message_id, date=dt.datetime.now(), chat=Chat(id=chat_id, type="supergroup"), text="?"))))
                await asyncio.sleep(0.05)

            plan["read"] = {"what": "спросить Андрея про готовность", "date": tomorrow, "time": "10:20", "repair": None}
            asked = await say(GROUP, MEMBER_TG, "Сергей", "бот поставь напоминание завтра на 10:20 что нужно спросить андрея про готовность", thread=TOPIC)
            with get_conn(db_path) as conn:
                first = newest(conn)
            check("asked in the group: set at once for that chat and topic, under the asker's Telegram name",
                  first["text"] == "спросить Андрея про готовность" and first["chat_id"] == str(GROUP) and first["thread_id"] == TOPIC
                  and first["author"] == "Сергей" and first["order_id"] is None and reminders.kyiv(first["due_at"]).endswith("10:20"))
            check("the bot says back exactly what it understood — when and what — with «Отменить» under it",
                  sent[0]["reply_to"] == asked and re.search(r"<b>\d\d\.\d\d\.\d{4} 10:20</b> \(по Киеву\): спросить Андрея про готовность", sent[0]["text"])
                  and sent[0]["buttons"] == [f"rem_cancel:{first['id']}"])

            plan["read"] = {"what": "отдать телефон клиенту", "date": tomorrow, "time": "15:00", "repair": None}
            await say(GROUP, MEMBER_TG, "Сергей", "бот напомни завтра в 15:00 что этот заказ нужно отдать", reply_to=CARD)
            with get_conn(db_path) as conn:
                tied = newest(conn)
                check("said in reply to a repair's card — «этот заказ» is that repair; the phone is NOT handed over by the word «отдать»",
                      tied["order_id"] == repair_id and repairs.get_repair(conn, repair_id)["status"] == "new")
            check("the answer names the repair", f"РК-{repair_id:03d} · Apple iPhone 13" in sent[0]["text"])

            plan["read"] = {"what": "проверить АКБ", "date": tomorrow, "time": "12:00", "repair": "поко"}
            await say(OWNER_TG, OWNER_TG, "Павел", "бот напомни завтра в 12 проверить акб на поко")
            with get_conn(db_path) as conn:
                from_dm = newest(conn)
            check("asked in the DM: it will go to the точка's work group, and the repair named in words is found",
                  from_dm["chat_id"] == str(GROUP) and from_dm["order_id"] == other and from_dm["author"] == "Павел"
                  and "Пришлю в рабочую группу" in sent[0]["text"])
            plan["read"] = {"what": "позвонить", "date": tomorrow, "time": "12:00", "repair": "смартфон"}
            await say(OWNER_TG, OWNER_TG, "Павел", "бот напомни завтра позвонить по смартфону")
            with get_conn(db_path) as conn:
                check("words that fit several repairs tie it to none — no guessing", newest(conn)["order_id"] is None)

            plan["read"] = {"what": "позвонить", "date": None, "time": None, "repair": None}
            await say(GROUP, MEMBER_TG, "Сергей", "бот напомни позвонить клиенту")
            check("no time named — nothing is set, the bot asks when", "когда напомнить" in sent[0]["text"] and sent[0]["buttons"] == [])
            plan["read"] = {"what": "позвонить", "date": "2020-01-01", "time": "10:00", "repair": None}
            await say(GROUP, MEMBER_TG, "Сергей", "бот напомни первого января двадцатого года")
            with get_conn(db_path) as conn:
                check("a time in the past — nothing is set", "уже прошло" in sent[0]["text"] and len(reminders.pending(conn, 1)) == 4)

            await say(GROUP, MEMBER_TG, "Сергей", "бот какие напоминания")
            check("«какие напоминания» lists what is pending, soonest first, each with its full date — день.месяц.год время",
                  sent[0]["text"].count("• ") == 4 and len(re.findall(r"• \d\d\.\d\d\.\d{4} \d\d:\d\d — ", sent[0]["text"])) == 4 and "спросить Андрея про готовность" in sent[0]["text"] and f"РК-{repair_id:03d}" in sent[0]["text"])
            await press(f"rem_cancel:{from_dm['id']}", 8600)
            with get_conn(db_path) as conn:
                check("«Отменить» cancels it", reminders.get(conn, from_dm["id"])["status"] == "cancelled" and {"edit": "✖️ Напоминание отменено."} in sent)

            # ---- доставка
            sent.clear()
            check("nothing goes out before its time", await reminder_flow.deliver_due(bot) == 0 and sent == [])
            with get_conn(db_path) as conn:
                conn.execute("UPDATE reminders SET due_at = datetime('now', '-1 minute') WHERE id IN (?, ?)", (first["id"], tied["id"]))
            refuse["on"] = True
            check("if Telegram won't take it, it stays pending for the next tick", await reminder_flow.deliver_due(bot) == 0)
            refuse["on"] = False
            delivered = await reminder_flow.deliver_due(bot)
            plain = next(m for m in sent if "спросить Андрея" in m["text"])
            about = next(m for m in sent if "отдать телефон клиенту" in m["text"])
            check("when its time comes each is posted once into its chat: the plain one into the topic it was asked in",
                  delivered == 2 and plain["chat"] == GROUP and plain["thread"] == TOPIC and plain["reply_to"] is None
                  and "поставил(а) Сергей" in plain["text"] and plain["buttons"] == [f"rem_snooze:{first['id']}"])
            check("…and the one about a repair as a reply to that repair's card", about["reply_to"] == CARD and f"РК-{repair_id:03d} · Apple iPhone 13" in about["text"])
            sent.clear()
            check("a second tick sends nothing again", await reminder_flow.deliver_due(bot) == 0 and sent == [])
            await press(f"rem_snooze:{first['id']}", 8700)
            with get_conn(db_path) as conn:
                check("«Напомнить через час» makes it pending again", reminders.get(conn, first["id"])["status"] == "pending"
                      and any("Напомню ещё раз" in (m.get("answer") or "") for m in sent))

        try:
            asyncio.run(run())
        finally:
            ai_notes.parse_reminder, ai_notes.analyze = orig_parse, orig_analyze


def scenario_agent_actions() -> None:
    """«Бот, запиши расход…», «бот, прими ремонт…», «бот, продай…» — the
    assistant fills in a form, code decides and does, a receipt with
    «Вернуть» reports it."""
    print("scenario: действия по просьбе боту — расход, ремонт, продажа, деньги, склад; чек и «Вернуть»")
    import asyncio
    import datetime as dt

    from aiogram import Bot
    from aiogram.types import CallbackQuery, Chat, Message, Update, User

    from bot import assistant_chat
    from core import agent_actions, ai_agent, notify, orders, repair_notes, settlements

    GROUP, OWNER_TG, KEEPER_TG, STRANGER_TG = -1004480000111, 889001, 889002, 889003

    with tempfile.TemporaryDirectory() as tmp, _separate_base(tmp, "actions.sqlite3", ("Мастерская", "Магазин")) as db_path:
        with get_conn(db_path) as conn:
            conn.execute("UPDATE locations SET staff_group_chat_id = ? WHERE id = 1", (GROUP,))
            owner = auth.create_staff(conn, "aa-owner", "pass", "Павел", "owner")
            auth.link_staff_telegram(conn, "aa-owner", OWNER_TG)
            keeper = auth.create_staff(conn, "aa-keeper", "pass", "Кладовщик", "storekeeper", location_id=1)
            auth.link_staff_telegram(conn, "aa-keeper", KEEPER_TG)
            master = auth.create_master(conn, "Эдик", None, "percent", 50)
            auth.create_master(conn, "Сергей", None, None, None)
            cell = inventory.create_cell(conn, "AA-1", None, None, location_id=1)
            cable = inventory.create_product(conn, "Кабель Lightning", "AA-CBL", None, "шт", False, True, 0, 300)
            glass = inventory.create_product(conn, "Стекло iPhone 13", "AA-GLS", None, "шт", True, True, 0, None)
            purchases.create_receipt(conn, None, None, owner, [(cable, cell, 10, 100), (glass, cell, 4, 50)], location_id=1)
            cash.record_adjustment(conn, 20000, "старт", owner, location_id=1)
            buyback.create_purchase(conn, seller_phone="0671237000", model="iPhone 12", imei="359000000000001", comment=None, price="4000", staff_id=owner, location_id=1)
            buyback.create_purchase(conn, seller_phone="0671237000", model="Redmi 12", imei="359000000000002", comment=None, price="2000", staff_id=owner, location_id=1)
            buyback.create_purchase(conn, seller_phone="0671237000", model="Redmi 12", imei="359000000000003", comment=None, price="2000", staff_id=owner, location_id=1)
            anna = clients.get_or_create_by_phone(conn, "Анна", "+380671237001", source="offline")
            repair_id = repairs.create_repair(conn, anna, "Смартфон", None, "13 про мах", None, "Экран", "offline", None, 1500, owner, location_id=1)
            acc = {(a["kind"], a["currency"]): a["id"] for a in accounts.list_accounts(conn, 1)}
            cash_uah, card_uah = acc[("cash", "UAH")], acc[("card", "UAH")]
            masters.accrue(conn, master, "repair", "repair_order", 1, 1000, comment="РК-001")
            owner_row, keeper_row = auth.get_staff_by_telegram_id(conn, OWNER_TG), auth.get_staff_by_telegram_id(conn, KEEPER_TG)

            def ctx_for(staff) -> dict:
                return {"location_id": 1, "can_money": bool(staff) and staff["role"] in ("owner", "admin"), "chat_id": str(GROUP), "topics": {},
                        "actions": [], "last_found": [], "staff": staff, "receipts": [], "done": set()}

            def do(staff, action, **args):
                ctx = ctx_for(staff)
                return agent_actions.run(conn, ctx, action, args), ctx["receipts"]

            # ---- кто что может
            check("someone who isn't staff in the CRM is offered no actions at all", agent_actions.schemas(None) == [] and "refused" in do(None, "add_expense", amount=100)[0])
            check("an action is offered only to the roles that may do it in the Mini App",
                  {"cash_correction", "master_payout", "cancel_document"} <= set(agent_actions.available(owner_row))
                  and not {"cash_correction", "master_payout", "cancel_document", "new_repair"} & set(agent_actions.available(keeper_row))
                  and "add_expense" in agent_actions.available(keeper_row))
            check("…and is refused if called anyway", do(keeper_row, "master_payout", master="Эдик", amount=100)[0] == {"refused": "Это действие вам недоступно."})
            check("no such action is a refusal, not an exception", "refused" in do(owner_row, "launch_rocket")[0])

            # ---- смена: без неё деньги не двигаются
            result, receipts = do(owner_row, "add_expense", amount=500, comment="вода")
            check("money doesn't move before the shift is opened — the refusal says what to do",
                  "Смена сегодня не открыта" in result["refused"] and receipts == [] and accounts.balance(conn, cash_uah) == 12000)
            shifts.open_shift(conn, owner, 1)
            shifts.open_shift(conn, keeper, 1)
            till = accounts.balance(conn, cash_uah)

            conn.execute("UPDATE shifts SET closed_at = datetime('now') WHERE staff_id = ?", (owner,))
            conn.execute("UPDATE shifts SET opened_at = datetime('now', '-2 days') WHERE staff_id = ?", (owner,))
            check("yesterday's shift is not today's", "Смена сегодня не открыта" in do(owner_row, "add_expense", amount=1)[0]["refused"])
            result, receipts = do(owner_row, "open_shift")
            check("«открой смену»: opened, and the receipt shows the balances it opens on",
                  result == {"done": True} and shifts.current_shift(conn, owner, 1) is not None and "Смена открыта · остатки приняты как есть" in receipts[0]["text"]
                  and "Наличные: 12000 UAH" in receipts[0]["text"])
            check("asked again, it just says the shift is open", do(owner_row, "open_shift")[0].get("note") == "смена сегодня уже открыта"
                  and conn.execute("SELECT COUNT(*) AS n FROM shifts WHERE staff_id = ? AND closed_at IS NULL", (owner,)).fetchone()["n"] == 1)

            # ---- расход
            result, receipts = do(keeper_row, "add_expense", amount="500", category="other", comment="вода")
            doc = conn.execute("SELECT * FROM documents WHERE doc_type = 'cash_out' ORDER BY id DESC LIMIT 1").fetchone()
            check("«запиши расход 500 на воду»: an РКО is made, the cash goes down, the receipt is written by code with the document's number",
                  result == {"done": True, "document": documents.doc_label(doc)} and accounts.balance(conn, cash_uah) == till - 500
                  and receipts[0]["text"] == f"✅ <b>{documents.doc_label(doc)}</b> расход 500 грн · Прочее · Наличные · вода" and receipts[0]["undo"] == ("doc", doc["id"]))
            check("«Вернуть» by the person who did it cancels the document and puts the money back",
                  "отменён" in agent_actions.undo(conn, ("doc", str(doc["id"])), keeper_row) and accounts.balance(conn, cash_uah) == till)
            result, receipts = do(owner_row, "add_expense", amount=700, category="rent", account="карта")
            doc_card = conn.execute("SELECT * FROM documents WHERE doc_type = 'cash_out' ORDER BY id DESC LIMIT 1").fetchone()
            check("the account is found by a word; money that left an account which didn't have it is flagged on the receipt",
                  accounts.balance(conn, card_uah) == -700 and "Аренда · Карта" in receipts[0]["text"] and "теперь -700 грн — меньше нуля" in receipts[0]["text"])
            try:
                agent_actions.undo(conn, ("doc", str(doc_card["id"])), keeper_row)
                check("a storekeeper can't undo the owner's document", False)
            except agent_actions.Refused:
                check("a storekeeper can't undo the owner's document", True)
            for bad, needle in (({"amount": "много"}, "числом"), ({"amount": -5}, "числом"), ({"amount": 100, "account": "биткоин"}, "На какой счёт"),
                                ({"amount": 100, "account": "доллары"}, "валютный")):
                check(f"expense with {bad} is refused with a question", needle in do(owner_row, "add_expense", **bad)[0]["refused"])
            check("refusals wrote nothing", accounts.balance(conn, cash_uah) == till and accounts.balance(conn, card_uah) == -700)

            # ---- касса: внести / изъять
            check("a cash correction needs a reason", "причину" in do(owner_row, "cash_correction", direction="in", amount=1000, comment="")[0]["refused"])
            do(owner_row, "cash_correction", direction="out", amount=1000, comment="инкассация")
            check("«изъять 1000, инкассация» is a КР, not an expense", accounts.balance(conn, cash_uah) == till - 1000
                  and conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'cash_adjust'").fetchone()["n"] == 2)

            # ---- ремонт: приём, цена, мастер, без запчасти, заметка
            result, receipts = do(owner_row, "new_repair", client_phone="067 123 70 02", client_name="Борис", device="iPhone 13 Pro", defect="не заряжается", price=2500, master="эдик")
            new_id = max(r["id"] for r in repairs.list_repairs(conn))
            new_repair = repairs.get_repair(conn, new_id)
            check("«прими ремонт: айфон 13 про, не заряжается, 2500, клиент 067…, мастер Эдик»: client created by phone, repair on record",
                  result["document"] == f"РК-{new_id:03d}" and new_repair["client_phone"] == "+380671237002" and new_repair["client_name"] == "Борис"
                  and new_repair["model"] == "iPhone 13 Pro" and new_repair["price_estimate"] == 2500 and new_repair["master_id"] == master
                  and new_repair["defect_description"] == "не заряжается" and new_repair["status"] == "new")
            check("the receipt carries what the bot has to do after the write is committed — post the new card to the groups — and how to undo it",
                  receipts[0]["after"][:2] == ("notify_repair", new_id) and receipts[0]["undo"] == ("repair_new", new_id) and "принят: iPhone 13 Pro · 2500 грн" in receipts[0]["text"])
            for bad, needle in (({"client_phone": "0671237002"}, "Какое устройство"), ({"device": "iPhone"}, "Какой клиент"),
                                ({"client_phone": "12", "device": "iPhone"}, "Не похоже на номер"), ({"client_phone": "0671237002", "device": "X", "master": "Вася"}, "Какой мастер")):
                check(f"a repair with {sorted(bad)} only is refused with the question to ask", needle in do(owner_row, "new_repair", **bad)[0]["refused"])
            check("refused intakes created nothing", max(r["id"] for r in repairs.list_repairs(conn)) == new_id)
            agent_actions.undo(conn, ("repair_new", str(new_id)), owner_row)
            check("«Вернуть» on a repair just taken in cancels it", repairs.get_repair(conn, new_id)["status"] == "cancelled")

            result, receipts = do(owner_row, "set_repair_price", repair="13 про мах", price=3000)
            check("«поставь цену 3000 на 13 про мах»: done, with the old price kept for «Вернуть»",
                  repairs.current_price(repairs.get_repair(conn, repair_id)) == 3000 and receipts[0]["undo"] == ("price", repair_id, 1500) and "1500 → <b>3000 грн</b>" in receipts[0]["text"])
            agent_actions.undo(conn, ("price", str(repair_id), "1500"), owner_row)
            check("…and back", repairs.current_price(repairs.get_repair(conn, repair_id)) == 1500)
            check("a repair that isn't there is refused with the list of open ones", "Сейчас открыты: РК-" in do(owner_row, "set_repair_price", repair="самсунг", price=100)[0]["refused"])
            do(owner_row, "assign_repair_master", repair=f"РК-{repair_id}", master="Сергей")
            do(owner_row, "repair_without_parts", repair=str(repair_id))
            do(keeper_row, "add_repair_note", repair="про мах", text="клиент просил позвонить после 18")
            fixed = repairs.get_repair(conn, repair_id)
            check("master, «без запчасти» and a note — by number or by words", fixed["master_name"] == "Сергей" and fixed["no_parts"] == 1
                  and repair_notes.list_notes(conn, repair_id)[-1]["text"] == "клиент просил позвонить после 18")

            result, receipts = do(keeper_row, "clear_repair_notes", repair="про мах")
            check("«бот, убери заметки с 13 про мах»: the block comes off the card, the note stays on record",
                  "заметки убраны с карточки (1)" in receipts[0]["text"] and repair_notes.card_notes(conn, repair_id) == [] and len(repair_notes.list_notes(conn, repair_id)) == 1)
            result, receipts = do(keeper_row, "restore_repair_notes", repair="про мах")
            check("«бот, верни заметки на 13 про мах»", "возвращены на карточку (1)" in receipts[0]["text"] and len(repair_notes.card_notes(conn, repair_id)) == 1)
            do(owner_row, "set_repair_price", repair=str(repair_id), price=0)
            check("«поставь цену 0» is allowed — free", repairs.current_price(repairs.get_repair(conn, repair_id)) == 0)
            do(owner_row, "set_repair_price", repair=str(repair_id), price=1500)

            # ---- продажа
            till = accounts.balance(conn, cash_uah)
            result, receipts = do(keeper_row, "sell", product="кабель", qty=2, client_phone="0671237001")
            sale = sales.list_sales(conn)[0]
            check("«продай 2 кабеля клиенту 067…»: price from the product's card, cash by default, the client on the sale",
                  sale["total"] == 600 and sale["client_name"] == "Анна" and accounts.balance(conn, cash_uah) == till + 600 and inventory.product_total_qty(conn, cable, 1) == 8
                  and "продажа: Кабель Lightning × 2 — 600 грн · Наличные · Анна" in receipts[0]["text"] and result["sold_product_id"] == cable)
            agent_actions.undo(conn, receipts[0]["undo"], keeper_row)
            check("«Вернуть» on a sale puts the goods and the money back", inventory.product_total_qty(conn, cable, 1) == 10 and accounts.balance(conn, cash_uah) == till)
            do(keeper_row, "sell", product="кабель", price=250, client_phone="0671237001", account="в долг")
            check("«в долг»: nothing into the касса, the client owes it", accounts.balance(conn, cash_uah) == till and settlements.client_position(conn, anna)["they_owe"] == 250)
            for bad, needle in (({"product": "кабель", "account": "в долг"}, "только клиенту"), ({"product": "стекло"}, "нет цены"),
                                ({"product": "айфон"}, "несколько"), ({"product": "редми"}, "Назовите IMEI"), ({"product": "чехол"}, "в каталоге нет"),
                                ({"product": "кабель", "qty": 500}, "")):
                refusal = do(keeper_row, "sell", **bad)[0].get("refused")
                check(f"sale {bad} is refused, never guessed", refusal is not None and needle in refusal)
            do(keeper_row, "sell", product="359000000000001", price=6500, account="карта")
            check("a phone is sold by its IMEI", inventory.find_unit_by_imei(conn, "359000000000001") is None and accounts.balance(conn, card_uah) == -700 + 6500)
            do(keeper_row, "sell", product="айфон 12", price=6500)
            check("…and a serial product with nothing left on the shelf is refused", inventory.find_unit_by_imei(conn, "359000000000001") is None)

            # ---- деньги клиента, выплата мастеру
            result, receipts = do(keeper_row, "client_money", direction="in", client_phone="0671237001", amount=250)
            check("«Анна принесла 250»: ПКО, her debt is gone, the receipt says where she stands",
                  settlements.balance(conn, anna) == 0 and "принято от Анна: 250 грн · Наличные · теперь взаиморасчёты 0" in receipts[0]["text"])
            check("handing money out is the owner's call", "владелец или админ" in do(keeper_row, "client_money", direction="out", client_name="Анна", amount=100)[0]["refused"])
            do(owner_row, "client_money", direction="out", client_name="анна", amount=100, comment="возврат")
            check("the owner may; a client is found by name when only one fits", settlements.client_position(conn, anna)["they_owe"] == 100)
            check("an unknown client is not created by a money move", "в базе нет" in do(owner_row, "client_money", direction="in", client_phone="0509990000", amount=5)[0]["refused"])
            result, receipts = do(owner_row, "master_payout", master="эдик", amount=600)
            check("«выплати Эдику 600»: РКО, and the receipt says what is still owed", masters.owed(conn, master) == 400 and "осталось должны 400 грн" in receipts[0]["text"])

            # ---- склад, резерв, клиент, отмена документа
            check("a write-off needs a reason", "причину" in do(keeper_row, "stock_write_off", product="кабель", qty=1, comment="")[0]["refused"])
            do(keeper_row, "stock_write_off", product="кабель", qty=2, comment="брак")
            do(keeper_row, "stock_add", product="стекло", qty=3)
            check("«спиши 2 кабеля, брак» and «оприходуй 3 стекла»", inventory.product_total_qty(conn, cable, 1) == 7 and inventory.product_total_qty(conn, glass, 1) == 7)
            check("devices by IMEI are not written off or added this way", "IMEI" in do(keeper_row, "stock_write_off", product="359000000000002", qty=1, comment="x")[0]["refused"]
                  and "IMEI" in do(keeper_row, "stock_add", product="редми", qty=1)[0].get("refused", ""))
            result, receipts = do(keeper_row, "reserve_for_client", product="кабель", qty=3, client_phone="0671237005", client_name="Вика")
            check("«отложи 3 кабеля для 067…»: a заказ with a 24-hour hold", orders.list_orders(conn, statuses=("reserved",))[0]["total"] == 900
                  and "отложено на 24 часа" in receipts[0]["text"])
            do(keeper_row, "add_client", name="Глеб", phone="0671237006")
            check("a new client is recorded; the same number twice is refused", clients.get_by_phone(conn, "+380671237006")["name"] == "Глеб"
                  and "уже есть" in do(keeper_row, "add_client", name="Глеб", phone="0671237006")[0]["refused"])
            sale_label = documents.doc_label(documents.get_for(conn, "sale", sales.list_sales(conn)[0]["id"]))
            check("cancelling a document needs a reason and a number that exists",
                  "причину" in do(owner_row, "cancel_document", number=sale_label, reason="")[0]["refused"]
                  and "не найден" in do(owner_row, "cancel_document", number="ПД-999", reason="x")[0]["refused"]
                  and "Какой документ" in do(owner_row, "cancel_document", number="что-то", reason="x")[0]["refused"])
            do(owner_row, "cancel_document", number=sale_label.lower(), reason="клиент вернул")
            check("«отмени ПД-…, клиент вернул»", documents.get_for(conn, "sale", sales.list_sales(conn)[0]["id"])["status"] == "cancelled")
            do(keeper_row, "sell", product="кабель")
            do(keeper_row, "sell", product="кабель", price=310)
            live_sales = conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'sale' AND status = 'posted'").fetchone()["n"]
            several = do(owner_row, "cancel_document", last_of_type="sale", reason="то был тест")[0]
            check("«удали ту продажу, то был тест» with several live sales names them and cancels none",
                  live_sales > 1 and "Таких документов несколько: ПД-" in several["refused"]
                  and conn.execute("SELECT COUNT(*) AS n FROM documents WHERE doc_type = 'sale' AND status = 'posted'").fetchone()["n"] == live_sales)
            only_order = do(owner_row, "cancel_document", last_of_type="client_order", reason="то был тест")
            check("…and with only one of that kind it is cancelled, no number needed",
                  only_order[0].get("done") and orders.list_orders(conn, statuses=("reserved",)) == [] and "отменён · причина: то был тест" in only_order[1][0]["text"])
            check("no reason, or a kind with nothing live, is refused",
                  "причину" in do(owner_row, "cancel_document", last_of_type="sale", reason="")[0]["refused"]
                  and "нет" in do(owner_row, "cancel_document", last_of_type="exchange", reason="x")[0]["refused"])

            # ---- не дважды; чужая точка; целостность
            ctx = ctx_for(owner_row)
            first = agent_actions.run(conn, ctx, "add_expense", {"amount": 50, "comment": "скотч"})
            again = agent_actions.run(conn, ctx, "add_expense", {"comment": "скотч", "amount": 50})
            check("the same request twice in one turn is carried out once", first.get("done") and "уже сделано" in again["refused"] and len(ctx["receipts"]) == 1)
            check("an action that fails halfway leaves nothing behind (the write is rolled back to before it)", _stock_is_consistent(conn))

        # ---- цикл модели с действием и весь путь через диспетчер
        rounds: list[dict] = []
        script: list[dict] = []

        class _R:
            status_code, text = 200, "ok"

            def __init__(self, message):
                self._m = message

            def json(self):
                return {"choices": [{"message": self._m}]}

        def _post(url, **kwargs):
            rounds.append(kwargs["json"])
            if len(rounds) > len(script):   # the script ran out: the model «goes down»
                return type("Down", (), {"status_code": 500, "text": "boom"})()
            return _R(script[len(rounds) - 1])

        sent: list[dict] = []
        posted_cards: list[int] = []

        class _FakeBot(Bot):
            async def __call__(self, method, request_timeout=None):
                name = type(method).__name__
                if name == "SendMessage":
                    markup = getattr(method, "reply_markup", None)
                    sent.append({"text": method.text, "reply_to": getattr(method.reply_parameters, "message_id", None),
                                 "buttons": [b.callback_data for row in getattr(markup, "inline_keyboard", None) or [] for b in row]})
                    return Message(message_id=8800 + len(sent), date=dt.datetime.now(), chat=Chat(id=method.chat_id, type="supergroup"), text="x")
                if name == "EditMessageText":
                    sent.append({"edit": method.text})
                if name == "AnswerCallbackQuery":
                    sent.append({"answer": method.text, "alert": bool(method.show_alert)})
                return True

        from core import ai_notes

        orig_post, orig_key, orig_notify, orig_analyze = httpx.post, os.environ.get("OPENAI_API_KEY"), repairs.notify_and_save, ai_notes.analyze
        httpx.post, os.environ["OPENAI_API_KEY"] = _post, "sk-test"
        repairs.notify_and_save = lambda store, order_id, text, keyboard, photo: posted_cards.append(order_id)
        # None of these requests reports a status; the reading that looks for one is out of the way here.
        ai_notes.analyze = lambda text, status_label="", price=None: {"summary": text, "stage": None, "status": None, "price": None, "payment": None}

        def tool(name: str, **args) -> dict:
            return {"content": None, "tool_calls": [{"id": f"c{len(script)}", "type": "function", "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}]}

        async def run() -> None:
            bot = _FakeBot(token="123456:test-bot-token-not-real")
            dp = _the_dispatcher()
            counter = 900

            async def say(user_id: int, text: str, reply_to_bot: int | None = None) -> int:
                nonlocal counter
                counter += 1
                sent.clear()
                rounds.clear()
                chat = Chat(id=GROUP, type="supergroup")
                replied = Message(message_id=reply_to_bot, date=dt.datetime.now(), chat=chat, text="?", from_user=User(id=123456, is_bot=True, first_name="bot")) if reply_to_bot else None
                await dp.feed_update(bot, Update(update_id=counter, message=Message(
                    message_id=counter, date=dt.datetime.now(), chat=chat, reply_to_message=replied,
                    from_user=User(id=user_id, is_bot=False, first_name="T"), text=text)))
                await asyncio.sleep(0.05)
                return counter

            with get_conn(db_path) as conn:
                till = accounts.balance(conn, cash_uah)
            script[:] = [tool("add_expense", amount=300, comment="такси", category="other"), {"content": "Записал расход."}]
            asked = await say(KEEPER_TG, "бот запиши расход 300 на такси")
            with get_conn(db_path) as conn:
                doc = conn.execute("SELECT * FROM documents WHERE doc_type = 'cash_out' ORDER BY id DESC LIMIT 1").fetchone()
                check("«бот, запиши расход 300 на такси»: done at once — no question, no trip to the app",
                      accounts.balance(conn, cash_uah) == till - 300 and doc["title"] is not None)
            check("the model's line comes as the reply, and the receipt — written by code — as its own message with «Вернуть»",
                  sent[0]["text"] == "Записал расход." and sent[0]["reply_to"] == asked
                  and sent[1]["text"].startswith(f"✅ <b>{documents.doc_label(doc)}</b> расход 300 грн") and sent[1]["buttons"] == [f"aundo:doc:{doc['id']}"])
            names = {t["function"]["name"] for t in rounds[0]["tools"]}
            check("a storekeeper's model is offered the actions he may do and no others, and is told to act without asking back",
                  "add_expense" in names and "master_payout" not in names and "cash_state" not in names
                  and "не переспрашивай" in rounds[0]["messages"][0]["content"])
            fed = [m for m in rounds[1]["messages"] if m["role"] == "tool"][0]["content"]
            check("the model is told the action went through, with the document's number", '"done": true' in fed and documents.doc_label(doc) in fed)

            counter += 1
            sent.clear()
            await dp.feed_update(bot, Update(update_id=counter, callback_query=CallbackQuery(
                id=str(counter), from_user=User(id=KEEPER_TG, is_bot=False, first_name="T"), chat_instance="c", data=f"aundo:doc:{doc['id']}",
                message=Message(message_id=8802, date=dt.datetime.now(), chat=Chat(id=GROUP, type="supergroup"), text="?"))))
            with get_conn(db_path) as conn:
                check("«Вернуть» under the receipt undoes it", accounts.balance(conn, cash_uah) == till and any("отменён" in (m.get("edit") or "") for m in sent))

            # не хватает данных — вопрос, и ответ на него без слова «бот»
            script[:] = [tool("add_expense", comment="вода"), {"content": "Назовите сумму числом."}]
            await say(KEEPER_TG, "бот запиши расход на воду")
            with get_conn(db_path) as conn:
                check("with the amount missing nothing is written — the bot asks for it", accounts.balance(conn, cash_uah) == till and sent[0]["text"] == "Назовите сумму числом." and len(sent) == 1)
            script[:] = [tool("add_expense", amount=120, comment="вода"), {"content": "Готово."}]
            await say(KEEPER_TG, "120", reply_to_bot=8803)
            with get_conn(db_path) as conn:
                check("the answer, sent as a reply to the bot's question without the word «бот», finishes the request",
                      accounts.balance(conn, cash_uah) == till - 120 and len(sent) == 2)
            # (the list is the live one the loop went on appending tool calls to — only what was said counts)
            history = [m["content"] for m in rounds[0]["messages"] if m["role"] in ("user", "assistant") and m.get("content") and not m.get("tool_calls")]
            check("the model was given the conversation so far", history[-3:] == ["запиши расход на воду", "Назовите сумму числом.", "120"])
            before = len(rounds)
            await say(OWNER_TG, "500", reply_to_bot=8803)
            check("someone the bot has no conversation with isn't taken for one by replying to its message", sent == [])

            # приём ремонта: карточка в группы уходит после записи
            script[:] = [tool("new_repair", client_phone="0671237009", device="Poco X5", defect="экран", price=1800), {"content": "Принял."}]
            await say(OWNER_TG, "бот прими ремонт поко х5 экран 1800 клиент 0671237009")
            with get_conn(db_path) as conn:
                accepted = max(r["id"] for r in repairs.list_repairs(conn))
            check("a repair taken in by request gets its card posted to the work groups, like one taken in the usual way",
                  posted_cards == [accepted] and f"РК-{accepted:03d}</b> принят: Poco X5 · 1800 грн" in sent[1]["text"] and sent[1]["buttons"] == [f"aundo:repair_new:{accepted}"])

            # не сотрудник
            script[:] = [{"content": "Для этого нужно привязать ваш Telegram в карточке сотрудника."}]
            await say(STRANGER_TG, "бот запиши расход 100")
            check("someone in the group who isn't CRM staff gets no actions — the model isn't even offered them and is told why",
                  not any(t["function"]["name"] in agent_actions.ACTIONS for t in rounds[0]["tools"]) and "не подключён к CRM" in rounds[0]["messages"][0]["content"])

            # сбой модели после действия — ничего не остаётся
            with get_conn(db_path) as conn:
                till_now = accounts.balance(conn, cash_uah)
            script[:] = [tool("add_expense", amount=999, comment="сбой")]
            await say(KEEPER_TG, "бот запиши расход 999")
            with get_conn(db_path) as conn:
                check("if the model fails after an action, the action is not kept — and no receipt is shown for it",
                      accounts.balance(conn, cash_uah) == till_now and not any("999" in (m.get("text") or "") for m in sent))

        try:
            asyncio.run(run())
        finally:
            httpx.post, repairs.notify_and_save, ai_notes.analyze = orig_post, orig_notify, orig_analyze
            if orig_key is None:
                os.environ.pop("OPENAI_API_KEY", None)
            else:
                os.environ["OPENAI_API_KEY"] = orig_key
            assistant_chat._HISTORY.clear()


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
    scenario_purchase_chat()
    scenario_service_center()
    scenario_service_center_http()
    scenario_repair_part_chat()
    scenario_settlements()
    scenario_settlements_http()
    scenario_sale_chat()
    scenario_overview()
    scenario_bot_dispatch()
    scenario_repair_notes()
    scenario_bot_questions()
    scenario_reminders()
    scenario_agent_actions()
    if os.path.exists(_WEBAPP_TEST_DB):
        os.remove(_WEBAPP_TEST_DB)

    print(f"\nPASS={PASS} FAIL={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
