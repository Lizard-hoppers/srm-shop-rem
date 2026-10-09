"""The bot's AI assistant: a request in plain words → an answer from the
base, or the thing done. «Бот, найди заказ по 13 айфону и пришли в чат»,
«бот, сколько сегодня продали», «бот, запиши расход 500 на воду», «бот,
прими ремонт: айфон 13, экран, 3000, клиент 0671234567».

The model is given the request and two sets of tools: ones that READ
(core.agent_tools) and ones that DO (core.agent_actions). It calls them —
as many rounds as it needs, within a limit — and writes a line from what
they returned. It has no other way to the data. What an action does is
decided by its code, not by the model: the model only fills in the form
(see core.agent_actions for the rule), and what was done is reported by
receipts the code writes.

OpenAI's function calling (the only provider configured). The model's
name is OPENAI_AGENT_MODEL.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3

import httpx

from core import agent_actions
from core import agent_tools
from core.timefmt import kyiv_now_text, kyiv_today

logger = logging.getLogger(__name__)

MAX_ROUNDS = 6
HISTORY_TURNS = 4
MAX_ANSWER = 3500

_SYSTEM = (
    "Ты — помощник в рабочем чате мастерской и магазина электроники «{point}». К тебе обращаются сотрудники "
    "словом «бот». Сейчас {now} по Киеву (для аргументов инструментов сегодняшняя дата — {today}).\n"
    "Отвечай ТОЛЬКО по данным инструментов. Нужных данных нет или инструмент вернул пусто — так и скажи, "
    "ничего не выдумывай: ни ремонтов, ни сумм, ни имён. Сомневаешься, что именно ищут, — сначала поищи "
    "шире (другое написание модели, без лишних слов), потом ответь.\n"
    "{actions}\n"
    "Просили прислать/показать/выставить заказ — сам найди его и пришли карточку (send_repair_card), не "
    "переспрашивай: подходит один ремонт (в том числе единственный «ближайший») — шли его; несколько — перечисли "
    "коротко и спроси, какой. Просят список ремонтов — show_open_repairs. Статус ремонта («выдан», «готов», "
    "«взял») ты не меняешь: бот делает это сам, когда так отвечают на карточку ремонта или пишут «бот, РК-48 "
    "выдан».\n"
    "{money}\n"
    "Даты и время в ответе пиши только так: ДД.ММ.ГГГГ ЧЧ:ММ, по Киеву (например 09.10.2026 18:30) — как они "
    "приходят из инструментов; никогда не ГГГГ-ММ-ДД и не время сервера.\n"
    "Пиши коротко, по делу, на языке вопроса. Обычный текст без Markdown и без звёздочек; списки — строками "
    "с «• ». Суммы — в гривнах. Если у ремонта есть card_link, дай ссылку в виде [название устройства](ссылка) — "
    "только так и только на card_link из данных. Когда прислал карточку или список в чат либо выполнил действие, "
    "не пересказывай их — хватит одной короткой строки: чек о сделанном бот покажет сам."
)
_ACTIONS_YES = (
    "Ты можешь ВЫПОЛНЯТЬ действия в учёте — инструменты: {names}. Правила:\n"
    "• выполняй сразу, когда просьба прямая и всё нужное названо, — не переспрашивай «точно?» и не отправляй в "
    "приложение;\n"
    "• передавай в поля то, что сказал человек, своими словами ничего не дополняй: не названа сумма, клиент "
    "или товар — НЕ выдумывай, спроси одной короткой фразой;\n"
    "• инструмент ответил refused — ничего не сделано: передай человеку причину или вопрос из ответа как есть;\n"
    "• одно действие — один вызов; не повторяй вызов, который уже прошёл;\n"
    "• «расход» — add_expense; «внести/изъять из кассы» — cash_correction; «клиент принёс/отдал долг, аванс» — "
    "client_money in; «вернуть клиенту» — client_money out."
)
_ACTIONS_NO = ("Действий в учёте (записать расход, принять ремонт, продать) ты для этого человека не выполняешь: "
               "он не подключён к CRM как сотрудник. Скажи, что для этого нужно привязать его Telegram в карточке "
               "сотрудника.")
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
        chat_id: str | None = None, topics: dict | None = None, staff=None, history: list[dict] | None = None,
        ) -> tuple[str, list[tuple], list[dict]]:
    """Answer a question or carry a request out. Returns (text, actions,
    receipts): actions are what the bot should post into the chat besides
    the text — ("repair_card", id), ("open_repairs", statuses); receipts
    are what was DONE in the books (core.agent_actions), each a line to
    show and what «Вернуть» undoes. `staff` is the CRM staff row of whoever
    asks (None — not linked: he gets answers, not actions); `history` —
    the last few turns with this person, so «наличными» after «на какой
    счёт?» makes sense. Raises AgentError if the model can't be reached or
    never gets to an answer — nothing done in that call is kept then (the
    caller's transaction is not committed)."""
    ctx = {"location_id": location_id, "can_money": can_money, "chat_id": chat_id, "topics": topics or {},
           "actions": [], "last_found": [], "staff": staff, "receipts": [], "done": set()}
    action_names = list(agent_actions.available(staff))
    tools = agent_tools.schemas(can_money) + agent_actions.schemas(staff)
    system = _SYSTEM.format(
        point=point_name, now=kyiv_now_text(), today=kyiv_today(), money=_MONEY_YES if can_money else _MONEY_NO,
        actions=_ACTIONS_YES.format(names=", ".join(action_names)) if action_names else _ACTIONS_NO,
    )
    messages = [{"role": "system", "content": system}, *(history or [])[-HISTORY_TURNS * 2:],
                {"role": "user", "content": question.strip()[:1500]}]
    for _round in range(MAX_ROUNDS):
        reply = _complete(messages, tools)
        calls = reply.get("tool_calls") or []
        if not calls:
            text = (reply.get("content") or "").strip()
            if not text and not ctx["actions"] and not ctx["receipts"]:
                raise AgentError("Модель вернула пустой ответ")
            # The small model often describes the repair it found instead
            # of sending its card, however it is told. When the person
            # asked for the card and the search came down to ONE repair,
            # there is nothing to decide — the card goes.
            found = ctx.get("last_found") or []
            if wants_card(question) and len(found) == 1 and not any(a[0] == "repair_card" for a in ctx["actions"]):
                ctx["actions"].append(("repair_card", found[0]))
            return text[:MAX_ANSWER], ctx["actions"], ctx["receipts"]
        messages.append({"role": "assistant", "content": reply.get("content"), "tool_calls": calls})
        for call in calls:
            name = (call.get("function") or {}).get("name") or ""
            try:
                args = json.loads((call.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            result = (agent_actions.run if name in agent_actions.ACTIONS else agent_tools.call)(conn, ctx, name, args)
            logger.info("agent tool %s(%s)", name, json.dumps(args, ensure_ascii=False)[:200])
            messages.append({"role": "tool", "tool_call_id": call.get("id"),
                             "content": json.dumps(result, ensure_ascii=False, default=str)[:6000]})
    raise AgentError("Слишком длинный разбор — уточните вопрос")
