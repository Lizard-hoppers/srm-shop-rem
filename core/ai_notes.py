"""The language-model half of «заметки по ремонту» (core.repair_notes):
turn a voice message into text and a long message into one short line.

Both are helpers around what a person said — the caller keeps the
original next to the result, and treats any failure here as «no short
version», never as a reason to lose the message.

Speech-to-text is OpenAI's (the only provider configured that takes
audio). Shortening goes to Claude when ANTHROPIC_API_KEY is set, OpenAI
otherwise — the same rule as core.vision_ocr. Keys are read at call time,
so adding one to .env needs only a restart, no code change.
"""
from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger(__name__)

# A message this short has nothing to cut — it IS the short version.
# Sending «готово, можно выдавать» through a model could only distort it.
SHORT_ENOUGH = 70
# Hard ceiling on what a «short» line may be, whatever the model returned.
MAX_SUMMARY = 200

_PROMPT = (
    "Ты помогаешь вести карточку ремонта в мастерской электроники. Ниже — сообщение сотрудника "
    "из рабочего чата по одному ремонту. Перепиши его одной короткой фразой (до 15 слов) на том же "
    "языке, на котором оно написано. Сохрани все факты: суммы, сроки, названия деталей и моделей, "
    "имена, договорённости с клиентом. Ничего не добавляй и не додумывай, не используй кавычки и "
    "вводные слова. Ответь только этой фразой."
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


def _ask_claude(text: str) -> str:
    resp = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        json={
            "model": os.environ.get("ANTHROPIC_NOTES_MODEL", "claude-haiku-4-5-20251001"), "max_tokens": 200,
            "system": _PROMPT, "messages": [{"role": "user", "content": text}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise NoteAiError(f"Claude вернул ошибку {resp.status_code}")
    return "".join(block.get("text", "") for block in resp.json()["content"] if block.get("type") == "text")


def _ask_openai(text: str) -> str:
    resp = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {_openai_key()}"},
        json={
            "model": os.environ.get("OPENAI_NOTES_MODEL", "gpt-4o-mini"), "max_tokens": 200, "temperature": 0,
            "messages": [{"role": "system", "content": _PROMPT}, {"role": "user", "content": text}],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise NoteAiError(f"OpenAI вернул ошибку {resp.status_code}")
    return resp.json()["choices"][0]["message"]["content"]


def shorten(text: str) -> str:
    """One short line saying what the message says. A message that is
    already short comes back as it is. Raises NoteAiError when the model
    can't be reached or answers nonsense — the caller then shows the
    original."""
    text = " ".join((text or "").split())
    if len(text) <= SHORT_ENOUGH:
        return text
    try:
        answer = _ask_claude(text) if os.environ.get("ANTHROPIC_API_KEY") else _ask_openai(text)
    except httpx.HTTPError as exc:
        raise NoteAiError("Не удалось связаться с моделью") from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise NoteAiError("Не смог разобрать ответ модели") from exc
    answer = " ".join((answer or "").split()).strip("«»\"' ")
    if not answer:
        raise NoteAiError("Модель вернула пустой ответ")
    # A «summary» longer than what it summarises is not one.
    if len(answer) >= len(text):
        return text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 1] + "…"
    return answer if len(answer) <= MAX_SUMMARY else answer[: MAX_SUMMARY - 1] + "…"
