"""The language-model half of «заметки по ремонту» (core.repair_notes):
turn a voice message into text, a long message into one short line, and
read from it where the repair stands.

Both are helpers around what a person said — the caller keeps the
original next to the result, and treats any failure here as «no short
version», never as a reason to lose the message.

Speech-to-text is OpenAI's (the only provider configured that takes
audio). Reading the text goes to Claude when ANTHROPIC_API_KEY is set, OpenAI
otherwise — the same rule as core.vision_ocr. Keys are read at call time,
so adding one to .env needs only a restart, no code change.
"""
from __future__ import annotations

import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# A message this short has nothing to cut — it IS the short version.
# Sending «готово, можно выдавать» through a model could only distort it.
SHORT_ENOUGH = 70
# Hard ceiling on what a «short» line may be, whatever the model returned.
MAX_SUMMARY = 200

# What the model may say about where the repair stands. These are the
# CRM's own statuses a message can point to; «new» is never a destination.
STATUSES = ("in_progress", "ready", "issued", "cancelled")
MAX_STAGE = 80

_PROMPT = (
    "Ты помогаешь вести карточку ремонта в мастерской электроники. Ниже — сообщение сотрудника из "
    "рабочего чата по одному ремонту. Сейчас ремонт в системе в статусе «{status}», цена для клиента: "
    "{price}.\n"
    'Верни СТРОГО JSON: {{"summary": "...", "stage": "..." или null, "status": "in_progress" | "ready" | '
    '"issued" | "cancelled" | null, "price": <число> или null, "payment": "cash" | "card" | null}}.\n'
    "summary — то же сообщение одной короткой фразой (до 15 слов) на том же языке, на котором оно "
    "написано. Сохрани все факты: суммы, сроки, названия деталей и моделей, имена, договорённости с "
    "клиентом. Ничего не добавляй и не додумывай.\n"
    "stage — если из сообщения понятно, на какой стадии сейчас ремонт, назови её короткой фразой (до 8 "
    "слов) на языке сообщения: например «ждём запчасть до понедельника», «разобран, идёт диагностика», "
    "«согласовываем цену с клиентом», «собран, осталось проклеить». Формулируй по самому сообщению, "
    "не подставляй примеры; что уже сделано и что осталось — это тоже стадия. Если сообщение не о ходе "
    "ремонта (вопрос, просьба, разговор о клиенте) — null.\n"
    "status — только если сообщение ПРЯМО говорит об этом: мастер взял или начал ремонт — in_progress; "
    "ремонт закончен и устройство можно выдавать — ready; устройство уже отдали клиенту — issued; "
    "починить не получилось или клиент отказался от ремонта — cancelled. Планы, вопросы и ожидание "
    "(«завтра доделаю», «жду запчасть») статусом не являются. Сомневаешься — null.\n"
    "price — новая ПОЛНАЯ стоимость этого ремонта для клиента в гривнах, числом, только если сообщение "
    "прямо говорит, что цена ремонта изменилась или теперь такая: «ремонт будет 3000», «выходит 4500 "
    "вместо 3000», «нашли ещё поломку, плюс 500» (тогда прибавь к текущей цене). Короткое сообщение, "
    "которое просто называет сумму за ремонт, — «ремонт 3000», «цена 3000», «3000 за всё», «итого 3000» "
    "— это тоже новая цена. Цена запчасти или "
    "закупки, предоплата, сколько клиент уже заплатил, вопрос о цене, предположение («может выйти "
    "дороже») — это НЕ новая цена: null. Если цена не известна и сообщение называет только доплату — "
    "null. Сомневаешься — null.\n"
    "payment — как клиент заплатил, если сообщение это называет: наличные, «налом», «кэш» — cash; карта, "
    "«на карту», перевод, терминал — card. Не сказано — null.\n"
    "Ничего кроме JSON в ответе быть не должно."
)


class NoteAiError(Exception):
    pass


def _openai_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise NoteAiError("OPENAI_API_KEY не задан")
    return key


def transcribe(audio: bytes, filename: str = "voice.ogg") -> str:
    """A voice message as text. Raises NoteAiError if it can't be done or
    nothing intelligible came back."""
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {_openai_key()}"},
            data={"model": os.environ.get("OPENAI_STT_MODEL", "whisper-1")},
            files={"file": (filename, audio, "audio/ogg")},
            timeout=90,
        )
    except httpx.HTTPError as exc:
        raise NoteAiError("Не удалось связаться с сервисом расшифровки") from exc
    if resp.status_code != 200:
        logger.warning("transcription failed: %s %s", resp.status_code, resp.text[:300])
        raise NoteAiError(f"Сервис расшифровки вернул ошибку {resp.status_code}")
    try:
        text = (resp.json().get("text") or "").strip()
    except ValueError as exc:
        raise NoteAiError("Не смог разобрать ответ расшифровки") from exc
    if not text:
        raise NoteAiError("В голосовом не удалось разобрать речь")
    return text


def _ask_claude(prompt: str, text: str) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": os.environ.get("ANTHROPIC_NOTES_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 300,
            "system": prompt, "messages": [{"role": "user", "content": text}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise NoteAiError(f"Claude вернул ошибку {resp.status_code}")
    return "".join(block.get("text", "") for block in resp.json()["content"] if block.get("type") == "text")


def _ask_openai(prompt: str, text: str) -> str:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_openai_key()}"},
        json={
            "model": os.environ.get("OPENAI_NOTES_MODEL", "gpt-4o-mini"), "max_tokens": 300, "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise NoteAiError(f"OpenAI вернул ошибку {resp.status_code}")
    return resp.json()["choices"][0]["message"]["content"]


def _clean(value) -> str:
    return " ".join(str(value or "").split()).strip("«»\"' ")


MAX_PRICE = 1_000_000


def analyze(text: str, status_label: str = "", price=None) -> dict:
    """What a message about a repair says, five ways:

      summary  one short line (the message itself when it is already
               short, or when the model's «summary» came out longer);
      stage    where the repair stands now, in a few words — or None if
               the message isn't about that;
      status   the CRM status the message plainly points to (STATUSES) —
               or None. The bot acts on it (core.repairs.move_from_chat)
               and offers «Вернуть» in case this reading was wrong.
      payment  'cash' / 'card' if the message says how the client paid —
               what «Выдан» needs to take the money without asking.

      price    the repair's new full price for the client, if the message
               plainly states one (`price` passed in is the current one —
               the model needs it for «плюс 500») — or None. Also only a
               hint: a person confirms before the price changes.

    Raises NoteAiError when the model can't be reached or answers
    nonsense — the caller then keeps the original and knows no stage."""
    text = " ".join((text or "").split())
    prompt = _PROMPT.format(status=status_label or "неизвестен", price=f"{price} грн" if price else "не известна")
    try:
        answer = _ask_claude(prompt, text) if os.environ.get("ANTHROPIC_API_KEY") else _ask_openai(prompt, text)
        # OpenAI is held to a JSON object; Claude is asked for one — take
        # the outermost object either way.
        data = json.loads(answer[answer.index("{"): answer.rindex("}") + 1])
    except httpx.HTTPError as exc:
        raise NoteAiError("Не удалось связаться с моделью") from exc
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise NoteAiError("Не смог разобрать ответ модели") from exc
    if not isinstance(data, dict):
        raise NoteAiError("Не смог разобрать ответ модели")

    summary = _clean(data.get("summary"))
    # A short message is its own short version, and a «summary» longer
    # than what it summarises is not one.
    if len(text) <= SHORT_ENOUGH or not summary or len(summary) >= len(text):
        summary = text
    if len(summary) > MAX_SUMMARY:
        summary = summary[: MAX_SUMMARY - 1] + "…"
    stage = _clean(data.get("stage")) or None
    if stage and len(stage) > MAX_STAGE:
        stage = stage[: MAX_STAGE - 1] + "…"
    status = data.get("status") if data.get("status") in STATUSES else None
    new_price = data.get("price")
    # bool is an int in Python — «true» is not a price.
    if isinstance(new_price, bool) or not isinstance(new_price, (int, float)) or not 0 < new_price < MAX_PRICE:
        new_price = None
    elif new_price == int(new_price):
        new_price = int(new_price)
    if new_price is not None and price and new_price == price:
        new_price = None
    payment = data.get("payment") if data.get("payment") in ("cash", "card") else None
    return {"summary": summary, "stage": stage, "status": status, "price": new_price, "payment": payment}


# ---- напоминания: «когда» и «о чём» из фразы ----

_WEEKDAYS = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

_REMINDER_PROMPT = (
    "Сотрудник мастерской просит поставить напоминание. Сейчас {now}, {weekday} (время киевское).\n"
    'Верни СТРОГО JSON: {{"what": "...", "date": "ГГГГ-ММ-ДД" или null, "time": "ЧЧ:ММ" или null, "repair": "..." или null}}.\n'
    "what — о чём напомнить, одной короткой фразой в повелительной форме, как запись в ежедневнике («спросить "
    "Андрея про готовность», «отдать iPhone 13 клиенту»). Без слов «напомни», «бот», без даты и времени. Язык — "
    "как в сообщении.\n"
    "date — день напоминания, если он назван или следует из фразы: «завтра», «послезавтра», «в пятницу» "
    "(ближайшая будущая), «12 числа», «12.10», «через 3 дня». Не назван — null.\n"
    "time — время в 24-часовом виде, если названо: «в 10:20», «в три часа дня» → 15:00, «утром» → 09:00, "
    "«вечером» → 18:00, «через 2 часа» / «через полчаса» — посчитай от текущего времени (и date, если перешло "
    "за полночь). Назван только час («в три», «о третій дня») — минуты :00, текущие минуты не подставляй. "
    "Не названо — null.\n"
    "repair — если напоминание про конкретный ремонт и он назван (модель телефона, «РК-48», имя клиента) — эти "
    "слова как есть; иначе null.\n"
    "Ничего не выдумывай. Ничего кроме JSON в ответе быть не должно."
)


def parse_reminder(text: str, now) -> dict:
    """«Напомни завтра в 10:20 спросить Андрея про готовность» →
    {"what", "date", "time", "repair"}; date/time are None when the
    sentence doesn't say (core.reminders.resolve_due decides what that
    means). `now` is a Kyiv datetime — the model needs it for «завтра»,
    «в пятницу», «через час». Raises NoteAiError if the model can't be
    reached or answers nonsense."""
    prompt = _REMINDER_PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), weekday=_WEEKDAYS[now.weekday()])
    text = " ".join((text or "").split())
    try:
        answer = _ask_claude(prompt, text) if os.environ.get("ANTHROPIC_API_KEY") else _ask_openai(prompt, text)
        data = json.loads(answer[answer.index("{"): answer.rindex("}") + 1])
    except httpx.HTTPError as exc:
        raise NoteAiError("Не удалось связаться с моделью") from exc
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise NoteAiError("Не смог разобрать ответ модели") from exc
    if not isinstance(data, dict):
        raise NoteAiError("Не смог разобрать ответ модели")

    def _text(key: str) -> str | None:
        value = data.get(key)
        return _clean(value) or None if isinstance(value, str) else None

    return {"what": _text("what"), "date": _text("date"), "time": _text("time"), "repair": _text("repair")}
