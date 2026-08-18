"""Best-effort parsing of a pasted invoice (from Excel/a supplier's sheet)
into draft goods-receipt rows. Never writes to the DB — only proposes a
match against the existing product catalog for a human to review/fix on
the purchases_list.html form (same "🆕 новый товар" flow as manual intake
for anything that doesn't match)."""
from __future__ import annotations

import re
import sqlite3

_COLUMN_SPLIT_RE = re.compile(r"\t|;| {2,}")
_NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _parse_number(token: str) -> int | None:
    token = token.strip().replace(" ", "")
    if not _NUMBER_RE.fullmatch(token):
        return None
    return int(float(token.replace(",", ".")))


def _extract_numbers(tokens: list[str]) -> list[int]:
    numbers = []
    for token in tokens:
        n = _parse_number(token)
        if n is not None:
            numbers.append(n)
    return numbers


def _match_product(name_guess: str, products: list[sqlite3.Row]) -> int | None:
    """Exact SKU match, then exact name match, then substring match
    (preferring the most specific/longest product name) — no fuzzy-match
    library, the local catalog is small and mostly exact-ish text."""
    needle = name_guess.strip().lower()
    if not needle:
        return None

    for p in products:
        if p["sku"] and p["sku"].strip().lower() == needle:
            return p["id"]

    for p in products:
        if p["name"].strip().lower() == needle:
            return p["id"]

    candidates = [p for p in products if p["name"].lower() in needle or needle in p["name"].lower()]
    if candidates:
        candidates.sort(key=lambda p: len(p["name"]), reverse=True)
        return candidates[0]["id"]

    return None


def parse_invoice_text(conn: sqlite3.Connection, text: str) -> list[dict]:
    """Split pasted invoice text line by line (tab/`;`/2+ spaces as column
    separators — how a paste from Excel/Google Sheets usually looks) and
    try to match each line's first column against the product catalog.
    `product_id` is None when nothing matched — the caller (purchases_list.html)
    flags that row for the same inline quick-add-product flow as manual intake."""
    products = conn.execute("SELECT id, name, sku FROM products WHERE active = 1").fetchall()

    rows = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = [p.strip() for p in _COLUMN_SPLIT_RE.split(line) if p.strip()]
        if not parts:
            continue

        name_guess = parts[0]
        numbers = _extract_numbers(parts[1:])
        qty = numbers[0] if len(numbers) >= 1 else None
        unit_cost = numbers[1] if len(numbers) >= 2 else None

        rows.append({
            "raw": raw_line,
            "name_guess": name_guess,
            "qty": qty,
            "unit_cost": unit_cost,
            "product_id": _match_product(name_guess, products),
        })
    return rows


def match_items(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Resolve product_id for already-structured items — e.g. from
    core.vision_ocr's photo OCR, which returns {"name", "qty",
    "unit_cost"} dicts directly rather than raw text to split into
    columns. Same catalog-matching heuristic as parse_invoice_text(),
    just skipping the line-splitting step."""
    products = conn.execute("SELECT id, name, sku FROM products WHERE active = 1").fetchall()
    return [
        {
            "name_guess": (item.get("name") or "").strip(),
            "qty": item.get("qty"),
            "unit_cost": item.get("unit_cost"),
            "product_id": _match_product(item.get("name") or "", products),
        }
        for item in items
    ]
