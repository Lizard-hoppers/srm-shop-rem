"""SQLite storage layer: schema, connection helper, soft migrations.

Single SQLite file shared by webapp and bot (WAL mode for concurrent
readers + one writer at a time) — and, since 06.10.2026, by every точка of
the business too: the earlier one-file-per-store layout (Фаза A, 23.08) was
merged into this one base, with `locations` as rows instead of files (see
OPERATIONS.md «Единая база»). Schema covers the full data model from
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
from core import accounts as _accounts
from core import documents as _documents

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

-- Заметки по ремонту из рабочего чата (09.10): a text or voice message
-- sent in reply to the repair's card (core.repair_notes). original_text
-- is what was written / what the voice message was transcribed as;
-- summary — the short line a language model made of it (NULL when there
-- is none — the original is shown then). chat_id + message_id are the
-- message itself: a reply to it lands in the same repair, and a
-- redelivered update doesn't make a second note.
CREATE TABLE IF NOT EXISTS repair_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES repair_orders(id),
    kind TEXT NOT NULL DEFAULT 'text',
    original_text TEXT NOT NULL,
    summary TEXT,
    staff_id INTEGER REFERENCES staff(id),
    author_name TEXT,
    chat_id TEXT,
    message_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_repair_notes_order ON repair_notes(order_id);
CREATE INDEX IF NOT EXISTS idx_repair_notes_message ON repair_notes(chat_id, message_id);

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

-- Витрина в Telegram-канале (02.10): one row per product card the bot
-- posted to the store's sales channel (store_settings.sales_channel), so
-- the card can be edited (price change) or taken down (sold) later — see
-- core.channel_posts. status: 'active' (live card), 'removed' (message
-- deleted from the channel), 'sold' (couldn't delete — Telegram only lets
-- a bot delete a message for ~48h — so the card was edited to a ПРОДАНО
-- stub instead). caption is the last text actually sent, so a sync that
-- changes nothing skips the API call.
CREATE TABLE IF NOT EXISTS channel_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    chat_id TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    has_photo INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','sold','removed')),
    caption TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_posts_product ON channel_posts(product_id);

-- A customer tapping «Купить» under a channel card (bot/channel_orders.py).
-- Kept so a second tap by the same person on the same product doesn't
-- re-notify staff, and so there's a trail of who asked about what.
CREATE TABLE IF NOT EXISTS channel_leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    telegram_id INTEGER NOT NULL,
    name TEXT,
    username TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(product_id, telegram_id)
);

-- Точки бизнеса (06.10): one row per физическая точка. Replaces both
-- stores.json (Telegram group ids) and the per-store store_settings
-- singleton (name/address/phone/hours/sales_channel) — one base for the
-- whole business now, a точка is a row here and a location_id on whatever
-- happened there. core.stores still hands these out as StoreConfig so the
-- rest of the code keeps saying "store".
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL DEFAULT 'Магазин',
    address TEXT,
    phone TEXT,
    working_hours TEXT,
    sales_channel TEXT,
    staff_group_chat_id INTEGER,
    repair_topic_id INTEGER,
    masters_group_chat_id INTEGER,
    sales_topic_id INTEGER,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Склады: where stock physically is and who answers for it. 'point' — a
-- точка's own stock (one per location, created automatically); 'master' —
-- a master's материально-ответственный склад (staff_id set, location_id
-- NULL: a master working from home belongs to no точка); 'transit' — the
-- single «В пути» склад a transfer sits in between «Передал» and «Принял».
-- A storage cell belongs to exactly one склад (storage_cells.warehouse_id),
-- which is how every stock figure gets its точка.
CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('point','master','transit')),
    location_id INTEGER REFERENCES locations(id),
    staff_id INTEGER REFERENCES staff(id),
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Единый журнал документов (06.10) — one row per thing that happened
-- (ремонт, продажа, приход, покупка, списание, расход из кассы…), across
-- every точка and every employee: core.documents. The business tables keep
-- holding the substance (repair_orders, sales_orders, …); this row is the
-- common envelope — номер, точка, автор, время, контрагент, сумма, статус —
-- that makes one chronological feed possible. A проведённый document is
-- never deleted: status flips to 'cancelled' with a reason, by whom and
-- when (core.doc_cancel reverses its stock/money effects). idempotency_key
-- is what makes a repeated tap/submit land on the same document instead
-- of creating a second one.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,
    number INTEGER NOT NULL,
    location_id INTEGER REFERENCES locations(id),
    staff_id INTEGER REFERENCES staff(id),
    client_id INTEGER REFERENCES clients(id),
    ref_table TEXT,
    ref_id INTEGER,
    title TEXT,
    amount INTEGER,
    currency TEXT NOT NULL DEFAULT 'UAH',
    profit INTEGER,
    status TEXT NOT NULL DEFAULT 'posted' CHECK(status IN ('posted','cancelled')),
    cancel_reason TEXT,
    cancelled_by INTEGER REFERENCES staff(id),
    cancelled_at TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(doc_type, number)
);
CREATE INDEX IF NOT EXISTS idx_documents_created ON documents(created_at);
CREATE INDEX IF NOT EXISTS idx_documents_ref ON documents(doc_type, ref_id);
CREATE INDEX IF NOT EXISTS idx_documents_client ON documents(client_id);

-- Фото купленного устройства (Заход 4): up to six per покупка, one per
-- side (core.buyback.PHOTO_SLOTS). buyback_orders.photo_path keeps the
-- first one for the list thumbnail.
CREATE TABLE IF NOT EXISTS buyback_photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES buyback_orders(id),
    position INTEGER NOT NULL,
    label TEXT,
    path TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buyback_photos_order ON buyback_photos(order_id);

-- Партии (Заход 3, 06.10): «один SKU — много партий». A партия is one
-- arrival of a product: which supplier, which приход document, what it
-- cost (in what currency, at what rate). Stock keeps its origin all the
-- way through — перемещение, ремонт, продажа, возврат each take from a
-- specific партия and snapshot its cost. A serial item (телефон) is a
-- партия of exactly one unit carrying its IMEI. `stock` stays as the
-- per-cell total every screen already reads; batch_stock is the same
-- quantity broken down by партия, and core.inventory keeps the two equal.
CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    supplier_id INTEGER REFERENCES suppliers(id),
    receipt_id INTEGER REFERENCES goods_receipts(id),
    source TEXT NOT NULL DEFAULT 'manual',
    unit_cost REAL,
    currency TEXT NOT NULL DEFAULT 'UAH',
    rate REAL NOT NULL DEFAULT 1,
    unit_cost_uah REAL,
    imei TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_batches_product ON batches(product_id);
CREATE INDEX IF NOT EXISTS idx_batches_imei ON batches(imei);

CREATE TABLE IF NOT EXISTS batch_stock (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (batch_id, cell_id)
);

-- Резерв (Заход 5): «доступно = физический остаток − резерв». A quantity
-- of a specific партия in a specific cell held for one document (a
-- производство order's parts and phone now; a client's заказ in Заход 6).
-- Reserved stock is still physically there and still counted in `stock`,
-- but nothing except the document it is held for can take it
-- (core.inventory.record_movement). A row stops holding when released_at
-- is set or expires_at has passed.
CREATE TABLE IF NOT EXISTS stock_reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    released_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_reservations_batch ON stock_reservations(batch_id, cell_id);
CREATE INDEX IF NOT EXISTS idx_stock_reservations_ref ON stock_reservations(ref_type, ref_id);

-- Взаиморасчёты с контрагентом (Заход 6): one signed row per thing that
-- changes what a client and the business owe each other. amount > 0 —
-- the client owes us more (we sold him something, we handed him money);
-- amount < 0 — he owes us less / we owe him (he paid, we bought from
-- him). The sum of live rows is his balance: > 0 «нам должны», < 0 «мы
-- должны» (an аванс is just that). Rows are cancelled with their
-- document, never deleted.
CREATE TABLE IF NOT EXISTS client_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    amount REAL NOT NULL,
    kind TEXT NOT NULL,
    ref_type TEXT,
    ref_id INTEGER,
    location_id INTEGER REFERENCES locations(id),
    staff_id INTEGER REFERENCES staff(id),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_client_ledger_client ON client_ledger(client_id);
CREATE INDEX IF NOT EXISTS idx_client_ledger_ref ON client_ledger(ref_type, ref_id);

-- Выплаты мастерам (Заход 6): «начислено − выплачено = мы должны мастеру».
CREATE TABLE IF NOT EXISTS master_payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    amount REAL NOT NULL,
    location_id INTEGER REFERENCES locations(id),
    paid_by INTEGER REFERENCES staff(id),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_master_payouts_staff ON master_payouts(staff_id);

-- Заказ клиента (Заход 6): goods we have are held for a client for 24
-- hours («резерв»), can be paid for («Оплатить») and handed over
-- («Выдать») as two separate steps. status: reserved → issued (became
-- the sale sale_id) | expired (the hold ran out) | cancelled.
CREATE TABLE IF NOT EXISTS client_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL REFERENCES clients(id),
    location_id INTEGER REFERENCES locations(id),
    status TEXT NOT NULL DEFAULT 'reserved',
    reserved_until TEXT,
    sale_id INTEGER REFERENCES sales_orders(id),
    comment TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_client_orders_client ON client_orders(client_id);

CREATE TABLE IF NOT EXISTS client_order_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES client_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    price REAL NOT NULL
);

-- Начисления мастерам (Заход 5): what the business owes a master for work
-- done — his share of a client repair's profit, the fixed price of an
-- internal job, parts he supplied himself. One row per document that
-- earned it; «начислено − выплачено = мы должны мастеру» (payouts arrive
-- in Заход 6). A row is cancelled, never deleted, when its document is
-- undone.
CREATE TABLE IF NOT EXISTS master_accruals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    cancelled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_master_accruals_staff ON master_accruals(staff_id);
CREATE INDEX IF NOT EXISTS idx_master_accruals_ref ON master_accruals(ref_type, ref_id);

-- Производство / наш ремонт (Заход 5): a phone the business owns goes to
-- a master with parts from our склады and comes back worth more — «не
-- создаёт прибыль, формирует фактическую себестоимость готового товара».
-- status: draft (детали подбираются и резервируются) → in_work (телефон и
-- детали на складе мастера) → reported (мастер отчитался, что
-- использовал и что добавил своего) → done (результат принят: расход
-- деталей, новая себестоимость, начисление мастеру, возврат на точку) |
-- cancelled. The cost_* columns are the «Результат» card, fixed at accept.
CREATE TABLE IF NOT EXISTS production_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    buyback_order_id INTEGER REFERENCES buyback_orders(id),
    master_id INTEGER REFERENCES staff(id),
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','in_work','reported','done','cancelled')),
    task TEXT,
    work_price REAL,
    report_note TEXT,
    cost_before REAL,
    cost_parts REAL,
    cost_extras REAL,
    cost_work REAL,
    cost_after REAL,
    return_transfer_id INTEGER REFERENCES stock_transfers(id),
    created_by INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    handed_at TEXT,
    reported_at TEXT,
    done_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_production_orders_status ON production_orders(status);
CREATE INDEX IF NOT EXISTS idx_production_orders_batch ON production_orders(batch_id);

-- Our parts picked for a производство order: a specific партия from a
-- specific cell. cell_id follows the part (source cell while reserved,
-- the master's cell after handover); used_qty is the master's report.
CREATE TABLE IF NOT EXISTS production_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES production_orders(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    used_qty INTEGER
);
CREATE INDEX IF NOT EXISTS idx_production_parts_order ON production_parts(order_id);

-- What the master added of his own to a производство order — «Шлейф
-- мастера — 400 грн», «Проклейка мастера — 100 грн»: goes into the
-- phone's cost and into what we owe him.
CREATE TABLE IF NOT EXISTS production_extras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES production_orders(id),
    title TEXT NOT NULL,
    amount REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_production_extras_order ON production_extras(order_id);

-- Перемещение товара между складами: «Отправил → В пути → Принял». Sending
-- moves the goods into the «В пути» склад at once (not sellable, nobody's
-- yet); they reach the destination only when its keeper — the master
-- himself for a master's склад — confirms what actually arrived
-- (received_qty per line; a shortfall stays «в пути» with a note until an
-- owner settles it). Cell-to-cell inside one склад needs none of this.
CREATE TABLE IF NOT EXISTS stock_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    to_warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    status TEXT NOT NULL DEFAULT 'sent' CHECK(status IN ('sent','received','cancelled')),
    comment TEXT,
    sent_by INTEGER REFERENCES staff(id),
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    received_by INTEGER REFERENCES staff(id),
    received_at TEXT,
    discrepancy_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_stock_transfers_status ON stock_transfers(status);

CREATE TABLE IF NOT EXISTS stock_transfer_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id INTEGER NOT NULL REFERENCES stock_transfers(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    from_cell_id INTEGER NOT NULL REFERENCES storage_cells(id),
    to_cell_id INTEGER REFERENCES storage_cells(id),
    qty INTEGER NOT NULL,
    received_qty INTEGER
);
CREATE INDEX IF NOT EXISTS idx_stock_transfer_items_transfer ON stock_transfer_items(transfer_id);

-- Денежные счета (Заход 2, 06.10): «не одна касса, а набор независимых
-- балансов» — every точка has its own set (наличные UAH/USD/EUR, ФОП,
-- карта, USDT by default; owner/admin add, rename and switch off their own
-- from Касса → Счета). A balance is never stored: it is the signed sum of
-- the account's live cash_transactions rows (core.accounts.balance), each
-- of which belongs to a document — «баланс нельзя исправлять вручную».
CREATE TABLE IF NOT EXISTS money_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    kind TEXT NOT NULL CHECK(kind IN ('cash','fop','card','crypto')),
    currency TEXT NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    sort INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_money_accounts_location ON money_accounts(location_id);

-- Смены: an employee opens their shift at a точка by looking at its
-- balances and saying «всё совпадает» or «есть расхождение» (the note is
-- kept — a discrepancy is a signal for the owner, it doesn't fix anything
-- by itself). snapshot_json is what the balances were at that moment.
CREATE TABLE IF NOT EXISTS shifts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    opened_at TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at TEXT,
    discrepancy_note TEXT,
    snapshot_json TEXT,
    closing_snapshot_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_shifts_staff ON shifts(staff_id, location_id);

-- Обмен валют: money leaves one account and arrives in another of the same
-- точка at a rate — two cash_transactions rows (category 'exchange') tied
-- to this row. Not income and not an expense of the business.
CREATE TABLE IF NOT EXISTS money_exchanges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id INTEGER NOT NULL REFERENCES locations(id),
    from_account_id INTEGER NOT NULL REFERENCES money_accounts(id),
    to_account_id INTEGER NOT NULL REFERENCES money_accounts(id),
    amount_from REAL NOT NULL,
    amount_to REAL NOT NULL,
    rate REAL NOT NULL,
    comment TEXT,
    staff_id INTEGER REFERENCES staff(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Перемещение денег between two accounts of the same currency. Between
-- точки it is «Отправил → В пути → Принял»: the money leaves the source at
-- once (status 'sent'), and only reaches the destination when someone
-- there confirms the amount they actually counted (received_amount; a
-- difference is kept as discrepancy_note, never silently absorbed).
-- Within one точка it is received in the same step.
CREATE TABLE IF NOT EXISTS money_transfers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_account_id INTEGER NOT NULL REFERENCES money_accounts(id),
    to_account_id INTEGER NOT NULL REFERENCES money_accounts(id),
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'sent' CHECK(status IN ('sent','received','cancelled')),
    comment TEXT,
    sent_by INTEGER REFERENCES staff(id),
    sent_at TEXT NOT NULL DEFAULT (datetime('now')),
    received_by INTEGER REFERENCES staff(id),
    received_at TEXT,
    received_amount REAL,
    discrepancy_note TEXT
);
CREATE INDEX IF NOT EXISTS idx_money_transfers_status ON money_transfers(status);

CREATE TABLE IF NOT EXISTS document_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    event TEXT NOT NULL,
    staff_id INTEGER REFERENCES staff(id),
    comment TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_document_events_doc ON document_events(document_id);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Soft migration: add a column if it doesn't exist yet. Never rely on manual ALTER TABLE."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


# Every table that records something happening AT a точка. NULL means "written
# before точки existed" (or by a caller that passed none) and is folded into
# the first точка on the next init_db — see _backfill_locations.
_LOCATION_SCOPED_TABLES = (
    "repair_orders", "sales_orders", "goods_receipts", "buyback_orders", "cash_transactions",
)


def _optional_int_env(name: str) -> int | None:
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else None


def _seed_first_location(conn: sqlite3.Connection) -> None:
    """A base with no точки yet gets its first one out of what it already
    knew about itself: the old store_settings singleton (name, address,
    sales channel…) plus the legacy CRM_*_GROUP_CHAT_ID env vars — so a
    single-store deployment upgrades with nothing to configure by hand."""
    if conn.execute("SELECT 1 FROM locations LIMIT 1").fetchone():
        return
    old = conn.execute("SELECT * FROM store_settings WHERE id = 1").fetchone()
    conn.execute(
        """INSERT INTO locations (name, address, phone, working_hours, sales_channel,
                                  staff_group_chat_id, repair_topic_id, masters_group_chat_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            old["name"] if old else "Магазин",
            old["address"] if old else None,
            old["phone"] if old else None,
            old["working_hours"] if old else None,
            old["sales_channel"] if old else None,
            _optional_int_env("CRM_STAFF_GROUP_CHAT_ID"),
            _optional_int_env("CRM_REPAIR_TOPIC_ID"),
            _optional_int_env("CRM_MASTERS_GROUP_CHAT_ID"),
        ),
    )


def ensure_warehouses(conn: sqlite3.Connection) -> None:
    """Every точка has its own 'point' склад, and there is exactly one
    «В пути». Idempotent — also called after a new точка is added."""
    for loc in conn.execute("SELECT id FROM locations").fetchall():
        exists = conn.execute(
            "SELECT 1 FROM warehouses WHERE kind = 'point' AND location_id = ?", (loc["id"],)
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO warehouses (kind, location_id, name) VALUES ('point', ?, 'Основной склад')",
                (loc["id"],),
            )
    if not conn.execute("SELECT 1 FROM warehouses WHERE kind = 'transit'").fetchone():
        conn.execute("INSERT INTO warehouses (kind, name) VALUES ('transit', 'В пути')")


def _backfill_locations(conn: sqlite3.Connection) -> None:
    first = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()["id"]
    for table in _LOCATION_SCOPED_TABLES:
        conn.execute(f"UPDATE {table} SET location_id = ? WHERE location_id IS NULL", (first,))
    first_warehouse = conn.execute(
        "SELECT id FROM warehouses WHERE kind = 'point' AND location_id = ?", (first,)
    ).fetchone()["id"]
    conn.execute("UPDATE storage_cells SET warehouse_id = ? WHERE warehouse_id IS NULL", (first_warehouse,))


TRANSIT_CELL_CODE = "В-ПУТИ"


def master_cell_code(staff_id: int) -> str:
    return f"МАСТЕР-{staff_id}"


def ensure_master_warehouse(conn: sqlite3.Connection, staff_id: int, name: str) -> int:
    """A master's материально-ответственный склад, with its one cell.
    Idempotent; returns the warehouse id. Stock only ever lives in cells,
    so a склад without shelves of its own (a master's, «В пути») gets
    exactly one, named after it."""
    row = conn.execute(
        "SELECT id FROM warehouses WHERE kind = 'master' AND staff_id = ?", (staff_id,)
    ).fetchone()
    if row:
        warehouse_id = row["id"]
        conn.execute("UPDATE warehouses SET name = ? WHERE id = ?", (name, warehouse_id))
    else:
        warehouse_id = conn.execute(
            "INSERT INTO warehouses (kind, staff_id, name) VALUES ('master', ?, ?)", (staff_id, name)
        ).lastrowid
    if not conn.execute("SELECT 1 FROM storage_cells WHERE warehouse_id = ?", (warehouse_id,)).fetchone():
        conn.execute(
            "INSERT INTO storage_cells (code, note, warehouse_id) VALUES (?, 'Склад мастера', ?)",
            (master_cell_code(staff_id), warehouse_id),
        )
    return warehouse_id


def _ensure_service_cells(conn: sqlite3.Connection) -> None:
    """The «В пути» склад's single cell, and a склад for every master
    already on the books."""
    transit = conn.execute("SELECT id FROM warehouses WHERE kind = 'transit'").fetchone()
    if transit and not conn.execute(
        "SELECT 1 FROM storage_cells WHERE warehouse_id = ?", (transit["id"],)
    ).fetchone():
        conn.execute(
            "INSERT INTO storage_cells (code, note, warehouse_id) VALUES (?, 'Товар в пути между складами', ?)",
            (TRANSIT_CELL_CODE, transit["id"]),
        )
    for master in conn.execute("SELECT id, name FROM staff WHERE role = 'master'").fetchall():
        ensure_master_warehouse(conn, master["id"], master["name"])


def _backfill_batches(conn: sqlite3.Connection) -> None:
    """Stock that was on the shelves before партии existed has no origin
    on record — each such (product, cell) remainder becomes one 'legacy'
    партия priced at the product's last known purchase cost, so that
    batch_stock adds up to stock everywhere from day one."""
    rows = conn.execute(
        """SELECT stock.product_id, stock.cell_id, stock.qty,
                  COALESCE((SELECT SUM(bs.qty) FROM batch_stock bs JOIN batches b ON b.id = bs.batch_id
                            WHERE b.product_id = stock.product_id AND bs.cell_id = stock.cell_id), 0) AS covered
           FROM stock WHERE stock.qty > 0"""
    ).fetchall()
    for row in rows:
        gap = row["qty"] - row["covered"]
        if gap <= 0:
            continue
        cost = conn.execute(
            """SELECT goods_receipt_items.unit_cost FROM goods_receipt_items
               JOIN goods_receipts ON goods_receipts.id = goods_receipt_items.receipt_id
               WHERE goods_receipt_items.product_id = ? AND goods_receipt_items.unit_cost IS NOT NULL
               ORDER BY goods_receipts.created_at DESC, goods_receipts.id DESC LIMIT 1""",
            (row["product_id"],),
        ).fetchone()
        unit_cost = cost["unit_cost"] if cost else None
        batch_id = conn.execute(
            "INSERT INTO batches (product_id, source, unit_cost, unit_cost_uah) VALUES (?, 'legacy', ?, ?)",
            (row["product_id"], unit_cost, unit_cost),
        ).lastrowid
        conn.execute(
            "INSERT INTO batch_stock (batch_id, cell_id, qty) VALUES (?, ?, ?)", (batch_id, row["cell_id"], gap)
        )


def init_db(db_path: str = DB_PATH) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
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
        _ensure_column(conn, "store_settings", "sales_channel", "sales_channel TEXT")
        _ensure_column(conn, "products", "description", "description TEXT")
        # Единая база (06.10): точка on everything that happens at one, склад
        # on every cell, and the контрагент links (one person by phone can be
        # a client, a master/employee and a supplier at once — clients is the
        # контрагент table, staff/suppliers point into it).
        for table in _LOCATION_SCOPED_TABLES:
            _ensure_column(conn, table, "location_id", "location_id INTEGER REFERENCES locations(id)")
        _ensure_column(conn, "storage_cells", "warehouse_id", "warehouse_id INTEGER REFERENCES warehouses(id)")
        _ensure_column(conn, "staff", "location_id", "location_id INTEGER REFERENCES locations(id)")
        _ensure_column(conn, "staff", "phone", "phone TEXT")
        _ensure_column(conn, "staff", "client_id", "client_id INTEGER REFERENCES clients(id)")
        _ensure_column(conn, "suppliers", "client_id", "client_id INTEGER REFERENCES clients(id)")
        _ensure_column(conn, "cash_transactions", "cancelled_at", "cancelled_at TEXT")
        # Заход 2: every money row sits on a specific account; amount is in
        # that account's currency, amount_uah is what it was worth in гривня
        # at the operation's own rate (what documents and reports add up).
        _ensure_column(conn, "cash_transactions", "account_id", "account_id INTEGER REFERENCES money_accounts(id)")
        _ensure_column(conn, "cash_transactions", "amount_uah", "amount_uah REAL")
        _ensure_column(conn, "cash_transactions", "rate", "rate REAL")
        # Заход 3: партии. is_serial marks a product tracked unit by unit
        # (IMEI); movements and sale lines remember which партия they took.
        _ensure_column(conn, "products", "is_serial", "is_serial INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "stock_movements", "batch_id", "batch_id INTEGER REFERENCES batches(id)")
        _ensure_column(conn, "goods_receipts", "currency", "currency TEXT NOT NULL DEFAULT 'UAH'")
        _ensure_column(conn, "goods_receipts", "rate", "rate REAL NOT NULL DEFAULT 1")
        _ensure_column(conn, "sales_order_items", "batch_id", "batch_id INTEGER REFERENCES batches(id)")
        _ensure_column(conn, "sales_order_items", "unit_cost", "unit_cost REAL")
        # Заход 4: покупка телефона — the price as agreed (its currency and
        # rate), its гривня value, the IMEI and the партия the phone became.
        _ensure_column(conn, "buyback_orders", "imei", "imei TEXT")
        _ensure_column(conn, "buyback_orders", "currency", "currency TEXT NOT NULL DEFAULT 'UAH'")
        _ensure_column(conn, "buyback_orders", "rate", "rate REAL NOT NULL DEFAULT 1")
        _ensure_column(conn, "buyback_orders", "purchase_price_uah", "purchase_price_uah REAL")
        _ensure_column(conn, "buyback_orders", "batch_id", "batch_id INTEGER REFERENCES batches(id)")
        _ensure_column(conn, "locations", "buyback_topic_id", "buyback_topic_id INTEGER")
        # Помощник (Заход 7): the daily «Проблемы» digest into the точка's
        # staff group — off until switched on in «Кабинет магазина» — and
        # the Kyiv date it last went out.
        _ensure_column(conn, "locations", "assistant_digest", "assistant_digest INTEGER NOT NULL DEFAULT 0")
        _ensure_column(conn, "locations", "assistant_last_digest", "assistant_last_digest TEXT")
        # Заход 5: a master is штатный or аутсорс and has skills on record;
        # a client repair remembers that its master said «без запчасти».
        _ensure_column(conn, "staff", "master_kind", "master_kind TEXT NOT NULL DEFAULT 'staff'")
        _ensure_column(conn, "staff", "skills", "skills TEXT")
        _ensure_column(conn, "repair_orders", "no_parts", "no_parts INTEGER NOT NULL DEFAULT 0")
        try:
            # One phone = one контрагент. Partial: a walk-in with no phone
            # on record is fine, any number of them.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_clients_phone_unique ON clients(phone) WHERE phone IS NOT NULL"
            )
        except sqlite3.IntegrityError:
            # Existing duplicates — leave the base usable; they have to be
            # merged by hand before the rule can be enforced.
            pass
        device_catalog.seed(conn)
        conn.execute("INSERT OR IGNORE INTO store_settings (id) VALUES (1)")
        _seed_first_location(conn)
        ensure_warehouses(conn)
        _backfill_locations(conn)
        _ensure_service_cells(conn)
        _backfill_batches(conn)
        _accounts.ensure_default_accounts(conn)
        _accounts.backfill_transactions(conn)
        _documents.backfill(conn)
        # Local import: settlements sits above storage in the import
        # order (it uses cash → accounts → …), storage only calls it here.
        from core import settlements as _settlements

        _settlements.backfill(conn)
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
