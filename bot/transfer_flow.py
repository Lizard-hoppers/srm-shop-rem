"""«🔁 Перемещение» in the bot DM (Заход 3) — sending goods to another
склад and confirming goods sent to you, without opening the Mini App.
Same core as the web page (core.stock_transfers): «Отправил → В пути →
Принял».

Sending: pick the destination склад → find a position by scanning or
typing its IMEI / штрихкод / part of the name → quantity (a serial unit is
always 1) → add more or send. Each position is a specific партия in a
specific cell of YOUR склад (a master sends from his own, stock staff from
their точка's). Whoever answers for the destination gets a DM with «✅
Принял всё»; a shortfall is reported from the Mini App page, where the
per-line quantities and the note are.

Reuses bot.quick_actions' Clean-Chat helpers and shift gate; the FSM
states (TransferFlow) are declared there so its shared ❌ Отмена and
fallback handlers cover this flow too.
"""
from __future__ import annotations

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot import quick_actions as qa
from bot.miniapp_links import crm_link
from core import auth as core_auth
from core import documents as core_documents
from core import inventory as core_inventory
from core import stock_transfers as core_transfers
from core import warehouses as core_warehouses
from core.storage import get_conn
from core.store_access import stores_for_staff
from core.stores import StoreConfig, get_store, registry_db_path

router = Router()

_TRANSFER_ROLES = ("owner", "admin", "storekeeper", "master")
_PICK_LIMIT = 8
_ASK_WHAT = "Что перемещаем? Отсканируйте или введите IMEI, штрихкод либо часть названия:"


def _location_ids(staff) -> set[int]:
    return {s.location_id for s in stores_for_staff(staff)}


def _receivable_ids(conn, staff) -> list[int]:
    ids = _location_ids(staff)
    return [
        w["id"] for w in core_warehouses.list_warehouses(conn, include_inactive_masters=True)
        if w["kind"] != "transit" and core_warehouses.may_receive(conn, staff, w, ids)
    ]


def _source_warehouse(conn, staff, store: StoreConfig):
    """The склад this person sends FROM in the bot: a master — his own,
    everyone else — the склад of the точка they are working in. (Sending
    from some other склад is an owner's job in the Mini App.)"""
    if staff["role"] == "master":
        return core_warehouses.master_warehouse(conn, staff["id"])
    return core_warehouses.point_warehouse(conn, store.location_id)


def _line_text(line) -> str:
    parts = [html.escape(line["product_name"])]
    if line["imei"]:
        parts.append(f"IMEI {html.escape(line['imei'])}")
    else:
        parts.append(f"партия №{line['batch_id']}")
        if line["supplier_name"]:
            parts.append(html.escape(line["supplier_name"]))
    return " · ".join(parts)


def _items_text(lines: list[dict]) -> str:
    return "\n".join(f"• {item['label']} — {item['qty']} шт" for item in lines)


def _review_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Ещё позиция", callback_data="tr_more")],
        [
            InlineKeyboardButton(text="📤 Отправить", callback_data="tr_send", style="success"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="tr_cancel", style="danger"),
        ],
    ])


def _review_text(data: dict) -> str:
    return f"🔁 <b>Перемещение</b>\n{data['route']}\n\n{_items_text(data['lines'])}"


async def _context(callback: CallbackQuery, state: FSMContext):
    """(store, staff, data) behind a flow button, or None having answered."""
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await state.clear()
        await callback.answer("Диалог устарел — нажмите «Перемещение» ещё раз.", show_alert=True)
        return None
    with get_conn(store.db_path) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff or staff["role"] not in _TRANSFER_ROLES:
        await state.clear()
        await callback.answer("Недостаточно прав.", show_alert=True)
        return None
    return store, staff, data


@router.message(F.text == qa.BTN_TRANSFER, F.chat.type == "private")
async def transfer_start(message: Message, state: FSMContext) -> None:
    resolved = qa._resolve_staff_for_dm(message.from_user.id)
    if not resolved:
        return
    store, staff = resolved
    if staff["role"] not in _TRANSFER_ROLES:
        await message.answer("Недостаточно прав для перемещения.")
        return
    if not await qa._shift_gate(message, state, store, staff):
        return

    with get_conn(store.db_path) as conn:
        source = _source_warehouse(conn, staff, store)
        targets = [
            w for w in core_warehouses.list_warehouses(conn)
            if w["kind"] != "transit" and (not source or w["id"] != source["id"])
        ]
        inbox = core_transfers.list_transfers(conn, status="sent", to_warehouse_ids=_receivable_ids(conn, staff))
        source_name = core_warehouses.display_name(source) if source else None
        target_buttons = [(w["id"], core_warehouses.display_name(w)) for w in targets]

    await qa._reset(message, state)
    await qa._consume(state, message)
    if not source:
        await qa._repost(message, state, "У вас нет склада, с которого можно отправлять.")
        return
    await state.update_data(store_id=store.id, source_id=source["id"], source_name=source_name, lines=[])
    rows = []
    if inbox:
        rows.append([InlineKeyboardButton(text=f"📥 Принять ({len(inbox)})", callback_data="tr_inbox", style="success")])
    rows += [[InlineKeyboardButton(text=f"→ {name}", callback_data=f"tr_to:{wid}")] for wid, name in target_buttons]
    await qa._repost(
        message, state,
        f"🔁 <b>Перемещение</b>\nСо склада: {html.escape(source_name)}\n\nКуда отправляем?",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.callback_query(F.data.startswith("tr_to:"))
async def transfer_pick_target(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, data = ctx
    with get_conn(store.db_path) as conn:
        target = core_warehouses.get_warehouse(conn, int(callback.data.split(":", 1)[1]))
        target_name = core_warehouses.display_name(target) if target else None
    if not target or target["kind"] == "transit" or target["id"] == data.get("source_id"):
        await callback.answer("Этот склад выбрать нельзя.", show_alert=True)
        return
    route = f"{html.escape(data['source_name'])} → {html.escape(target_name)}"
    await state.update_data(target_id=target["id"], route=route)
    await state.set_state(qa.TransferFlow.search)
    await qa._advance_callback(callback, state, f"🔁 <b>Перемещение</b>\n{route}\n\n{_ASK_WHAT}")
    await callback.answer()


def _find_lines(conn, source_id: int, query: str, taken: dict[tuple[int, int], int]) -> list:
    """Stock lines of the source склад matching what was scanned/typed —
    an IMEI exactly, a SKU exactly, or part of the name — minus what this
    transfer already holds in full."""
    lines = core_inventory.stock_lines(conn, warehouse_id=source_id)
    left = [line for line in lines if line["qty"] - taken.get((line["batch_id"], line["cell_id"]), 0) > 0]
    imei = core_inventory.normalize_imei(query)
    by_imei = [line for line in left if line["imei"] and line["imei"] == imei]
    if by_imei:
        return by_imei
    by_sku = [line for line in left if line["sku"] and line["sku"].strip().lower() == query.strip().lower()]
    if by_sku:
        return by_sku
    needle = query.strip().lower()
    return [line for line in left if needle in line["product_name"].lower()]


def _taken(data: dict) -> dict[tuple[int, int], int]:
    taken: dict[tuple[int, int], int] = {}
    for item in data.get("lines", []):
        key = (item["batch_id"], item["cell_id"])
        taken[key] = taken.get(key, 0) + item["qty"]
    return taken


async def _add_line(state: FSMContext, line, qty: int) -> dict:
    data = await state.get_data()
    lines = list(data.get("lines", []))
    lines.append({"batch_id": line["batch_id"], "cell_id": line["cell_id"], "qty": qty, "label": _line_text(line)})
    await state.set_state(qa.TransferFlow.review)
    return await state.update_data(lines=lines, pending=None)


async def _ask_qty_or_add(state: FSMContext, line, available: int):
    """A serial unit or a last single piece goes straight in; anything
    else needs a quantity. Returns the text + keyboard of the next screen."""
    if line["is_serial"] or available == 1:
        data = await _add_line(state, line, 1)
        return _review_text(data), _review_keyboard()
    await state.update_data(pending={
        "batch_id": line["batch_id"], "cell_id": line["cell_id"], "available": available, "label": _line_text(line),
    })
    await state.set_state(qa.TransferFlow.qty)
    return f"{_line_text(line)}\nНа складе: {available} {html.escape(line['unit'])}\n\nСколько перемещаем?", None


# ~in_(_ENTRY_BUTTONS) on both text steps: this router is registered ahead
# of bot.quick_actions', so a tap on a keyboard button mid-flow (❌ Отмена,
# or another action) must fall through to its own handler there rather
# than be taken for a search string or a quantity.
@router.message(qa.TransferFlow.search, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def transfer_search(message: Message, state: FSMContext) -> None:
    query = message.text.strip()
    if not query:
        await qa._nudge(message, state, "Введите IMEI, штрихкод или часть названия.")
        return
    data = await state.get_data()
    try:
        store = get_store(data["store_id"])
    except KeyError:
        await qa._consume(state, message)
        await state.clear()
        return
    taken = _taken(data)
    with get_conn(store.db_path) as conn:
        found = _find_lines(conn, data["source_id"], query, taken)

    if not found:
        await qa._nudge(message, state, f"На вашем складе не нашёл «{html.escape(query)}». Попробуйте ещё раз.")
        return
    if len(found) == 1:
        line = found[0]
        text, keyboard = await _ask_qty_or_add(state, line, line["qty"] - taken.get((line["batch_id"], line["cell_id"]), 0))
        await qa._advance(message, state, text, reply_markup=keyboard)
        return

    shown = found[:_PICK_LIMIT]
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=(f"{line['product_name']} · " + (f"IMEI {line['imei']}" if line["imei"] else f"партия №{line['batch_id']}")
                  + f" · {line['qty']} шт")[:60],
            callback_data=f"tr_pick:{line['batch_id']}:{line['cell_id']}",
        )]
        for line in shown
    ])
    suffix = f"\n\nПоказаны первые {len(shown)} из {len(found)} — уточните запрос." if len(found) > len(shown) else ""
    await qa._advance(message, state, f"Нашлось {len(found)} — выберите позицию:{suffix}", reply_markup=keyboard)


@router.callback_query(F.data.startswith("tr_pick:"), qa.TransferFlow.search)
async def transfer_pick_line(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, _staff, data = ctx
    _, batch_part, cell_part = callback.data.split(":")
    taken = _taken(data)
    with get_conn(store.db_path) as conn:
        line = next(
            (l for l in core_inventory.stock_lines(conn, warehouse_id=data["source_id"])
             if l["batch_id"] == int(batch_part) and l["cell_id"] == int(cell_part)),
            None,
        )
    available = (line["qty"] - taken.get((line["batch_id"], line["cell_id"]), 0)) if line else 0
    if available <= 0:
        await callback.answer("Этой позиции на складе уже нет.", show_alert=True)
        return
    text, keyboard = await _ask_qty_or_add(state, line, available)
    await qa._advance_callback(callback, state, text, keyboard)
    await callback.answer()


@router.message(qa.TransferFlow.qty, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def transfer_got_qty(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    pending = data.get("pending")
    qty = qa._parse_positive_int(message.text)
    if not pending:
        await qa._nudge(message, state, "Сначала выберите позицию.")
        return
    if not qty or qty > pending["available"]:
        await qa._nudge(message, state, f"Введите количество числом, не больше {pending['available']}.")
        return
    lines = list(data.get("lines", []))
    lines.append({"batch_id": pending["batch_id"], "cell_id": pending["cell_id"], "qty": qty, "label": pending["label"]})
    await state.set_state(qa.TransferFlow.review)
    data = await state.update_data(lines=lines, pending=None)
    await qa._advance(message, state, _review_text(data), reply_markup=_review_keyboard())


@router.callback_query(F.data == "tr_more", qa.TransferFlow.review)
async def transfer_more(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.set_state(qa.TransferFlow.search)
    await qa._advance_callback(callback, state, f"{_review_text(data)}\n\n{_ASK_WHAT}")
    await callback.answer()


def _recipients(conn, target, sender_id: int) -> list:
    """Staff rows to DM about an incoming transfer: the master himself for
    a master's склад; for a точка's — its stock staff plus owners/admins."""
    staff = core_auth.list_staff(conn)
    if target["kind"] == "master":
        chosen = [s for s in staff if s["id"] == target["staff_id"]]
    else:
        chosen = [
            s for s in staff
            if s["role"] in ("owner", "admin")
            or (s["role"] == "storekeeper" and (s["location_id"] or 0) == target["location_id"])
        ]
    return [s for s in chosen if s["telegram_id"] and s["id"] != sender_id]


def incoming_keyboard(transfer_id: int, staff_id: int, store_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принял всё", callback_data=f"tr_recv:{transfer_id}", style="success")],
        [InlineKeyboardButton(
            text="⚠ Есть расхождение — открыть",
            web_app=WebAppInfo(url=crm_link(f"/transfers/{transfer_id}", staff_id, store_id)),
        )],
    ])


def incoming_text(conn, transfer_id: int) -> str:
    transfer = core_transfers.get_transfer(conn, transfer_id)
    lines = [f"📦 <b>Перемещение №{transfer_id}</b>", html.escape(core_transfers.route(transfer)),
             f"Отправил: {html.escape(transfer['sent_by_name'] or '—')}", ""]
    for item in core_transfers.get_items(conn, transfer_id):
        detail = f"IMEI {html.escape(item['imei'])}" if item["imei"] else f"партия №{item['batch_id']}"
        lines.append(f"• {html.escape(item['product_name'])} · {detail} — {item['qty']} {html.escape(item['unit'])}")
    return "\n".join(lines)


@router.callback_query(F.data == "tr_send", qa.TransferFlow.review)
async def transfer_send(callback: CallbackQuery, state: FSMContext) -> None:
    ctx = await _context(callback, state)
    if not ctx:
        return
    store, staff, data = ctx
    if await qa._already_done(callback, store.db_path):
        return
    try:
        with get_conn(store.db_path) as conn:
            source = _source_warehouse(conn, staff, store)
            if not source or source["id"] != data["source_id"]:
                raise core_transfers.TransferError("Ваш склад изменился — начните перемещение заново.")
            transfer_id = core_transfers.send(
                conn, data["source_id"], data["target_id"],
                [(item["batch_id"], item["cell_id"], item["qty"]) for item in data["lines"]],
                staff["id"], key=qa._tap_key(callback),
            )
            label = core_documents.doc_label(core_transfers.document(conn, transfer_id))
            target = core_warehouses.get_warehouse(conn, data["target_id"])
            notify = [(s["telegram_id"], s["id"]) for s in _recipients(conn, target, staff["id"])]
            notice = incoming_text(conn, transfer_id)
    except core_transfers.TransferError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    user_message_ids = data.get("user_message_ids", [])
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, user_message_ids)
    await qa._safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        f"✅ <b>{label} отправлено</b>\n{data['route']}\n\n{_items_text(data['lines'])}\n\n"
        "Товар в пути — станет остатком получателя, когда тот нажмёт «Принял».",
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Открыть в CRM",
            web_app=WebAppInfo(url=crm_link(f"/transfers/{transfer_id}", staff["id"], store.id)),
        )]]),
    )
    await callback.answer("Отправлено")
    for telegram_id, staff_id in notify:
        try:
            await callback.bot.send_message(
                telegram_id, notice, reply_markup=incoming_keyboard(transfer_id, staff_id, store.id),
            )
        except TelegramAPIError:
            pass  # never started the bot in DM — the transfer still waits for them in «Перемещение»


@router.callback_query(F.data == "tr_inbox")
async def transfer_inbox(callback: CallbackQuery, state: FSMContext) -> None:
    with get_conn(registry_db_path()) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff:
            await callback.answer("Вы не подключены как сотрудник в CRM.", show_alert=True)
            return
        inbox = core_transfers.list_transfers(conn, status="sent", to_warehouse_ids=_receivable_ids(conn, staff))
        rows = [
            [InlineKeyboardButton(text=f"№{t['id']} · {core_transfers.route(t)}"[:60], callback_data=f"tr_show:{t['id']}")]
            for t in inbox[:_PICK_LIMIT]
        ]
    if not rows:
        await callback.answer("Нечего принимать.", show_alert=True)
        return
    await state.clear()
    await qa._safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        "📥 <b>Ждут вашего «Принял»</b>", InlineKeyboardMarkup(inline_keyboard=rows),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tr_show:"))
async def transfer_show(callback: CallbackQuery) -> None:
    transfer_id = int(callback.data.split(":", 1)[1])
    with get_conn(registry_db_path()) as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        transfer = core_transfers.get_transfer(conn, transfer_id)
        if not staff or not transfer or transfer["status"] != "sent":
            await callback.answer("Это перемещение уже принято или отменено.", show_alert=True)
            return
        text = incoming_text(conn, transfer_id)
        store_id = str(next(iter(sorted(_location_ids(staff)))))
    await qa._safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id, text,
        incoming_keyboard(transfer_id, staff["id"], store_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("tr_recv:"))
async def transfer_receive(callback: CallbackQuery) -> None:
    """«✅ Принял всё» — every line in full, into the default cell. Pressed
    on the DM notification or from the inbox; either way the presser must
    be the one who answers for the destination склад."""
    transfer_id = int(callback.data.split(":", 1)[1])
    try:
        with get_conn(registry_db_path()) as conn:
            staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
            transfer = core_transfers.get_transfer(conn, transfer_id)
            if not staff or not transfer:
                raise core_transfers.TransferError("Перемещение не найдено.")
            target = core_warehouses.get_warehouse(conn, transfer["to_warehouse_id"])
            if not core_warehouses.may_receive(conn, staff, target, _location_ids(staff)):
                raise core_transfers.TransferError("Принять может только тот, кто отвечает за склад-получатель.")
            core_transfers.receive(conn, transfer_id, staff["id"])
            text = incoming_text(conn, transfer_id)
    except core_transfers.TransferError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await qa._safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        f"{text}\n\n✅ <b>Принято.</b> Товар на вашем складе.",
    )
    await callback.answer("Принято")
