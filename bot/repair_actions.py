"""Inline-button actions on a repair card posted to the staff forum topic
and the masters group — "Взять в работу" / "Готов к выдаче" / "Не удалось
починить". Runs in the bot process (long polling), separate from
core/notify.py's plain httpx calls used by the web process — but both
edit the exact same messages via core.repairs.get_order_messages(), so a
button press and a status change made from the app never leave stale
buttons behind on the other channel.

The callback_data prefix for the third button stayed "repair_release:"
even though it now calls core_repairs.cancel_repair() (21.08, was
release_claim() — see that function's docstring for why the behavior
changed) — repairs already "in_progress" when this shipped have that
prefix baked into their already-posted Telegram message, and renaming it
would silently break their button.

Фаза C (23.08): one bot process handles every store's groups, and each
store has its own SQLite file — a button press carries only an order_id in
its callback_data, which is meaningless across stores (each has its own
independent auto-increment sequence). The message's own chat — the group
the button was pressed in — unambiguously names its store
(core.stores.store_for_chat_id), so every handler resolves that first and
opens that store's DB explicitly, never the process-wide default."""
from __future__ import annotations

import asyncio
import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo

from bot.miniapp_links import crm_link
from core import auth as core_auth
from core import documents as core_documents
from core import notify as core_notify
from core import repairs as core_repairs
from core.storage import get_conn
from core.inventory import InsufficientStockError
from core.stores import get_store, registry_db_path, store_for_chat_id

router = Router()

_ACTOR_ROLES = ("owner", "admin", "master")


def _parse_order_id(callback_data: str) -> int:
    return int(callback_data.split(":", 1)[1])


async def _resolve_store(callback: CallbackQuery):
    """The store this button's group belongs to, or None (having already
    answered the callback with an alert) if the group isn't configured for
    any store — shouldn't happen in practice (buttons only exist on cards
    the bot itself posted into a known store's group), but store config
    can change after cards are already out, so this stays defensive."""
    store = store_for_chat_id(callback.message.chat.id)
    if not store:
        await callback.answer("Эта группа не привязана ни к одному магазину.", show_alert=True)
        return None
    return store


async def _resolve_actor(callback: CallbackQuery, db_path: str):
    """Staff row for whoever pressed the button, or None (having already
    answered the callback with an alert) if they're not CRM staff with the
    right role to act on repairs."""
    with get_conn(db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff or staff["role"] not in _ACTOR_ROLES:
        await callback.answer("Вы не подключены как мастер в CRM.", show_alert=True)
        return None
    return staff


async def _resolve_any_staff(callback: CallbackQuery, db_path: str):
    """Staff row for whoever tapped, any role — used only by
    open_crm_repair below. Viewing the CRM isn't the same privilege
    boundary as claiming/completing/cancelling a repair (mirrors
    webapp.deps.require_staff, not this module's _ACTOR_ROLES-gated
    _resolve_actor), so a storekeeper who'd fail _resolve_actor can still
    open a repair card to look something up."""
    with get_conn(db_path) as conn:
        return core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)


def _sync_repair_cards_blocking(order_id: int, db_path: str) -> None:
    """Re-render every posted card for this order to match its current DB
    state — the same helper the web app calls after a manual status
    change, so both channels always agree. Plain sync function (not async)
    on purpose — see _sync_after_change below, it always runs off the
    event loop via asyncio.to_thread, never awaited directly."""
    with get_conn(db_path) as conn:
        text, keyboard = core_repairs.card(conn, order_id)
        messages = core_repairs.get_order_messages(conn, order_id)
    core_notify.sync_repair_cards(messages, text, keyboard)


def _sync_after_change(order_id: int, db_path: str) -> None:
    """Fire-and-forget: core.notify's httpx calls here used to run
    synchronously on THIS coroutine, meaning the whole bot (every Telegram
    user's messages, not just this button-presser's) froze for the round
    trip on every single claim/complete/cancel tap. asyncio.to_thread gets
    it off the event loop; create_task means the caller (repair_take etc.)
    doesn't even wait for that thread — callback.answer() fires immediately,
    the card updates a moment later."""
    asyncio.create_task(asyncio.to_thread(_sync_repair_cards_blocking, order_id, db_path))


@router.callback_query(F.data.startswith("repair_take:"))
async def repair_take(callback: CallbackQuery) -> None:
    store = await _resolve_store(callback)
    if not store:
        return
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback, store.db_path)
    if not staff:
        return

    with get_conn(store.db_path) as conn:
        ok = core_repairs.claim_repair(conn, order_id, staff["id"])
        repair = None if ok else core_repairs.get_repair(conn, order_id)

    if not ok:
        if repair["status"] != "new":
            await callback.answer(f"Уже не в очереди (статус: {core_repairs.STATUS_LABELS[repair['status']]}).", show_alert=True)
        else:
            await callback.answer(f"Уже взял: {repair['master_name'] or 'другой мастер'}", show_alert=True)
        return

    _sync_after_change(order_id, store.db_path)
    await callback.answer("Взяли в работу ✅")


@router.callback_query(F.data.startswith("repair_done:"))
async def repair_done(callback: CallbackQuery) -> None:
    store = await _resolve_store(callback)
    if not store:
        return
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback, store.db_path)
    if not staff:
        return

    override = staff["role"] in ("owner", "admin")
    with get_conn(store.db_path) as conn:
        current = core_repairs.get_repair(conn, order_id)
        missing_part = bool(current) and core_repairs.needs_part(conn, current)
    if missing_part and current["status"] == "in_progress" and (override or current["master_id"] == staff["id"]):
        # «Мастер нажал Готово → система предлагает выбрать установленную
        # запчасть»: the repair isn't closed until he says what went in
        # (or that nothing did). The picker goes to his DM.
        sent = await _send_part_picker(callback, store, order_id, staff)
        await callback.answer(
            "Сначала укажите запчасть — отправил список в личные сообщения." if sent
            else "Сначала укажите запчасть. Напишите боту в личку /start и нажмите ещё раз.",
            show_alert=True,
        )
        return
    with get_conn(store.db_path) as conn:
        ok = core_repairs.complete_repair(conn, order_id, staff["id"], override=override)
        repair = None if ok else core_repairs.get_repair(conn, order_id)

    if not ok:
        if repair["status"] == "new":
            await callback.answer("Ещё не взято в работу.", show_alert=True)
        elif repair["status"] != "in_progress":
            await callback.answer(f"Уже не в работе (статус: {core_repairs.STATUS_LABELS[repair['status']]}).", show_alert=True)
        else:
            await callback.answer(f"Ремонт не за вами (мастер: {repair['master_name'] or '—'}).", show_alert=True)
        return

    _sync_after_change(order_id, store.db_path)
    await callback.answer("Готов к выдаче ✅")


@router.callback_query(F.data.startswith("repair_release:"))
async def repair_cancel(callback: CallbackQuery) -> None:
    store = await _resolve_store(callback)
    if not store:
        return
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback, store.db_path)
    if not staff:
        return

    override = staff["role"] in ("owner", "admin")
    with get_conn(store.db_path) as conn:
        ok = core_repairs.cancel_repair(conn, order_id, staff["id"], override=override)
        repair = None if ok else core_repairs.get_repair(conn, order_id)

    if not ok:
        if repair["status"] != "in_progress":
            await callback.answer(f"Уже не в работе (статус: {core_repairs.STATUS_LABELS[repair['status']]}).", show_alert=True)
        else:
            await callback.answer(f"Ремонт не за вами (мастер: {repair['master_name'] or '—'}).", show_alert=True)
        return

    _sync_after_change(order_id, store.db_path)
    await callback.answer("Отмечено как не отремонтированное")


# ---- «Указать запчасть» ----
#
# The card lives in a group; choosing a part is a small dialog, so it
# happens in the presser's DM: a list of what is free on the склад the
# repair's parts come from (the master's own — core.repairs.parts_warehouse),
# one button per партия. Each tap installs one piece; «Без запчасти» says
# there was none. Every step refreshes the group cards.

_PART_PICK_LIMIT = 12


def _part_picker(order_id: int, lines: list[dict], *, done_button: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"{line['product_name']} · партия {line['batch_id']} · {line['free']} шт"[:60],
            callback_data=f"rp_pick:{order_id}:{line['batch_id']}:{line['cell_id']}",
        )]
        for line in lines[:_PART_PICK_LIMIT]
    ]
    last = [InlineKeyboardButton(text="Без запчасти", callback_data=f"rp_none:{order_id}")]
    if done_button:
        last.append(InlineKeyboardButton(text="✅ Готово", callback_data=f"rp_done:{order_id}", style="success"))
    rows.append(last)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _part_screen(conn, order_id: int) -> tuple[str, InlineKeyboardMarkup]:
    repair = core_repairs.get_repair(conn, order_id)
    used = core_repairs.get_used_parts(conn, order_id)
    lines = core_repairs.available_parts(conn, order_id)
    device = " ".join(x for x in (repair["device_type"], repair["brand"], repair["model"]) if x)
    text = [f"⚙️ <b>{core_documents.label('repair', order_id)}</b> · {html.escape(device)}"]
    if repair["defect_description"]:
        text.append(f"Работа: {html.escape(repair['defect_description'])}")
    if used:
        text.append("Списано: " + "; ".join(
            f"{html.escape(p['product_name'])} × {p['qty']} (партия {p['batch_id']})" for p in used
        ))
    elif repair["no_parts"]:
        text.append("Отмечено: без запчасти")
    text.append("")
    text.append("Что установили? Одно нажатие — одна штука." if lines else "На вашем складе нет свободных деталей.")
    done = repair["status"] == "in_progress" and (bool(used) or bool(repair["no_parts"]))
    return "\n".join(text), _part_picker(order_id, lines, done_button=done)


async def _send_part_picker(callback: CallbackQuery, store, order_id: int, staff) -> bool:
    with get_conn(store.db_path) as conn:
        text, keyboard = _part_screen(conn, order_id)
    try:
        await callback.bot.send_message(callback.from_user.id, text, reply_markup=keyboard)
    except TelegramForbiddenError:
        return False
    return True


def _may_touch_parts(repair, staff) -> bool:
    return staff["role"] in ("owner", "admin") or repair["master_id"] == staff["id"]


@router.callback_query(F.data.startswith("repair_part:"))
async def repair_part(callback: CallbackQuery) -> None:
    """«⚙️ Указать запчасть» on the group card."""
    store = await _resolve_store(callback)
    if not store:
        return
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback, store.db_path)
    if not staff:
        return
    with get_conn(store.db_path) as conn:
        repair = core_repairs.get_repair(conn, order_id)
    if not repair or repair["status"] not in ("in_progress", "ready"):
        await callback.answer("Запчасть указывают, пока ремонт в работе или готов.", show_alert=True)
        return
    if not _may_touch_parts(repair, staff):
        await callback.answer(f"Ремонт не за вами (мастер: {repair['master_name'] or '—'}).", show_alert=True)
        return
    if await _send_part_picker(callback, store, order_id, staff):
        await callback.answer("Список деталей — в личных сообщениях с ботом")
    else:
        await callback.answer("Сначала напишите боту в личку /start, затем нажмите ещё раз.", show_alert=True)


async def _dm_context(callback: CallbackQuery, order_id: int):
    """(store, staff, repair) for a part-picker button pressed in DM — the
    store comes from the repair itself (no group chat to read it from)."""
    with get_conn(registry_db_path()) as conn:
        repair = core_repairs.get_repair(conn, order_id)
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not repair or not staff or staff["role"] not in _ACTOR_ROLES:
        await callback.answer("Ремонт не найден или у вас нет доступа.", show_alert=True)
        return None
    if not _may_touch_parts(repair, staff):
        await callback.answer("Ремонт не за вами.", show_alert=True)
        return None
    try:
        store = get_store(str(repair["location_id"]))
    except KeyError:
        await callback.answer("Точка ремонта больше не настроена.", show_alert=True)
        return None
    return store, staff, repair


async def _refresh_picker(callback: CallbackQuery, store, order_id: int) -> None:
    with get_conn(store.db_path) as conn:
        text, keyboard = _part_screen(conn, order_id)
    try:
        await callback.message.edit_text(text, reply_markup=keyboard)
    except TelegramBadRequest:
        pass


@router.callback_query(F.data.startswith("rp_pick:"))
async def repair_part_pick(callback: CallbackQuery) -> None:
    _, order_part, batch_part, cell_part = callback.data.split(":")
    order_id = int(order_part)
    ctx = await _dm_context(callback, order_id)
    if not ctx:
        return
    store, staff, _repair = ctx
    try:
        with get_conn(store.db_path) as conn:
            core_repairs.use_part(conn, order_id, int(batch_part), int(cell_part), 1, staff["id"])
    except (core_repairs.RepairPartError, InsufficientStockError) as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    _sync_after_change(order_id, store.db_path)
    await _refresh_picker(callback, store, order_id)
    await callback.answer("Списано 1 шт")


@router.callback_query(F.data.startswith("rp_none:"))
async def repair_part_none(callback: CallbackQuery) -> None:
    order_id = _parse_order_id(callback.data)
    ctx = await _dm_context(callback, order_id)
    if not ctx:
        return
    store, _staff, repair = ctx
    if repair["status"] not in ("in_progress", "ready"):
        await callback.answer("Ремонт уже закрыт.", show_alert=True)
        return
    with get_conn(store.db_path) as conn:
        core_repairs.declare_no_parts(conn, order_id)
    _sync_after_change(order_id, store.db_path)
    await _refresh_picker(callback, store, order_id)
    await callback.answer("Отмечено: без запчасти")


@router.callback_query(F.data.startswith("rp_done:"))
async def repair_part_done(callback: CallbackQuery) -> None:
    """«✅ Готово» right from the picker — «Подтвердить и завершить»."""
    order_id = _parse_order_id(callback.data)
    ctx = await _dm_context(callback, order_id)
    if not ctx:
        return
    store, staff, _repair = ctx
    with get_conn(store.db_path) as conn:
        if core_repairs.needs_part(conn, core_repairs.get_repair(conn, order_id)):
            await callback.answer("Сначала укажите запчасть или «Без запчасти».", show_alert=True)
            return
        ok = core_repairs.complete_repair(conn, order_id, staff["id"], override=staff["role"] in ("owner", "admin"))
        text, _keyboard = _part_screen(conn, order_id)
    if not ok:
        await callback.answer("Ремонт уже не в работе.", show_alert=True)
        return
    _sync_after_change(order_id, store.db_path)
    try:
        await callback.message.edit_text(text.rsplit("\n\n", 1)[0] + "\n\n✅ <b>Готов к выдаче.</b>")
    except TelegramBadRequest:
        pass
    await callback.answer("Готов к выдаче ✅")


@router.callback_query(F.data.startswith("open_crm:repair:"))
async def open_crm_repair(callback: CallbackQuery) -> None:
    """"🔗 Открыть в CRM" on a repair card. The card sits in a shared group
    indefinitely (see core.repairs.render_keyboard's docstring on why this
    row is a callback, not a plain link baked in at post time) — so this
    mints a link scoped to whoever actually tapped, right now, and DMs it
    to them privately rather than answering inside the group."""
    store = await _resolve_store(callback)
    if not store:
        return
    order_id = int(callback.data.rsplit(":", 1)[1])
    staff = await _resolve_any_staff(callback, store.db_path)
    if not staff:
        await callback.answer("Вы не подключены как сотрудник в CRM.", show_alert=True)
        return

    link = crm_link(f"/repairs/{order_id}", staff["id"], store.id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"🔗 Ремонт №{order_id} в CRM", web_app=WebAppInfo(url=link)),
    ]])
    try:
        await callback.bot.send_message(callback.from_user.id, "Открываю карточку ремонта:", reply_markup=keyboard)
    except TelegramForbiddenError:
        await callback.answer("Сначала напишите боту в личку /start, затем нажмите ещё раз.", show_alert=True)
        return
    await callback.answer("Ссылка отправлена в личные сообщения с ботом")
