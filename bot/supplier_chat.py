"""Группа с поставщиком деталей (10.10): the bot reads what is posted
there and keeps the взаиморасчёты, always by the same steps.

Every supplier has a Telegram group of his own. Once a group is tied to a
supplier («/supplier Название», by the owner or an admin), the bot:

  * keeps everything written there (supplier_chat_messages);
  * reads every file and photo; a смета becomes ONE card with ONE question
    at a time, answered with buttons, in a fixed order —
        магазин → сумма → оплата (сейчас / в долг / из депозита)
               → счёт → курс → «Провести»;
    nothing reaches the books before «Провести», and after it the card
    carries «Отменить проведение»;
  * takes «бот, оплата 5000» / «бот, депозит 300 usd» / «бот, баланс» from
    the owner or an admin.

The supplier reads his group and the bot is not for him (12.10): what the
bot writes there goes without a sound, and «/supplier Название» written in
a staff group (in a тема of it) sends that supplier's cards THERE — under
a forward of what he posted — while in his group the bot stays silent.

The supplier himself sits in this chat, so: only the owner/admin can
press the buttons or give commands, and the general assistant («бот,
сколько в кассе…») does NOT answer here — this router takes every message
of a supplier chat and nothing of it reaches the routers below.

The rules and the books are core.supplier_accounts; reading files is
core.supplier_files.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, Filter
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyParameters

from bot.assistant_chat import question_of
from core import accounts as core_accounts
from core import auth as core_auth
from core import doc_cancel
from core import documents as core_documents
from core import locations as core_locations
from core import supplier_accounts as suppliers
from core import supplier_files
from core.accounts import money
from core.storage import get_conn
from core.stores import registry_db_path, store_for_chat_id
from core.timefmt import kyiv_datetime

router = Router()
logger = logging.getLogger(__name__)

_GROUPS = ("group", "supergroup")
# Who decides about money with a supplier.
_ROLES = ("owner", "admin")
_SHOWN_ITEMS = 12
_HELP = (
    "В этой группе я читаю файлы и фото: по каждой смете спрошу магазин, сумму и как платим.\n"
    "Команды (владелец и администратор):\n"
    "• «бот, баланс» — сколько мы должны или какой у нас депозит\n"
    "• «бот, оплата 5000» · «бот, оплата 300 usd» — заплатили поставщику\n"
    "• «бот, депозит 10000» — внесли деньги наперёд\n"
    "• «бот, остаток 3210» · «бот, мы должны 3210» — сколько между нами сейчас (по сверке); "
    "«бот, остаток: депозит 300 usd» — наш депозит у них. Можно ответом на сообщение с цифрой: «бот, возьми эту информацию»\n"
    "• «бот, дай доступ» / «бот, убери доступ» — ответом на сообщение человека: может ли он давать мне команды здесь; «бот, кто с доступом»"
)
_NOT_YOURS = "Это решает владелец или администратор."

_AMOUNT = re.compile(r"(\d[\d  ]*(?:[.,]\d+)?)")
_PAY_WORDS = ("оплат", "заплат", "перев", "депозит", "внес", "внёс", "аванс", "предоплат", "отправил", "скинул")
_BALANCE_WORDS = ("баланс", "долг", "должн", "взаиморасч", "депозит", "сальдо")
# 27.10: «сейчас между нами столько» — the balance set to what the сверка says.
_OPENING_WORDS = ("остат", "начальн", "стартов", "мы должны", "они должны", "у них", "сверк", "возьми", "запиши", "по их", "по ихн")
_THEIR_DEBT_WORDS = ("они должны", "они нам", "наш депозит", "депозит", "переплат", "в плюсе")
_CURRENCY_WORDS = (
    ("USDT", ("usdt", "юсдт", "тезер", "tether")), ("USD", ("usd", "$", "дол", "у.е", "бакс")), ("EUR", ("eur", "€", "евро")),
)


def _cur(currency: str) -> str:
    return core_accounts.currency_label(currency)


def _staff(conn, telegram_id: int):
    return core_auth.get_staff_by_telegram_id(conn, telegram_id)


def _may_decide(staff) -> bool:
    return bool(staff) and staff["role"] in _ROLES


def _actor(conn, chat_id, user):
    """Who may give this group's bot commands and press its buttons: the
    owner/admin, or someone they let in here (27.10: «бот, дай доступ» in
    reply to him) — only in this group, nothing in the CRM."""
    if not user:
        return None
    staff = _staff(conn, user.id)
    if _may_decide(staff):
        return staff
    row = conn.execute(
        "SELECT name FROM supplier_chat_people WHERE chat_id = ? AND user_id = ?", (str(chat_id), user.id),
    ).fetchone()
    return {"id": None, "role": "guest", "name": row["name"]} if row else None


_ACCESS_GIVE = ("дай доступ", "дать доступ", "разреши", "добавь в белый", "в белый список", "доступ дай")
_ACCESS_TAKE = ("убери доступ", "забери доступ", "запрети", "убери из белого", "без доступа")
_ACCESS_LIST = ("кто с доступом", "у кого доступ", "белый список", "список доступа")


async def _access(message: Message, question: str, actor) -> bool:
    """«бот, дай доступ» / «убери доступ» (in reply to the person) and «кто с
    доступом» — by the owner/admin only. True if the message was one."""
    lowered = question.lower()
    give, take = any(w in lowered for w in _ACCESS_GIVE), any(w in lowered for w in _ACCESS_TAKE)
    listing = any(w in lowered for w in _ACCESS_LIST) and not give and not take
    if not (give or take or listing):
        return False
    if not _may_decide(actor):
        await _say(message, _NOT_YOURS)
        return True
    chat = str(message.chat.id)
    with get_conn(registry_db_path()) as conn:
        if listing:
            people = conn.execute("SELECT name, user_id FROM supplier_chat_people WHERE chat_id = ? ORDER BY created_at", (chat,)).fetchall()
            await _say(message, "С доступом к командам здесь, кроме владельца и админа: "
                       + (", ".join(html.escape(p["name"] or str(p["user_id"])) for p in people) or "никого") + ".")
            return True
        person = message.reply_to_message.from_user if message.reply_to_message else None
        if not person or person.is_bot:
            await _say(message, "Ответьте этой командой на сообщение того человека.")
            return True
        if give:
            conn.execute(
                """INSERT INTO supplier_chat_people (chat_id, user_id, name, added_by) VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id, user_id) DO UPDATE SET name = excluded.name""",
                (chat, person.id, person.full_name, actor["id"]),
            )
            await _say(message, f"✅ <b>{html.escape(person.full_name)}</b> теперь может давать мне команды и нажимать кнопки в этой группе.")
        else:
            gone = conn.execute("DELETE FROM supplier_chat_people WHERE chat_id = ? AND user_id = ?", (chat, person.id)).rowcount
            await _say(message, f"Доступ <b>{html.escape(person.full_name)}</b> убран." if gone else "У него и не было доступа.")
    return True


class InSupplierChat(Filter):
    """A group tied to a supplier; hands the handler its `link`."""

    async def __call__(self, message: Message) -> bool | dict:
        if message.chat.type not in _GROUPS:
            return False
        with get_conn(registry_db_path()) as conn:
            link = suppliers.get_chat(conn, message.chat.id)
        return {"link": link} if link else False


async def _say(message: Message, text: str, **kwargs) -> Message:
    """Everything the bot writes in a supplier's group. The supplier reads
    this chat and the bot is not for him (12.10): every message goes
    without a sound, and what he posted is never answered as a reply — a
    reply rings through even a muted chat. Staff are answered by reply."""
    with get_conn(registry_db_path()) as conn:
        ours = bool(message.from_user and _staff(conn, message.from_user.id))
    return await (message.reply if ours else message.answer)(text, disable_notification=True, **kwargs)


# ---- карточка ----

def _button(text: str, *parts) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=":".join(["sup", *map(str, parts)]))


def card(conn, invoice) -> tuple[str, InlineKeyboardMarkup | None]:
    """The whole state of a смета or a payment as one message: what was
    read, what has been answered, and the one question that is next."""
    invoice_id, currency = invoice["id"], invoice["currency"]
    is_invoice = invoice["kind"] == "invoice"
    supplier = html.escape(invoice["supplier_name"])
    lines = [f"🧾 <b>Смета · {supplier}</b>" if is_invoice else f"💸 <b>Оплата поставщику · {supplier}</b>"]
    reading = suppliers.reading_of(invoice)
    if invoice["file_name"]:
        lines.append(f"📄 {html.escape(invoice['file_name'])}")
    items = reading.get("items") or []
    # Two price columns in the file (dollars and гривня): both are shown,
    # each under its own name, whichever of them the смета is settled in.
    columns = suppliers.totals_of(invoice)
    first, second = reading.get("currency"), next((code for code in columns if code != reading.get("currency")), None)
    for item in items[:_SHOWN_ITEMS]:
        price = f" — {item['qty'] or 1} × {item['price']}" if item.get("price") else (f" — {item['qty']} шт" if item.get("qty") else "")
        if item.get("price") and item.get("price2") and second:
            price += f" {_cur(first)} · {item['price2']} {_cur(second)}"
        lines.append(f"• {html.escape(item['name'])}{price}")
    if len(items) > _SHOWN_ITEMS:
        lines.append(f"…и ещё {len(items) - _SHOWN_ITEMS} поз.")
    if is_invoice and invoice["total"]:
        others = "".join(f" · {money(value)} {_cur(code)}" for code, value in columns.items() if code != currency)
        lines.append(f"Итого в файле: <b>{money(invoice['total'])} {_cur(currency)}</b>{others}"
                     + (f" (курс {reading['rate']:.2f})" if others and reading.get("rate") else ""))
        printed, by_lines = reading.get("printed_total"), reading.get("lines_total")
        if printed and by_lines and abs(printed - by_lines) >= 0.01:
            lines.append(f"⚠️ По строкам выходит {by_lines} — сверьте сумму.")
    lines.append("")
    if invoice["location_id"]:
        lines.append(f"🏪 Магазин: {html.escape(invoice['location_name'])}")
    amount_text = f"{money(invoice['amount'])} {_cur(currency)}" if invoice["amount"] else None
    step, status = invoice["step"], invoice["status"]
    if amount_text and (not is_invoice or step not in ("store", "amount") or status != "draft"):
        lines.append(f"💰 Сумма: {amount_text}")
    if is_invoice and invoice["pay_mode"]:
        how = {"now": "оплатили сейчас", "debt": "в долг", "deposit": "из нашего депозита"}[invoice["pay_mode"]]
        lines.append(f"💳 Оплата: {how}")
    if invoice["account_id"]:
        lines.append(f"🏦 Счёт: {html.escape(invoice['account_name'])}")
    if invoice["rate"]:
        lines.append(f"💱 Курс: {invoice['rate']:g} (≈ {money(invoice['amount'] * invoice['rate'])} грн)")

    drop = _button("✖️ Не учитывать", "drop", invoice_id)
    again = _button("◀️ Сначала", "again", invoice_id)
    rows: list[list[InlineKeyboardButton]] = []
    if status == "posted":
        staff = core_auth.get_staff_by_id(conn, invoice["staff_id"]) if invoice["staff_id"] else None
        lines += ["", f"✅ <b>{suppliers.label(invoice)} проведён</b> · {html.escape(staff['name']) if staff else '—'} · {kyiv_datetime(invoice['posted_at'])}",
                  f"Взаиморасчёты: {suppliers.balance_text(conn, invoice['supplier_id'])}"]
        rows = [[_button("↩️ Отменить проведение", "undo", invoice_id)]]
    elif status == "cancelled":
        lines += ["", "✖️ <b>Не учтено</b> — в учёт ничего не попало.", f"Взаиморасчёты: {suppliers.balance_text(conn, invoice['supplier_id'])}"]
    elif step == "store":
        lines.append("❓ <b>В какой магазин это?</b>")
        rows = [[_button(f"🏪 {location['name']}", "store", invoice_id, location["id"])] for location in core_locations.list_locations(conn)]
        rows.append([drop])
    elif step == "amount":
        if amount_text:
            lines.append(f"❓ <b>Сумма по смете — {amount_text}?</b>\nДругая сумма — ответьте на это сообщение числом.")
            rows = [[_button(f"✅ Да, {amount_text}", "amt", invoice_id)]]
        else:
            lines.append("❓ <b>Сумму в файле не нашёл.</b> Ответьте на это сообщение числом.")
        rows.append([_button(f"Валюта: {_cur(other)}", "cur", invoice_id, other) for other in suppliers.CURRENCIES if other != currency])
        rows.append([again, drop])
    elif step == "mode":
        held = -suppliers.balance(conn, invoice["supplier_id"], currency)
        lines.append("❓ <b>Как рассчитываемся?</b>")
        rows = [[_button("💵 Оплатили сейчас", "mode", invoice_id, "now")], [_button("📒 В долг", "mode", invoice_id, "debt")]]
        if held >= invoice["amount"]:
            rows.append([_button(f"🏦 Из депозита (есть {money(held)} {_cur(currency)})", "mode", invoice_id, "deposit")])
        elif held > 0:
            lines.append(f"Депозита у поставщика {money(held)} {_cur(currency)} — на эту сумму не хватает.")
        rows.append([again, drop])
    elif step == "account":
        payable = suppliers.payable_accounts(conn, invoice)
        if payable:
            lines.append("❓ <b>С какого счёта платим?</b>")
            rows = [[_button(account["name"], "acc", invoice_id, account["id"])] for account in payable]
        else:
            lines.append(f"⚠️ У этой точки нет счёта в {_cur(currency)}. Заведите его в CRM (Касса → Счета) или начните сначала и выберите другой способ.")
        rows.append([again, drop])
    elif step == "rate":
        lines.append(f"❓ <b>По какому курсу?</b> Ответьте на это сообщение числом — сколько гривен за 1 {_cur(currency)}.")
        by_file = suppliers.file_rate(invoice)
        rows = ([[_button(f"✅ Как в смете — {by_file:.2f}", "rate", invoice_id)]] if by_file else []) + [[again, drop]]
    else:
        lines.append("❓ <b>Всё верно? Проводим?</b>")
        rows = [[_button("✅ Провести", "post", invoice_id)], [again, drop]]
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


async def _send_card(message: Message, invoice_id: int, *, into: Message | None = None) -> None:
    """Show the card: in place of `into` (a «читаю…» line), else as a new
    message under the one being answered."""
    with get_conn(registry_db_path()) as conn:
        text, keyboard = card(conn, suppliers.get(conn, invoice_id))
    if into is not None:
        try:
            await into.edit_text(text, reply_markup=keyboard)
            sent = into
        except TelegramAPIError:
            sent = await _say(message, text, reply_markup=keyboard)
    else:
        sent = await _say(message, text, reply_markup=keyboard)
    with get_conn(registry_db_path()) as conn:
        suppliers.set_card(conn, invoice_id, sent.message_id, sent.chat.id)


async def _to_desk(bot, link, text: str, *, source: Message | None = None, reply_markup=None) -> Message:
    """A message for the staff in the place chosen for this supplier
    (suppliers.set_desk) — under a forward of what was posted in his
    group, so the file is in front of whoever answers."""
    chat_id, thread = int(link["desk_chat_id"]), link["desk_thread_id"]
    forwarded = None
    if source is not None:
        try:
            forwarded = await bot.forward_message(chat_id, source.chat.id, source.message_id, message_thread_id=thread)
        except TelegramAPIError:
            logger.warning("supplier chat %s: message %s not forwarded to the desk", source.chat.id, source.message_id)
    return await bot.send_message(chat_id, text, message_thread_id=thread, reply_markup=reply_markup,
                                  reply_parameters=ReplyParameters(message_id=forwarded.message_id) if forwarded else None)


# ---- привязка группы ----

@router.message(Command("supplier"), F.chat.type.in_(_GROUPS))
async def link_command(message: Message, command: CommandObject) -> None:
    """«/supplier Название» — this group is this supplier's; «/supplier off»
    — no longer. In a shop's work group the same command sends that
    supplier's cards here («/supplier Название off» — back to his group).
    Anyone but the owner/admin gets no answer at all."""
    argument = " ".join((command.args or "").split())
    with get_conn(registry_db_path()) as conn:
        staff = _staff(conn, message.from_user.id)
        if not _may_decide(staff):
            return
        if store_for_chat_id(message.chat.id):
            name, _, last = argument.rpartition(" ")
            back = bool(name) and last.lower() in ("off", "стоп", "выкл")
            try:
                desk = suppliers.set_desk(conn, name if back else argument, None if back else message.chat.id,
                                          message.message_thread_id if message.is_topic_message else None)
            except suppliers.SupplierError as exc:
                await _say(message, str(exc))
                return
            who = html.escape(desk["supplier_name"])
            await _say(message, f"Сметы поставщика <b>{who}</b> снова подтверждаются в его группе." if back else
                       f"Сметы поставщика <b>{who}</b> теперь приходят сюда: карточка с вопросами — здесь, в его группе я молчу.")
            return
        if argument.lower() in ("off", "стоп", "выкл", "отвязать"):
            was = suppliers.unlink_chat(conn, message.chat.id)
            await _say(message, "Группа отвязана: больше её не читаю." if was else "Эта группа и не была привязана.")
            return
        try:
            link = suppliers.link_chat(conn, message.chat.id, message.chat.title, staff["id"], argument or None)
        except suppliers.SupplierError as exc:
            await _say(message, str(exc))
            return
        text = (f"Готово: это группа поставщика <b>{html.escape(link['supplier_name'])}</b>.\n"
                f"Взаиморасчёты: {suppliers.balance_text(conn, link['supplier_id'])}\n\n{_HELP}")
    await _say(message, text)


@router.my_chat_member(F.chat.type.in_(_GROUPS))
async def added_to_group(event: ChatMemberUpdated) -> None:
    """The bot was just put into a group by the owner/admin: say how to
    make it a supplier's group (and that it must be an administrator to
    see the files — Telegram hides ordinary messages from a plain bot)."""
    if event.old_chat_member.status not in ("left", "kicked") or event.new_chat_member.status not in ("member", "administrator"):
        return
    if store_for_chat_id(event.chat.id):
        return
    with get_conn(registry_db_path()) as conn:
        if suppliers.get_chat(conn, event.chat.id) or not _may_decide(_staff(conn, event.from_user.id)):
            return
    text = "Если это группа поставщика — напишите здесь <code>/supplier Название поставщика</code>, и я начну читать сметы."
    if event.new_chat_member.status != "administrator":
        text += "\n⚠️ Сделайте меня администратором группы — иначе Telegram не показывает мне сообщения и файлы."
    try:
        await event.bot.send_message(event.chat.id, text)
    except TelegramAPIError:
        pass


# ---- всё, что пишут в группе поставщика ----

async def _download(bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    return (await bot.download_file(file.file_path)).read()


async def _read_file(message: Message, link, file_id: str, file_name: str, size: int | None, *, is_document: bool) -> None:
    """A file or a photo: read it; a смета gets its card. A photo that
    isn't one (a part, a parcel) is passed over in silence; a document
    that can't be read or holds no смета is answered — it was sent as a
    file for a reason."""
    kind = supplier_files.kind_of(file_name)
    if kind not in supplier_files.READABLE and kind != ".xls":
        return   # a video, an archive — not something a смета comes as
    desk = link["desk_chat_id"]   # the staff's place for this supplier: nothing is said in his group then
    status = await _say(message, "📄 Читаю файл…") if is_document and not desk else None

    async def not_read(text: str) -> None:
        if status:
            await status.edit_text(text)
        elif desk and is_document:
            try:
                await _to_desk(message.bot, link, f"<b>{html.escape(link['supplier_name'])}</b>\n{text}", source=message)
            except TelegramAPIError:
                logger.warning("supplier chat %s: the desk is unreachable", message.chat.id)

    try:
        if size and size > supplier_files.MAX_FILE_BYTES:
            raise supplier_files.FileError("Файл больше 15 МБ — такой не прочитаю.")
        if kind == ".xls":
            raise supplier_files.FileError("Старый формат .xls не читаю — пересохраните как .xlsx или пришлите PDF/фото.")
        data = await _download(message.bot, file_id)
        # Off the event loop: the reading is a blocking call that takes seconds.
        reading = await asyncio.to_thread(supplier_files.read, file_name, data, message.caption)
    except supplier_files.FileError as exc:
        logger.info("supplier chat %s: %s not read: %s", message.chat.id, file_name, exc)
        await not_read(f"⚠️ {exc}")
        return
    except TelegramAPIError:
        await not_read("⚠️ Не смог скачать файл из Telegram — пришлите его ещё раз.")
        return
    if reading is None:
        await not_read("В этом файле не нашёл сметы (позиций с ценами или суммы к оплате). Если это смета — пришлите её как xlsx, pdf или чёткое фото.")
        return
    with get_conn(registry_db_path()) as conn:
        invoice_id = suppliers.create(
            conn, supplier_id=link["supplier_id"], chat_id=message.chat.id, reading=reading, currency=reading["currency"],
            total=reading["total"], file_name=file_name if is_document else None, source_message_id=message.message_id,
        )
    logger.info("supplier chat %s: смета %s read from %s: %s %s, %s поз.", message.chat.id, invoice_id, file_name,
                reading["total"], reading["currency"], len(reading["items"]))
    if desk:
        with get_conn(registry_db_path()) as conn:
            text, keyboard = card(conn, suppliers.get(conn, invoice_id))
        try:
            sent = await _to_desk(message.bot, link, text, source=message, reply_markup=keyboard)
        except TelegramAPIError:
            # Better a card in his group than a смета nobody was asked about.
            logger.warning("supplier chat %s: the desk is unreachable — смета %s goes to the group itself", message.chat.id, invoice_id)
        else:
            with get_conn(registry_db_path()) as conn:
                suppliers.set_card(conn, invoice_id, sent.message_id, sent.chat.id)
            return
    await _send_card(message, invoice_id, into=status)


def _money_said(text: str) -> tuple[float | int, str] | None:
    """«оплата 5 000 usd» -> (5000, 'USD'); None when no amount is named."""
    match = _AMOUNT.search(text)
    amount = core_accounts.parse_amount(match.group(1)) if match else None
    if not amount:
        return None
    lowered = text.lower()
    currency = next((code for code, words in _CURRENCY_WORDS if any(word in lowered for word in words)), "UAH")
    return amount, currency


async def _command(message: Message, link, question: str, actor=None) -> None:
    if await _access(message, question, actor):
        return
    replied = message.reply_to_message
    if not _AMOUNT.search(question) and replied and (replied.text or replied.caption):
        # «бот, возьми эту информацию» in reply to «у них 3210»: the number is there.
        question = f"{question} {replied.text or replied.caption}"
    lowered = question.lower()
    said = _money_said(question)
    if said and any(word in lowered for word in _OPENING_WORDS):
        amount, currency = said
        target = -amount if any(word in lowered for word in _THEIR_DEBT_WORDS) else amount
        with get_conn(registry_db_path()) as conn:
            before, after = suppliers.set_opening(conn, link["supplier_id"], currency, target, actor["id"] if actor else None)
            total = suppliers.balance_text(conn, link["supplier_id"])
        what = f"мы должны {money(after)} {_cur(currency)}" if after > 0 else (f"наш депозит {money(-after)} {_cur(currency)}" if after < 0 else "0")
        await _say(message, f"✅ Остаток записан: {what} (было {money(before)}).\nВзаиморасчёты: {total}\n"
                            f"Ошиблись — напишите «бот, остаток …» с верной суммой.")
        return
    if said and any(word in lowered for word in _PAY_WORDS):
        amount, currency = said
        with get_conn(registry_db_path()) as conn:
            invoice_id = suppliers.create(conn, supplier_id=link["supplier_id"], chat_id=message.chat.id, kind="payment",
                                          currency=currency, total=amount, source_message_id=message.message_id)
        await _send_card(message, invoice_id)
        return
    if any(word in lowered for word in _BALANCE_WORDS):
        with get_conn(registry_db_path()) as conn:
            lines = [f"<b>{html.escape(link['supplier_name'])}</b> — {suppliers.balance_text(conn, link['supplier_id'])}"]
            for row in suppliers.recent(conn, link["supplier_id"], limit=5):
                what = suppliers.MODES[row["pay_mode"]] if row["kind"] == "invoice" else "оплата"
                lines.append(f"• {suppliers.label(row)} · {kyiv_datetime(row['posted_at'])} · {money(row['amount'])} {_cur(row['currency'])} · {what}")
        await _say(message, "\n".join(lines))
        return
    await _say(message, _HELP)


async def _number_answered(message: Message, invoice, value: str, staff) -> bool:
    """A number sent in reply to a card that is waiting for one (сумма or
    курс). The card moves to the bottom of the chat: the question was
    answered with a message, so the next one must be the last message."""
    if invoice["status"] != "draft" or invoice["step"] not in ("amount", "rate") or core_accounts.parse_amount(value) is None:
        return False
    try:
        with get_conn(registry_db_path()) as conn:
            (suppliers.set_amount if invoice["step"] == "amount" else suppliers.set_rate)(conn, invoice["id"], value, staff["id"])
    except suppliers.SupplierError as exc:
        await _say(message, str(exc))
        return True
    try:
        await message.bot.delete_message(message.chat.id, invoice["card_message_id"])
    except TelegramAPIError:
        pass
    await _send_card(message, invoice["id"])
    return True


@router.message(InSupplierChat())
async def in_chat(message: Message, link) -> None:
    """Every message of a supplier's group ends here."""
    author = message.from_user
    document, photo = message.document, (message.photo[-1] if message.photo else None)
    text = message.text or message.caption
    with get_conn(registry_db_path()) as conn:
        staff = _staff(conn, author.id) if author else None
        actor = _actor(conn, message.chat.id, author)
        suppliers.log_message(conn, message.chat.id, message.message_id, author.id if author else None,
                              author.full_name if author else None, bool(staff), text,
                              document.file_name if document else ("фото" if photo else None))
    if document:
        await _read_file(message, link, document.file_id, document.file_name or "", document.file_size, is_document=True)
    elif photo:
        await _read_file(message, link, photo.file_id, "photo.jpg", photo.file_size, is_document=False)
    elif message.text and actor:
        replied = message.reply_to_message
        if replied:
            with get_conn(registry_db_path()) as conn:
                invoice = suppliers.by_card(conn, message.chat.id, replied.message_id)
            if invoice and await _number_answered(message, invoice, message.text, actor):
                return
        question = question_of(message.text)
        if question is not None:
            await _command(message, link, question, actor)


class OnDeskCard(Filter):
    """A number, by the owner or an admin, in reply to a card that lives
    outside the supplier's group and is waiting for one (сумма or курс).
    Anything else written there is not this router's."""

    async def __call__(self, message: Message) -> bool | dict:
        replied = message.reply_to_message
        if message.chat.type not in _GROUPS or not replied or not message.text or not message.from_user:
            return False
        if core_accounts.parse_amount(message.text) is None:
            return False
        with get_conn(registry_db_path()) as conn:
            invoice = suppliers.by_card(conn, message.chat.id, replied.message_id)
            staff = _staff(conn, message.from_user.id)
        if not invoice or invoice["status"] != "draft" or invoice["step"] not in ("amount", "rate") or not _may_decide(staff):
            return False
        return {"invoice": invoice, "staff": staff}


@router.message(OnDeskCard())
async def desk_answer(message: Message, invoice, staff) -> None:
    await _number_answered(message, invoice, message.text, staff)


# ---- кнопки карточки ----

@router.callback_query(F.data.startswith("sup:"))
async def pressed(callback: CallbackQuery) -> None:
    _, action, invoice_id, *rest = callback.data.split(":")
    argument = rest[0] if rest else None
    try:
        with get_conn(registry_db_path()) as conn:
            staff = _actor(conn, callback.message.chat.id, callback.from_user)
            if not staff:
                await callback.answer(_NOT_YOURS, show_alert=True)
                return
            invoice = suppliers.get(conn, int(invoice_id))
            if not invoice or suppliers.card_chat(invoice) != str(callback.message.chat.id):
                await callback.answer("Запись не найдена.", show_alert=True)
                return
            if action == "store":
                suppliers.choose_store(conn, invoice["id"], int(argument), staff["id"])
            elif action == "amt":
                suppliers.set_amount(conn, invoice["id"], None, staff["id"])
            elif action == "cur":
                suppliers.set_currency(conn, invoice["id"], argument, staff["id"])
            elif action == "mode":
                suppliers.choose_mode(conn, invoice["id"], argument, staff["id"])
            elif action == "acc":
                suppliers.choose_account(conn, invoice["id"], int(argument), staff["id"])
            elif action == "rate":
                suppliers.take_file_rate(conn, invoice["id"], staff["id"])
            elif action == "again":
                suppliers.restart(conn, invoice["id"], staff["id"])
            elif action == "drop":
                suppliers.cancel_draft(conn, invoice["id"])
            elif action == "post":
                suppliers.post(conn, invoice["id"], staff["id"])
            elif action == "undo":
                doc = suppliers.document(conn, invoice)
                if invoice["status"] != "posted" or not doc:
                    raise suppliers.SupplierError("Это уже отменено.")
                doc_cancel.cancel_document(conn, doc["id"], staff["id"], "Отменено в чате поставщика")
            else:
                raise suppliers.SupplierError("Кнопка устарела.")
            text, keyboard = card(conn, suppliers.get(conn, invoice["id"]))
    except (suppliers.SupplierError, core_documents.DocumentError, core_accounts.AccountError, ValueError) as exc:
        await callback.answer(str(exc) or "Не получилось.", show_alert=True)
        return
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramAPIError:
        pass
    await callback.answer({"post": "Проведено", "undo": "Отменено", "drop": "Не учитываю"}.get(action))
