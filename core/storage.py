"""SQLite storage layer: schema, connection helper, soft migrations.

Single SQLite file shared by webapp and bot (WAL mode for concurrent
readers + one writer at a time). Schema covers the full data model from
the project plan; business logic modules (clients.py, inventory.py, ...)
are added phase by phase, but the schema is created up front so later
phases only need to add code, not migrate the DB shape.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar

from core import device_catalog

DB_PATH = os.environ.get("CRM_DB_PATH", os.path.join(os.path.dirname(__file__), "..", "crm.sqlite3"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('owner','admin','master','storekeeper')),
    telegram_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT,
    telegram_id INTEGER,
    source TEXT NOT NULL DEFAULT 'offline' CHECK(source IN ('online','offline')),
    notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_clients_phone ON clients(phone);
CREATE INDEX IF NOT EXISTS idx_clients_telegram_id ON clients(telegram_id);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    device_type TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    serial_number TEXT,
    defect_description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS repair_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    client_id INTEGER NOT NULL REFERENCES clients(id),
    master_id INTEGER REFERENCES staff(id),
    status TEXT NOT NULL DEFAULT 'new',
    priority TEXT NOT NULL DEFAULT 'normal',
    price_estimate INTEGER,
    price_final INTEGER,
    channel TEXT NOT NULL DEFAULT 'offline' CHECK(channel IN ('online','offline')),
    warranty_until TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    issued_at TEXT
);

CREATE TABLE IF NOT EXISTS repair_status_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    status TEXT NOT NULL,
    changed_by INTEGER REFERENCES staff(id),
    comment TEXT,
    changed_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Telegram messages posted for a repair order (staff-group card, forum
-- topic card, ...), so a later status change can edit them in place
-- instead of spamming a new message per update.
CREATE TABLE IF NOT EXISTS repair_order_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    has_photo INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_repair_order_messages_order ON repair_order_messages(order_id);

-- Photos staff reply with directly on a repair's card in the group
-- (bot/repair_attachments.py) — a lightweight documentation trail per
-- repair (parts, damage, whatever's worth a photo), not a formal receipt.
CREATE TABLE IF NOT EXISTS repair_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    photo_path TEXT NOT NULL,
    caption TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_repair_attachments_order ON repair_attachments(order_id);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE,
    name TEXT NOT NULL,
    category TEXT,
    unit TEXT NOT NULL DEFAULT 'шт',
    is_repair_part INTEGER NOT NULL DEFAULT 0,
    is_sellable INTEGER NOT NULL DEFAULT 1,
    min_qty INTEGER NOT NULL DEFAULT 0,
    price INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS storage_cells (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    zone TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS stock (
    product_id INTEGER NOT NULL REFERENCES products(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (product_id, cell_id)
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    from_cell_id INTEGER REFERENCES storage_cells(id),
    to_cell_id INTEGER REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    reason TEXT NOT NULL CHECK(reason IN ('receipt','sale','repair_use','adjustment','transfer')),
    ref_type TEXT,
    ref_id INTEGER,
    staff_id INTEGER REFERENCES staff(id),
    comment TEXT,
    unit_cost INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suppliers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    contact TEXT
);

CREATE TABLE IF NOT EXISTS goods_receipts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id INTEGER REFERENCES suppliers(id),
    invoice_no TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goods_receipt_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    receipt_id INTEGER NOT NULL REFERENCES goods_receipts(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    unit_cost INTEGER
);

-- A defective-parts return to whichever supplier delivered them (21.08).
-- Stock from different suppliers of the same product sits mixed in one
-- cell (Павел confirmed — not physically segregated by supplier), so this
-- isn't a precise per-unit trace: staff pick the supplier/receipt from the
-- product's purchase history by memory/judgement (which delivery this
-- batch likely came from) and enter the qty they're holding themselves.
-- receipt_id is nullable — a return can also be logged against a
-- supplier directly with no specific receipt line remembered/found.
CREATE TABLE IF NOT EXISTS supplier_returns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
    receipt_id INTEGER REFERENCES goods_receipts(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    reason TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_supplier_returns_product ON supplier_returns(product_id);
CREATE INDEX IF NOT EXISTS idx_supplier_returns_supplier ON supplier_returns(supplier_id);

-- A photo-of-invoice OCR result, pending human review before it ever
-- touches stock. items_json is a list of {name_guess, qty, unit_cost,
-- product_id} dicts (core.purchase_import.match_items() shape) — kept as
-- JSON rather than a separate items table since a draft is short-lived
-- and gets converted into a real goods_receipts row (or discarded), never
-- queried/reported on independently.
CREATE TABLE IF NOT EXISTS purchase_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    items_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'applied')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER REFERENCES clients(id),
    channel TEXT NOT NULL DEFAULT 'offline' CHECK(channel IN ('online','offline')),
    status TEXT NOT NULL DEFAULT 'new',
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES sales_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    qty INTEGER NOT NULL,
    price INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS device_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_type TEXT NOT NULL,
    brand TEXT NOT NULL,
    model TEXT NOT NULL,
    UNIQUE(device_type, brand, model)
);

-- A barcode-label print request, polled and fulfilled by the small
-- print_agent.py script Павел runs on a Linux box on the same LAN as
-- the Xprinter XP-420B (19.08) — the CRM server itself has no network
-- path to a printer sitting behind a shop/home router, so printing is
-- queue+poll rather than the server pushing to the printer directly.
CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','printed','failed')),
    staff_id INTEGER REFERENCES staff(id),
    error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    printed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_print_jobs_status ON print_jobs(status);

-- Касса (21.08): a single append-only cash ledger, income and expense
-- both. No shift open/close ritual — cash-on-hand is just the running
-- signed sum of every method='cash' row (see core.cash.cash_balance), and
-- a "day" for reporting is a plain calendar date (core.timefmt.kyiv_date_range_utc).
-- income rows link back to what earned the money (ref_type/ref_id, same
-- convention as stock_movements); expense/adjustment rows don't.
CREATE TABLE IF NOT EXISTS cash_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
    method TEXT NOT NULL CHECK(method IN ('cash','card')),
    amount INTEGER NOT NULL,
    category TEXT,
    ref_type TEXT,
    ref_id INTEGER,
    comment TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_cash_transactions_created ON cash_transactions(created_at);

-- Store profile, editable per-store (Фаза A, 23.08 — schema only, no UI
-- yet). One store = one SQLite file, so this is a singleton row (id=1) in
-- each store's own DB, not a shared table — the eventual "Кабинет магазина"
-- screen reads/writes this row for whichever store the request's contextvar
-- currently points at. stores.json (core/stores.py) stays purely
-- infrastructural (db_path + Telegram group ids); this table is the
-- user-facing name/metrics a store owner edits themselves.
CREATE TABLE IF NOT EXISTS store_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT 'Магазин',
    address TEXT,
    phone TEXT,
    working_hours TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Скупка техники у клиентов (24.08) — purpose='parts' is just a log
-- entry (staff disassembles by hand, adds resulting parts via the
-- ordinary Приход flow); purpose='resale' also gets a matching row in
-- products (qty=1, see core.buyback.create_buyback_intake) so the item
-- sells through the existing Продажи pipeline unchanged — product_id
-- stays NULL for purpose='parts'.
CREATE TABLE IF NOT EXISTS buyback_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    device_type TEXT NOT NULL,
    brand TEXT,
    model TEXT,
    serial_number TEXT,
    condition_note TEXT,
    photo_path TEXT,
    purchase_price INTEGER NOT NULL,
    purpose TEXT NOT NULL CHECK(purpose IN ('parts','resale')),
    product_id INTEGER REFERENCES products(id),
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_buyback_orders_client ON buyback_orders(client_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Soft migration: add a column if it doesn't exist yet. Never rely on manual ALTER TABLE."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _ensure_column(conn, "goods_receipt_items", "cell_id", "cell_id INTEGER REFERENCES storage_cells(id)")
        _ensure_column(conn, "sales_orders", "warranty_until", "warranty_until TEXT")
        _ensure_column(conn, "products", "photo_path", "photo_path TEXT")
        _ensure_column(conn, "devices", "photo_path", "photo_path TEXT")
        _ensure_column(conn, "staff", "language", "language TEXT NOT NULL DEFAULT 'ru'")
        _ensure_column(conn, "repair_order_messages", "has_photo", "has_photo INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "sales_orders", "payment_method", "payment_method TEXT")
        _ensure_column(conn, "stock_movements", "unit_cost", "unit_cost INTEGER")
        _ensure_column(conn, "staff", "pay_type", "pay_type TEXT")
        _ensure_column(conn, "staff", "pay_value", "pay_value INTEGER")
        device_catalog.seed(conn)
        conn.execute("INSERT OR IGNORE INTO store_settings (id) VALUES (1)")
        conn.commit()
    finally:
        conn.close()


# Per-request DB path (Фаза A, 23.08): a multi-store deployment has one
# process serving every store, so the DB file to use can't be a fixed
# module constant any more — webapp.main's middleware sets this from the
# request's resolved store before the route runs, and resets it after.
# Default is the legacy DB_PATH, so ~90 existing `get_conn()` call sites
# with no explicit db_path keep working unchanged outside a request context
# (CLI tools, tests, a bare `python -c ...`).
_current_db_path: ContextVar[str] = ContextVar("current_db_path", default=DB_PATH)


def set_current_db_path(path: str):
    """Returns a token; pass it to reset_current_db_path() when done (use try/finally)."""
    return _current_db_path.set(path)


def reset_current_db_path(token) -> None:
    _current_db_path.reset(token)


@contextmanager
def get_conn(db_path: str | None = None):
    path = db_path or _current_db_path.get()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
