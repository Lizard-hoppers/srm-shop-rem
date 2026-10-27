"""Отчёт бота поставщика (27.10, «Hybrid / U 22») — наш бот его сам не видит:
Telegram не показывает ботам сообщения других ботов. Поэтому отчёт приходит
к нам ответом на него («бот, возьми эту информацию») или пересылкой в группу,
и разбирается здесь по строкам:

    💰 Баланс по взаиморасчётам
    📅 Данные на 27.10.2026, 16:20
    🔸 Долг клиента: 3 210,0 $
    ⏰ Оплата на сегодня: 1 473,0 $

«Клиент» в отчёте поставщика — это мы: «долг клиента» = мы должны (весь,
с отсрочкой), «оплата на сегодня» — просрочка: то, что пора оплатить
сейчас (так объяснил поставщик, 27.10)."""
from __future__ import annotations

import re

_LINE = re.compile(
    r"(?P<label>[A-Za-zА-Яа-яЁёІіЇїЄєҐґ' ]{3,60}?)\s*[:—-]\s*(?P<num>[-−]?\d[\d   ]*(?:[.,]\d+)?)\s*(?P<cur>\$|usdt|usd|€|eur|₴|грн|uah)?",
    re.IGNORECASE,
)
_AS_OF = re.compile(r"(?:данные|дані|станом)\s+на\s+(\d{1,2}\.\d{1,2}\.\d{2,4})(?:[,\s]+(\d{1,2}:\d{2}))?", re.IGNORECASE)
_WE_OWE = ("долг клиента", "борг клієнта", "задолженность клиента", "заборгованість клієнта", "мы должны", "к оплате всего", "итого долг")
_THEY_OWE = ("аванс клиента", "переплата", "депозит", "предоплата", "долг поставщика", "наш аванс", "баланс клиента в плюсе")
_DUE = ("оплата на сегодня", "к оплате сегодня", "оплатить сегодня", "оплата сьогодні", "до сплати сьогодні", "сегодня к оплате")
_CURRENCY = {"$": "USD", "usd": "USD", "usdt": "USDT", "€": "EUR", "eur": "EUR", "₴": "UAH", "грн": "UAH", "uah": "UAH"}


def without_dates(text: str) -> str:
    """Dates and clock times out — so «27.10» is never taken for a sum."""
    return re.sub(r"\d{1,2}\.\d{1,2}\.\d{2,4}|\b\d{1,2}:\d{2}\b", " ", text or "")


def _number(raw: str) -> float:
    cleaned = re.sub(r"[   ]", "", raw).replace("−", "-").replace(",", ".")
    value = float(cleaned)
    return int(value) if value == int(value) else value


def parse(text: str | None) -> dict | None:
    """{"balance": (signed amount, currency) — + we owe, − our deposit;
    "due": (amount, currency); "as_of": "27.10.2026 16:20"} — or None when
    the text names neither a balance nor a payment due."""
    if not text:
        return None
    found: dict = {}
    as_of = _AS_OF.search(text)
    if as_of:
        found["as_of"] = " ".join(filter(None, as_of.groups()))
    for line in text.splitlines():
        match = _LINE.search(line)
        if not match:
            continue
        label = match.group("label").strip().lower()
        currency = _CURRENCY.get((match.group("cur") or "").lower(), "UAH")
        amount = _number(match.group("num"))
        if any(word in label for word in _DUE):
            found["due"] = (amount, currency)
        elif any(word in label for word in _THEY_OWE):
            found["balance"] = (-abs(amount), currency)
        elif any(word in label for word in _WE_OWE):
            found["balance"] = (amount, currency)
    return found if ("balance" in found or "due" in found) else None
