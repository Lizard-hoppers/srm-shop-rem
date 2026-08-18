"""Photo-of-invoice line-item extraction via OpenAI's vision API. This is a
best-effort guess, never a source of truth — the caller (bot/purchase_photo.py)
must always show the result to a human for confirmation before it can touch
stock; a misread quantity must never silently corrupt inventory counts.
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

_PROMPT = (
    "На фото — накладная от поставщика (магазин электроники, запчасти, "
    "аксессуары). Извлеки список позиций. Верни СТРОГО JSON вида "
    '{"items": [{"name": "...", "qty": <число или null>, '
    '"unit_cost": <число или null>}, ...]}. Название бери как в накладной, '
    "без сокращений. Если количество или цена не читаются чётко — null, "
    "не выдумывай значения. Ничего кроме JSON в ответе быть не должно."
)


class VisionOcrError(Exception):
    pass


def extract_invoice_items(photo_bytes: bytes) -> list[dict]:
    """Send a photo to OpenAI vision, get back a best-effort list of
    {"name", "qty", "unit_cost"} dicts. Raises VisionOcrError on any
    failure (missing key, network error, bad/unparseable response) — the
    caller must treat that as "couldn't recognize, ask to retry or enter
    manually", never fall through to an empty/default result silently."""
    if not _API_KEY:
        raise VisionOcrError("OPENAI_API_KEY не задан")

    b64 = base64.b64encode(photo_bytes).decode("ascii")
    payload = {
        "model": _MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": _PROMPT},
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
        items = json.loads(content)["items"]
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning("OpenAI vision returned an unexpected shape: %s", resp.text)
        raise VisionOcrError("Не смог разобрать ответ распознавания") from exc

    return [
        {"name": (it.get("name") or "").strip(), "qty": it.get("qty"), "unit_cost": it.get("unit_cost")}
        for it in items
        if isinstance(it, dict) and (it.get("name") or "").strip()
    ]
