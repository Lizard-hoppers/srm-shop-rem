"""Боты точек (23.10). У точки может быть свой Telegram-бот: у 007 —
@OO7servis_bot, у «Гибрид сервиса» — @Gibrit_Repair_Bot (его подключают к
CRM вместо старого бота). Бот и есть точка: что пришло в бот Гибрида, то
про Гибрид, кто бы ни писал; карточки и уведомления в чаты точки уходят её
ботом.

  CRM_BOT_TOKEN   — бот по умолчанию: точки без своего бота;
  CRM_POINT_BOTS  — свои боты точек: «2=<токен>», несколько — через запятую.

Без CRM_POINT_BOTS всё как раньше: один бот на все точки.

Which bot an update came through is kept in CURRENT_BOT_ID for the
duration of that update (set by bot.bot's middleware, and by the Mini App
login for its one request) — core.store_access narrows the точки by it, so
the ~100 places that resolve «the store of this Telegram user» did not have
to learn about bots.
"""
from __future__ import annotations

import os
from contextvars import ContextVar

CURRENT_BOT_ID: ContextVar[int | None] = ContextVar("crm_current_bot_id", default=None)


def bot_id(token: str | None) -> int | None:
    """A bot's id is the number before the colon in its token."""
    head = (token or "").split(":", 1)[0]
    return int(head) if head.isdigit() else None


def default_token() -> str | None:
    return os.environ.get("CRM_BOT_TOKEN") or None


def point_tokens() -> dict[int, str]:
    """location_id -> token of the точка's own bot."""
    result: dict[int, str] = {}
    for part in (os.environ.get("CRM_POINT_BOTS") or "").split(","):
        location, _, token = part.strip().partition("=")
        if location.strip().isdigit() and bot_id(token.strip()):
            result[int(location)] = token.strip()
    return result


def all_tokens() -> list[str]:
    """Every bot the CRM runs: the default one first, then the точки's own."""
    tokens = [t for t in [default_token(), *point_tokens().values()] if t]
    return list(dict.fromkeys(tokens))


def token_for_location(location_id: int | None) -> str | None:
    return point_tokens().get(location_id) or default_token()


def location_of_bot(bot: int | None) -> int | None:
    """The точка a bot belongs to; None — the default bot (or unknown)."""
    for location, token in point_tokens().items():
        if bot_id(token) == bot:
            return location
    return None


def is_default_bot(bot: int | None) -> bool:
    return bot is not None and bot == bot_id(default_token()) and location_of_bot(bot) is None


def stores_for_bot(stores: list, bot: int | None) -> list:
    """Of these точки, the ones this bot serves: its own точка for a
    точка's bot; for the default bot — every точка without a bot of its
    own. bot None (outside any update, e.g. the Mini App switcher) — all."""
    if bot is None:
        return list(stores)
    own = location_of_bot(bot)
    if own is not None:
        return [s for s in stores if s.location_id == own]
    with_bots = set(point_tokens())
    return [s for s in stores if s.location_id not in with_bots]


_usernames: dict[str, str] = {}


def username(token: str | None) -> str | None:
    """@username of the bot with this token (getMe, remembered)."""
    if not token:
        return None
    if token not in _usernames:
        import httpx

        try:
            resp = httpx.post(f"https://api.telegram.org/bot{token}/getMe", json={}, timeout=5)
            _usernames[token] = resp.json()["result"]["username"]
        except (httpx.HTTPError, KeyError, ValueError):
            return None
    return _usernames[token]


def token_for_chat(chat_id) -> str | None:
    """The bot that speaks in this chat: a точка's group — that точка's
    bot; a person's DM — the bot of the точка he works at (owner/admin, who
    work everywhere, — the default bot). None when no точка has a bot of its
    own (the caller then uses the default token)."""
    points = point_tokens()
    if not points or chat_id in (None, ""):
        return None
    from core import auth
    from core.storage import get_conn
    from core.stores import registry_db_path, store_for_chat_id

    store = store_for_chat_id(chat_id)
    if store is not None:
        return points.get(store.location_id)
    try:
        telegram_id = int(chat_id)
    except (TypeError, ValueError):
        return None
    with get_conn(registry_db_path()) as conn:
        # the точка's label printer, a master's own group (core.simple_flow)
        row = conn.execute("SELECT id FROM locations WHERE label_chat_id = ?", (telegram_id,)).fetchone()
        if row:
            return points.get(row["id"])
        row = conn.execute("SELECT location_id FROM staff WHERE personal_chat_id = ? AND active = 1", (telegram_id,)).fetchone()
        if row:
            return points.get(row["location_id"])
        if telegram_id < 0:
            return None
        staff = auth.get_staff_by_telegram_id(conn, telegram_id)
    if staff and staff["role"] not in ("owner", "admin"):
        return points.get(staff["location_id"])
    return None
