"""Reading a смета a supplier dropped into his chat (10.10).

Whatever it came as — Excel, PDF, a photo, a Word file, plain text — ends
up as the same thing: `{"items": [{"name", "qty", "price"}], "total",
"currency", "lines_total"}`, or None when the file isn't a смета at all
(a photo of a part, a warranty card).

Some suppliers print every price twice — in dollars and in гривня, two
columns side by side, often with the header cut off the photo (12.10).
Then the смета is in the foreign currency, each item keeps the гривня
price as `price2`, and the reading carries `totals` ({"USD": …, "UAH": …})
and the `rate` the supplier counted by. Which column is which is decided
here by the numbers, not by the model: see `_two_columns`.

The model only transcribes: names, quantities, prices, the printed total,
the currency. Sums are code's job — `lines_total` is qty × price added up
here, shown next to the printed total when they disagree, so a misread
digit is visible before anybody agrees to pay it. Nothing here writes to
the base; the amount to pay is confirmed by a person afterwards
(core.supplier_accounts).

Excel and Word are unpacked with the standard library (they are zip
archives of XML) — no extra packages on the server. A PDF goes to the
model as a file; the old binary .xls can't be read and is refused by name.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import zipfile
from io import BytesIO
from xml.etree import ElementTree

import httpx

from core import vision_ocr
from core.accounts import money

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 15 * 1024 * 1024   # Telegram hands bots files up to 20 MB
MAX_TEXT = 14_000
MAX_ITEMS = 200
CURRENCIES = ("UAH", "USD", "EUR", "USDT")
IMAGE_TYPES = (".jpg", ".jpeg", ".png", ".webp")
TEXT_TYPES = (".txt", ".csv", ".tsv")
READABLE = (*IMAGE_TYPES, *TEXT_TYPES, ".xlsx", ".docx", ".pdf")

_PROMPT = (
    "Это файл от поставщика запчастей магазину электроники: смета, счёт или накладная — либо что-то другое. "
    "Верни СТРОГО JSON: {\"is_invoice\": true|false, \"items\": [{\"name\": \"...\", \"qty\": число|null, "
    "\"price\": число|null, \"price2\": число|null}], \"total\": число|null, \"total2\": число|null, "
    "\"currency\": \"UAH\"|\"USD\"|\"EUR\"|\"USDT\"|null, \"currency2\": \"UAH\"|\"USD\"|\"EUR\"|\"USDT\"|null}. "
    "is_invoice=true только если есть позиции с ценами или итоговая сумма к оплате. "
    "name — как в файле, без сокращений. price — цена ЗА ОДНУ штуку. total — итог, НАПЕЧАТАННЫЙ в файле "
    "(«Итого», «Всего», «Разом», «Total»); если его нет — null, сам не считай. "
    "currency: грн/₴/UAH → UAH, $/usd/у.е. → USD, €/eur → EUR, usdt → USDT; если валюта не указана — null. "
    "Если у позиции ДВЕ колонки с ценой — одна и та же цена в двух валютах (обычно в долларах и в гривне), "
    "даже если колонки не подписаны, — то price и total берутся из первой (левой) колонки, price2 и total2 — "
    "из второй, currency и currency2 — их валюты, если они подписаны. Колонка «Сумма» (количество × цена) — "
    "не вторая цена, в price2 её не клади. Колонка цен одна — price2, total2 и currency2 = null. "
    "Чего не видно чётко — null, ничего не выдумывай."
)


# A table of small digits wants the full-size model: the «mini» one read
# only one of two price columns, and not the same way twice (12.10).
_MODEL = os.environ.get("SUPPLIER_FILE_MODEL", "gpt-4.1")

# How many гривень a dollar or a euro can cost: two price columns that
# differ by this much — and by the same much on every line — are one price
# in a currency and in гривня, whether or not anything is labelled.
_RATE_BAND = (20, 90)
_FOREIGN = ("USD", "EUR", "USDT")


class FileError(Exception):
    """The file can't be read — message is shown in the chat as-is."""


def kind_of(file_name: str | None) -> str:
    return os.path.splitext(file_name or "")[1].lower()


def _xlsx_text(data: bytes) -> str:
    """Every sheet's cells, row by row, tab-separated."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(t.text or "" for t in item.iter("{%s}t" % ns["m"])) for item in root.findall("m:si", ns)]
        lines: list[str] = []
        for name in sorted(n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)):
            for row in ElementTree.fromstring(archive.read(name)).iter("{%s}row" % ns["m"]):
                cells = []
                for cell in row.findall("m:c", ns):
                    value = cell.find("m:v", ns)
                    if cell.get("t") == "inlineStr":
                        text = "".join(t.text or "" for t in cell.iter("{%s}t" % ns["m"]))
                    elif value is None or value.text is None:
                        text = ""
                    elif cell.get("t") == "s":
                        text = shared[int(value.text)] if value.text.isdigit() and int(value.text) < len(shared) else ""
                    else:
                        text = value.text
                    cells.append(text.strip())
                if any(cells):
                    lines.append("\t".join(cells))
    return "\n".join(lines)


def _docx_text(data: bytes) -> str:
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    with zipfile.ZipFile(BytesIO(data)) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    lines = []
    for paragraph in root.iter(w + "p"):
        text = "".join(node.text or "" for node in paragraph.iter(w + "t")).strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _plain_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _prompt(hint: str | None) -> str:
    """The prompt, plus what the person wrote under the file — «количество
    / $ / грн» says more about the columns than a cropped photo does."""
    hint = " ".join((hint or "").split())[:300]
    return _PROMPT + (f" Подпись к файлу от того, кто его прислал (подсказка о колонках и валюте): «{hint}»." if hint else "")


def _ask(content, hint: str | None = None) -> dict:
    """One JSON answer from the model for a text or a PDF."""
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise FileError("Чтение файлов не настроено (нет ключа OpenAI).")
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": _MODEL, "max_tokens": 4000, "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": _prompt(hint)}, {"role": "user", "content": content}],
            },
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise FileError("Не удалось связаться с сервисом чтения файлов — пришлите файл ещё раз чуть позже.") from exc
    if resp.status_code != 200:
        logger.warning("supplier file: model answered %s %s", resp.status_code, resp.text[:300])
        raise FileError("Сервис чтения файлов ответил ошибкой — пришлите файл ещё раз чуть позже.")
    try:
        return json.loads(resp.json()["choices"][0]["message"]["content"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FileError("Не смог разобрать файл — пришлите его в другом виде (xlsx, pdf или фото).") from exc


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 < number < 100_000_000 else None


def _two_columns(items: list[dict], currency: str, currency2: str) -> tuple[str, str, bool] | None:
    """Two price columns that are one price in a foreign currency and in
    гривня -> (foreign currency, "UAH", гривня came first). None when the
    second column is something else (a «сумма», a retail price): then it
    is dropped. The test is the numbers themselves — every line must give
    the same rate, and that rate must be a believable one."""
    priced = [item for item in items if item["price"]]
    if not priced or any(not item.get("price2") for item in priced):
        return None
    rates = [item["price2"] / item["price"] for item in priced]
    if max(rates) > min(rates) * 1.03:
        return None
    low, high = _RATE_BAND
    if low <= rates[0] <= high:
        return (currency if currency in _FOREIGN else "USD"), "UAH", False
    if low <= 1 / rates[0] <= high:
        return (currency2 if currency2 in _FOREIGN else "USD"), "UAH", True
    return None


def tidy(raw: dict) -> dict | None:
    """The model's answer -> the смета, or None when it isn't one. Nothing
    is trusted as it comes: numbers are re-read, the list is capped, the
    currency is one of ours (unknown — гривня, changeable in the chat)."""
    if not isinstance(raw, dict):
        return None
    items = []
    for row in raw.get("items") or []:
        if not isinstance(row, dict) or not str(row.get("name") or "").strip():
            continue
        qty, price, price2 = _number(row.get("qty")), _number(row.get("price")), _number(row.get("price2"))
        items.append({"name": " ".join(str(row["name"]).split())[:120], "qty": money(qty) if qty else None,
                      "price": money(price) if price else None, "price2": money(price2) if price2 else None})
    items = items[:MAX_ITEMS]
    total, total2 = _number(raw.get("total")), _number(raw.get("total2"))
    currency, currency2 = str(raw.get("currency") or "").upper(), str(raw.get("currency2") or "").upper()
    pair = _two_columns(items, currency, currency2)
    if pair and pair[2]:   # гривня was the left column — the смета is still in the currency
        total, total2 = total2, total
        for item in items:
            item["price"], item["price2"] = item["price2"], item["price"]
    if not pair:
        for item in items:
            del item["price2"]
    priced = [item for item in items if item["price"]]
    lines_total = money(sum((item["qty"] or 1) * item["price"] for item in priced)) if priced else None
    total = money(total) if total else None
    if not raw.get("is_invoice") or not (total or lines_total):
        return None
    reading = {
        "items": items, "total": total or lines_total, "printed_total": total, "lines_total": lines_total,
        "currency": currency if currency in CURRENCIES else "UAH", "currency_known": currency in CURRENCIES,
    }
    if pair:
        second = money(total2) if total2 else money(sum((item["qty"] or 1) * item["price2"] for item in priced))
        reading.update(currency=pair[0], currency_known=True, totals={pair[0]: reading["total"], pair[1]: second},
                       rate=round(second / reading["total"], 4))
    return reading


def read(file_name: str | None, data: bytes, hint: str | None = None) -> dict | None:
    """The смета in the file, None if the file is something else.
    FileError — with words for the chat — when it can't be read. `hint` is
    the caption the file came with."""
    kind = kind_of(file_name)
    if len(data) > MAX_FILE_BYTES:
        raise FileError("Файл больше 15 МБ — такой не прочитаю.")
    if kind == ".xls":
        raise FileError("Старый формат .xls не читаю — пересохраните как .xlsx или пришлите PDF/фото.")
    if kind not in READABLE:
        raise FileError(f"Файлы «{kind or 'без расширения'}» не читаю — подойдут xlsx, pdf, docx, csv или фото.")
    if kind in IMAGE_TYPES:
        try:
            return tidy(vision_ocr._call_vision_json(_prompt(hint), data, _MODEL))
        except vision_ocr.VisionOcrError as exc:
            raise FileError("Не смог прочитать фото — пришлите снимок почётче или файл.") from exc
    if kind == ".pdf":
        encoded = base64.b64encode(data).decode("ascii")
        return tidy(_ask([{"type": "file", "file": {"filename": file_name or "smeta.pdf", "file_data": f"data:application/pdf;base64,{encoded}"}}], hint))
    try:
        text = _xlsx_text(data) if kind == ".xlsx" else _docx_text(data) if kind == ".docx" else _plain_text(data)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
        raise FileError("Файл повреждён или это не тот формат — пришлите его ещё раз или как PDF/фото.") from exc
    if not text.strip():
        return None
    if len(text) > MAX_TEXT:
        raise FileError("Файл слишком большой, чтобы прочитать его целиком — пришлите только смету (без прайса).")
    return tidy(_ask(text, hint))
