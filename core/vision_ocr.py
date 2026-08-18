"""Photo -> structured data via OpenAI's vision API — invoice line items
and product barcode/label reads. Both are best-effort guesses, never a
source of truth: every caller must show the result to a human for
confirmation before it touches stock or the product catalog; a misread
must never silently corrupt data.
"""
from __future__ import annotations

import base64
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_API_KEY = os.environ.get("OPENAI_API_KEY")
_MODEL = os.environ.get("OPENAI_VISION_MODEL", "gpt-4o-mini")

_INVOICE_PROMPT = (
    "На фото — накладная от поставщика (магазин электроники, запчасти, "
    "аксессуары). Извлеки список позиций. Верни СТРОГО JSON вида "
    '{"items": [{"name": "...", "qty": <число или null>, '
    '"unit_cost": <число или null>}, ...]}. Название бери как в накладной, '
    "без сокращений. Если количество или цена не читаются чётко — null, "
    "не выдумывай значения. Ничего кроме JSON в ответе быть не должно."
)

_LABEL_PROMPT = (
    "На фото — этикетка или штрихкод на упаковке запчасти/товара (часто "
    "китайская доставка, генерик-запчасти для ремонта телефонов — экраны, "
    "шлейфы, зарядки, кабели и т.п.). Извлеки: название/модель товара, "
    "если оно читается на этикетке (переведи коротко на русский, если "
    "очевидно что это), и код/артикул — обычно ряд цифр или букв+цифр под "
    "штрихкодом или рядом с ним. Верни СТРОГО JSON вида "
    '{"name": "..." или null, "sku": "..." или null}. Если название или '
    "код не читаются чётко — null, не выдумывай. Цену в ответ НЕ включай "
    "вообще, даже если на этикетке есть цифра похожая на цену — она не "
    "нужна и будет неверной (обычно китайская закупочная, не наша)."
)


_DEVICE_PROMPT = (
    "На фото — устройство, которое сдают в ремонт (телефон, ноутбук, "
    "планшет и т.п.). На фото может быть этикетка на коробке, гравировка "
    "или наклейка на задней панели устройства, либо экран настроек "
    "«Об устройстве»/«О телефоне». Определи по фото: тип устройства — "
    "ОБЯЗАТЕЛЬНО на русском языке, с большой буквы, одно слово в "
    "именительном падеже (например: Смартфон, Ноутбук, Планшет, "
    "Наушники), бренд как он написан на устройстве латиницей (например: "
    "Apple, Samsung, Xiaomi), модель (например: iPhone 13, Galaxy A54) и "
    "серийный номер или IMEI (обычно длинная последовательность цифр "
    "рядом с подписью IMEI, S/N или Serial Number — если на фото два "
    "IMEI, возьми первый). Верни СТРОГО JSON вида "
    '{"device_type": "..." или null, "brand": "..." или null, '
    '"model": "..." или null, "serial_number": "..." или null}. Если '
    "что-то из этого не видно на фото или читается нечётко — null, не "
    "выдумывай значения."
)


class VisionOcrError(Exception):
    pass


def _call_vision_json(prompt: str, photo_bytes: bytes) -> dict:
    """POST a photo + prompt to OpenAI's vision-capable chat completions
    endpoint, constrained to a JSON object response. Raises VisionOcrError
    on any failure (missing key, network error, non-200, unparseable
    content) — callers must treat that as "couldn't recognize", never
    fall through to an empty/default result silently."""
    if not _API_KEY:
        raise VisionOcrError("OPENAI_API_KEY не задан")

    b64 = base64.b64encode(photo_bytes).decode("ascii")
    payload = {
        "model": _MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }],
        "response_format": {"type": "json_object"},
        "max_tokens": 1500,
    }

    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_API_KEY}"},
            json=payload,
            timeout=30,
        )
    except httpx.HTTPError as exc:
        raise VisionOcrError("Не удалось связаться с OpenAI") from exc

    if resp.status_code != 200:
        logger.warning("OpenAI vision request failed: %s %s", resp.status_code, resp.text)
        raise VisionOcrError(f"OpenAI вернул ошибку {resp.status_code}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        return json.loads(content)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("OpenAI vision returned an unexpected shape: %s", resp.text)
        raise VisionOcrError("Не смог разобрать ответ распознавания") from exc


def extract_invoice_items(photo_bytes: bytes) -> list[dict]:
    """Best-effort list of {"name", "qty", "unit_cost"} dicts from a photo
    of a supplier invoice."""
    data = _call_vision_json(_INVOICE_PROMPT, photo_bytes)
    try:
        items = data["items"]
    except (KeyError, TypeError) as exc:
        raise VisionOcrError("Не смог разобрать ответ распознавания") from exc

    return [
        {"name": (it.get("name") or "").strip(), "qty": it.get("qty"), "unit_cost": it.get("unit_cost")}
        for it in items
        if isinstance(it, dict) and (it.get("name") or "").strip()
    ]


def extract_product_label(photo_bytes: bytes) -> dict:
    """Best-effort {"name", "sku"} (either may be None) from a photo of a
    product's barcode/label — used by the scan button next to the SKU
    field on the product create/edit forms. Deliberately never returns a
    price: this shop's stock is largely generic Chinese-sourced parts
    whose printed/barcode price, if any, doesn't match local pricing and
    would just be misleading if auto-filled."""
    data = _call_vision_json(_LABEL_PROMPT, photo_bytes)
    name = data.get("name")
    sku = data.get("sku")
    return {
        "name": name.strip() if isinstance(name, str) and name.strip() else None,
        "sku": sku.strip() if isinstance(sku, str) and sku.strip() else None,
    }


def extract_device_info(photo_bytes: bytes) -> dict:
    """Best-effort {"device_type", "brand", "model", "serial_number"} (any
    may be None) from a photo of a device being taken in for repair — a
    box label, back-panel engraving, or a Settings/'About phone' screen.
    Used by the scan button next to Серийный №/IMEI on repair intake."""
    data = _call_vision_json(_DEVICE_PROMPT, photo_bytes)

    def _clean(key: str) -> str | None:
        value = data.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    return {
        "device_type": _clean("device_type"),
        "brand": _clean("brand"),
        "model": _clean("model"),
        "serial_number": _clean("serial_number"),
    }
