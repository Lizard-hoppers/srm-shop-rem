"""The bot's AI assistant: a question in plain words → an answer from the
base. «Бот, найди заказ по 13 айфону и пришли в чат», «бот, что с
ремонтом Poco», «бот, сколько сегодня продали».

The model is given the question and a set of tools (core.agent_tools); it
calls them — as many rounds as it needs, within a limit — and writes the
answer from what they returned. It has no other way to the data and no
way to change it: the tools read, and the two that «do» something only
ask the bot to post into the chat. Numbers in the answer are therefore
the base's numbers, not the model's memory.

OpenAI's function calling (the only provider configured). The model's
name is OPENAI_AGENT_MODEL.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

import httpx

from core import agent_tools
from core.timefmt import kyiv_now_text, kyiv_today

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6
MAX_ANSWER = 3500

_SYSTEM = (
    "Ты — помощник в рабочем чате мастерской и магазина электроники «{point}». К тебе обращаются сотрудники "
    "словом «бот». Сейчас {now} по Киеву (для аргументов инструментов сегодняшняя дата — {today}).\n"
    "Отвечай ТОЛЬКО по данным инструментов. Нужных данных нет или инструмент вернул пусто — так и скажи, "
    "ничего не выдумывай: ни ремонтов, ни сумм, ни имён. Сомневаешься, что именно ищут, — сначала поищи "
    "шире (другое написание модели, без лишних слов), потом ответь.\n"
    "Ты ничего не меняешь в учёте. Можешь только найти и показать, а также прислать в чат карточку ремонта "
    "(send_repair_card) или готовый список ремонтов (show_open_repairs). Просили прислать/показать/выставить "
    "заказ — сам найди его и пришли карточку, не переспрашивай: подходит один ремонт (в том числе единственный "
    "«ближайший», когда точного совпадения нет) — шли его; подходят несколько — перечисли их коротко и спроси, "
    "какой. Просят сменить цену — найди ремонт, пришли карточку и скажи, что цену меняют ответом на неё "
    "(«ремонт 3000»); статус — так же ответом на карточку («выдан», «готово»).\n"
    "{money}\n"
    "Даты и время в ответе пиши только так: ДД.ММ.ГГГГ ЧЧ:ММ, по Киеву (например 09.10.2026 18:30) — как они "
    "приходят из инструментов; никогда не ГГГГ-ММ-ДД и не время сервера.\n"
    "Пиши коротко, по делу, на языке вопроса. Обычный текст без Markdown и без звёздочек; списки — строками "
    "с «• ». Суммы — в гривнах. Если у ремонта есть card_link, дай ссылку в виде [название устройства](ссылка) — "
    "только так и только на card_link из данных. Когда прислал карточку или список в чат, не пересказывай их "
    "целиком — хватит одной строки."
)
_MONEY_YES = "Спрашивающий — владелец или админ: вопросы о деньгах, прибыли и долгах ему можно."
_MONEY_NO = ("Спрашивающий не владелец и не админ: про кассу, прибыль, долги и зарплаты ответь, что это видно "
             "только владельцу и админу.")


# «Пришли», «выстав», «скинь», «покажи карточку» — the person wants the
# card itself in the chat, not a description of it.
_SEND_WORDS = ("пришл", "приши", "выстав", "скинь", "скин", "отправ", "кинь", "карточк", "надішли", "скинь")


def wants_card(question: str) -> bool:
    lowered = question.casefold()
    return any(word in lowered for word in _SEND_WORDS)


class AgentError(Exception):
    pass


def _complete(messages: list[dict], tools: list[dict]) -> dict:
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise AgentError("OPENAI_API_KEY не задан")
    try:
        resp = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": os.environ.get("OPENAI_AGENT_MODEL", "gpt-4o-mini"), "temperature": 0, "max_tokens": 900,
                  "messages": messages, "tools": tools, "tool_choice": "auto"},
            timeout=60,
        )
    except httpx.HTTPError as exc:
        raise AgentError("Не удалось связаться с моделью") from exc
    if resp.status_code != 200:
        logger.warning("agent request failed: %s %s", resp.status_code, resp.text[:300])
        raise AgentError(f"Модель вернула ошибку {resp.status_code}")
    try:
        return resp.json()["choices"][0]["message"]
    except (KeyError, IndexError, ValueError) as exc:
        raise AgentError("Не смог разобрать ответ модели") from exc


def ask(conn: sqlite3.Connection, question: str, *, location_id: int, point_name: str, can_money: bool,
        chat_id: str | None = None, topics: dict | None = None) -> tuple[str, list[tuple]]:
    """Answer a question. Returns (text, actions) — actions are what the
    bot should post into the chat besides the text: ("repair_card", id),
    ("open_repairs", statuses). Raises AgentError if the model can't be
    reached or never gets to an answer."""
    ctx = {"location_id": location_id, "can_money": can_money, "chat_id": chat_id, "topics": topics or {},
           "actions": [], "last_found": []}
    tools = agent_tools.schemas(can_money)
    messages = [
        {"role": "system", "content": _SYSTEM.format(point=point_name, now=kyiv_now_text(), today=kyiv_today(), money=_MONEY_YES if can_money else _MONEY_NO)},
        {"role": "user", "content": question.strip()[:1500]},
    ]
    for _round in range(MAX_ROUNDS):
        reply = _complete(messages, tools)
        calls = reply.get("tool_calls") or []
        if not calls:
            text = (reply.get("content") or "").strip()
            if not text and not ctx["actions"]:
                raise AgentError("Модель вернула пустой ответ")
            # The small model often describes the repair it found instead
            # of sending its card, however it is told. When the person
            # asked for the card and the search came down to ONE repair,
            # there is nothing to decide — the card goes.
            found = ctx.get("last_found") or []
            if wants_card(question) and len(found) == 1 and not any(a[0] == "repair_card" for a in ctx["actions"]):
                ctx["actions"].append(("repair_card", found[0]))
            return text[:MAX_ANSWER], ctx["actions"]
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            result = agent_tools.call(conn, ctx, name, args)
            logger.info("agent tool %s(%s)", name, json.dumps(args, ensure_ascii=False)[:200])
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
    raise AgentError("Слишком длинный разбор — уточните вопрос")
