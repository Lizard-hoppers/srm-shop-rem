"""«📦 Приход → ➕ Добавить товар» (30.10, Павел): товар без накладной,
по одному, как в CRM — фото штрихкода, фото товара, цена продажи,
количество — и сразу на склад отдельным ПХ (core.purchases.receive_one).

Штрихкод читает zbar на сервере; знакомый штрихкод сразу называет товар,
и фото с названием уже не нужны. Не считался — можно ввести цифры или
завести товар без штрихкода. Название берётся с фото товара
(core.vision_ocr.extract_product_label — только название, цену с этикетки
никогда), не прочиталось — сотрудник пишет сам.

У точки может быть несколько складов (на 007 — «Андрей/Никита», товар
владельцев): тогда первым шагом — куда. Один экран на весь диалог: нажатие
кнопки правит его на месте, присланное фото или текст переотправляют вниз
чата. Состояния (ReceiveAdd) объявлены в bot.quick_actions — общая
❌ Отмена и запасные обработчики покрывают и этот диалог."""
from __future__ import annotations

import asyncio
import html
import logging
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import quick_actions as qa
from bot.part_add_flow import parse_cost
from bot.quick_actions import ReceiveAdd
from core import barcode_scan
from core import doc_cancel as core_doc_cancel
from core import documents as core_documents
from core import inventory as core_inventory
from core import product_photos
from core import purchases as core_purchases
from core import vision_ocr
from core import warehouses as core_warehouses
from core.documents import DocumentError
from core.storage import get_conn

logger = logging.getLogger(__name__)
router = Router()

_SKU_RE = re.compile(r"^\d{6,14}$")
_QTY_RE = re.compile(r"^\s*(\d{1,4})\s*(?:шт\.?)?\s*$", re.IGNORECASE)
_UNDO_ROLES = ("owner", "admin")

_ASK_BARCODE = "📷 Сфотографируйте <b>штрихкод</b> товара."
_ASK_DIGITS = "🔢 Напишите цифры штрихкода (6–14 цифр):"
_ASK_PHOTO = "📷 Теперь сфотографируйте <b>сам товар</b> — фото пойдёт в карточку, название прочитаю с него."
_ASK_NAME = "✏️ Напишите название товара:"
_ASK_PRICE = "💰 <b>Цена продажи</b> за 1 шт, грн? Напишите число."
_ASK_QTY = "📦 Сколько штук? Нажмите или напишите число."


def _money(value) -> str:
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:.2f}"


# ---- кто и где ----

def _actor(user_id: int):
    """(store, staff) or None — staff of a точка allowed to do приход."""
    resolved = qa._resolve_staff_for_dm(user_id)
    if not resolved or resolved[1]["role"] not in qa._PURCHASE_ROLES:
        return None
    return resolved


async def _actor_or_alert(callback: CallbackQuery):
    resolved = _actor(callback.from_user.id)
    if not resolved:
        await callback.answer("Недостаточно прав для прихода.", show_alert=True)
    return resolved


# ---- экран ----

def _screen(data: dict, question: str) -> str:
    """Шапка со складом, всё уже введённое, вопрос — ответы сотрудника
    удаляются по мере прочтения, так что видно введённое только здесь."""
    lines = [f"📦 <b>Приход товара</b> → {html.escape(data.get('warehouse_name') or '')}"]
    if data.get("sku"):
        lines.append(f"Штрихкод: <code>{html.escape(data['sku'])}</code>")
    if data.get("name"):
        mark = " (уже есть в базе)" if data.get("product_id") else ""
        lines.append(f"Товар: <b>{html.escape(data['name'])}</b>{mark}")
    if data.get("price"):
        lines.append(f"Цена продажи: {_money(data['price'])} грн")
    return "\n".join(lines) + "\n\n" + question


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[row for row in rows if row])


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


_BARCODE_KB = _kb([_btn("🔢 Ввести цифры", "rcv_digits")], [_btn("✏️ Товар без штрихкода", "rcv_nocode")])
_PHOTO_KB = _kb([_btn("⏭ Без фото — напишу название", "rcv_skipphoto")])
_QTY_KB = _kb([_btn(str(n), f"rcv_qty:{n}") for n in (1, 2, 3, 5, 10)])


def _price_kb(data: dict) -> InlineKeyboardMarkup:
    rows = []
    if data.get("product_id") and data.get("current_price"):
        rows.append([_btn(f"💰 {_money(data['current_price'])} грн, как сейчас", "rcv_price_keep")])
    rows.append([_btn("✏️ Другое название", "rcv_rename")])
    return _kb(*rows)


async def _show(message: Message, state: FSMContext, question: str, markup=None) -> None:
    """Ответ сообщением прочитан: съесть его, экран — вниз чата."""
    await qa._consume(state, message)
    await qa._repost(message, state, _screen(await state.get_data(), question), markup)


async def _edit(callback: CallbackQuery, state: FSMContext, question: str, markup=None) -> None:
    """Нажатие кнопки: экран и так последний — правим на месте."""
    await qa._advance_callback(callback, state, _screen(await state.get_data(), question), markup)


# ---- вход: «📦 Приход» → выбор ----

def purchase_menu() -> InlineKeyboardMarkup:
    return _kb([_btn("🧾 Накладная (фото)", "rcv_inv")], [_btn("➕ Добавить товар", "rcv_add")])


@router.callback_query(F.data == "rcv_inv")
async def receive_invoice(callback: CallbackQuery, state: FSMContext) -> None:
    """Накладная — как было: любое фото в личке без открытого диалога
    ловит bot/purchase_photo.py."""
    if not await _actor_or_alert(callback):
        return
    await state.clear()
    await qa._safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id,
                        "📦 Пришлите фото накладной — распознаю позиции и предложу оприходовать.")
    await callback.answer()


@router.callback_query(F.data == "rcv_add")
async def receive_add(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _actor_or_alert(callback)
    if not resolved:
        return
    store, _staff = resolved
    with get_conn(store.db_path) as conn:
        warehouses = core_warehouses.point_warehouses(conn, store.location_id)
    if not warehouses:
        await callback.answer("У точки нет склада.", show_alert=True)
        return
    if len(warehouses) == 1:
        await _start(callback, state, warehouses[0], edit=True)
    else:
        await state.set_state(ReceiveAdd.warehouse)
        await state.update_data(prompt_message_id=callback.message.message_id)
        rows = [[_btn(("🏪 " if i == 0 else "👥 ") + core_warehouses.display_name(w), f"rcv_wh:{w['id']}")]
                for i, w in enumerate(warehouses)]
        await qa._advance_callback(callback, state, "📦 <b>Приход товара</b>\n\nНа какой склад?", _kb(*rows))
    await callback.answer()


@router.callback_query(F.data.startswith("rcv_wh:"), ReceiveAdd.warehouse)
async def receive_pick_warehouse(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _actor_or_alert(callback)
    if not resolved:
        return
    store, _staff = resolved
    with get_conn(store.db_path) as conn:
        warehouse = core_warehouses.get_warehouse(conn, int(callback.data.split(":", 1)[1]))
    if not warehouse or warehouse["kind"] != "point" or warehouse["location_id"] != store.location_id:
        await callback.answer("Этот склад не относится к вашей точке.", show_alert=True)
        return
    await _start(callback, state, warehouse, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("rcv_next:"))
async def receive_next(callback: CallbackQuery, state: FSMContext) -> None:
    """«➕ Следующий товар» под готовым ПХ — та карточка остаётся (на ней
    «↩️ Вернуть»), новый экран — вниз чата."""
    resolved = await _actor_or_alert(callback)
    if not resolved:
        return
    store, _staff = resolved
    with get_conn(store.db_path) as conn:
        warehouse = core_warehouses.get_warehouse(conn, int(callback.data.split(":", 1)[1]))
    if not warehouse or warehouse["kind"] != "point" or warehouse["location_id"] != store.location_id:
        await callback.answer("Этот склад не относится к вашей точке.", show_alert=True)
        return
    await _start(callback, state, warehouse, edit=False)
    await callback.answer()


async def _start(callback: CallbackQuery, state: FSMContext, warehouse, *, edit: bool) -> None:
    old = await state.get_data()
    leftovers = list(old.get("user_message_ids", []))
    keep = callback.message.message_id if edit else None
    if old.get("prompt_message_id") and old["prompt_message_id"] != keep:
        leftovers.append(old["prompt_message_id"])
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, leftovers)
    await state.set_state(ReceiveAdd.barcode)
    await state.update_data(warehouse_id=warehouse["id"], warehouse_name=core_warehouses.display_name(warehouse))
    data = await state.get_data()
    if edit:
        await state.update_data(prompt_message_id=callback.message.message_id)
        await _edit(callback, state, _ASK_BARCODE, _BARCODE_KB)
    else:
        text = _screen(data, _ASK_BARCODE)
        sent = await callback.message.answer(text, reply_markup=_BARCODE_KB)
        await state.update_data(prompt_message_id=sent.message_id, prompt_text=text, prompt_markup=_BARCODE_KB)


# ---- штрихкод ----

async def _by_sku(message: Message, state: FSMContext, store, sku: str) -> None:
    """Штрихкод известен (прочитан или введён): знакомый товар — сразу к
    цене, новый — к фото товара."""
    with get_conn(store.db_path) as conn:
        product = core_inventory.get_product_by_sku(conn, sku)
    if product and product["is_serial"]:
        await qa._nudge(message, state, f"«{product['name']}» — товар с IMEI, его приходуют в приложении.")
        return
    if product:
        await state.update_data(sku=sku, product_id=product["id"], name=product["name"], current_price=product["price"])
        await state.set_state(ReceiveAdd.price)
        await _show(message, state, _ASK_PRICE, _price_kb(await state.get_data()))
    else:
        await state.update_data(sku=sku, product_id=None, name=None, current_price=None)
        await state.set_state(ReceiveAdd.photo)
        await _show(message, state, "🆕 Новый товар.\n" + _ASK_PHOTO, _PHOTO_KB)


@router.message(ReceiveAdd.barcode, F.photo)
async def receive_barcode_photo(message: Message, state: FSMContext) -> None:
    resolved = _actor(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved
    data = await state.get_data()
    if data.get("prompt_message_id"):
        await qa._safe_edit(message.bot, message.chat.id, data["prompt_message_id"], _screen(data, "⏳ Читаю штрихкод…"))
    file = await message.bot.get_file(message.photo[-1].file_id)
    photo_bytes = (await message.bot.download_file(file.file_path)).read()
    sku = await asyncio.to_thread(barcode_scan.decode_barcode, photo_bytes)
    if sku:
        await _by_sku(message, state, store, sku)
        return
    await qa._consume(state, message)
    await qa._repost(
        message, state,
        _screen(data, "⚠️ Не считал штрихкод. Снимите ближе и ровнее — или введите цифры вручную.\n\n" + _ASK_BARCODE),
        _BARCODE_KB, remember=_screen(data, _ASK_BARCODE),
    )


@router.message(ReceiveAdd.barcode, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
@router.message(ReceiveAdd.digits, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def receive_digits(message: Message, state: FSMContext) -> None:
    resolved = _actor(message.from_user.id)
    if not resolved:
        return
    digits = re.sub(r"[\s-]", "", message.text)
    if not _SKU_RE.match(digits):
        await qa._nudge(message, state, "Штрихкод — это 6–14 цифр. Или пришлите его фото.")
        return
    await _by_sku(message, state, resolved[0], digits)


@router.callback_query(F.data == "rcv_digits", ReceiveAdd.barcode)
async def receive_ask_digits(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _actor_or_alert(callback):
        return
    await state.set_state(ReceiveAdd.digits)
    await _edit(callback, state, _ASK_DIGITS, _kb([_btn("✏️ Товар без штрихкода", "rcv_nocode")]))
    await callback.answer()


@router.callback_query(F.data == "rcv_nocode", ReceiveAdd.barcode)
@router.callback_query(F.data == "rcv_nocode", ReceiveAdd.digits)
async def receive_no_code(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _actor_or_alert(callback):
        return
    await state.update_data(sku=None, product_id=None, name=None, current_price=None)
    await state.set_state(ReceiveAdd.photo)
    await _edit(callback, state, _ASK_PHOTO, _PHOTO_KB)
    await callback.answer()


# ---- фото товара и название ----

@router.message(ReceiveAdd.photo, F.photo)
async def receive_product_photo(message: Message, state: FSMContext) -> None:
    resolved = _actor(message.from_user.id)
    if not resolved:
        return
    store, _staff = resolved
    data = await state.get_data()
    if data.get("prompt_message_id"):
        await qa._safe_edit(message.bot, message.chat.id, data["prompt_message_id"], _screen(data, "⏳ Читаю название с фото…"))
    file_id = message.photo[-1].file_id
    file = await message.bot.get_file(file_id)
    photo_bytes = (await message.bot.download_file(file.file_path)).read()
    await state.update_data(photo_file_id=file_id)
    name = None
    try:
        read = await asyncio.to_thread(vision_ocr.extract_product_label, photo_bytes)
        name = " ".join((read.get("name") or "").split()) or None
    except vision_ocr.VisionOcrError:
        logger.info("receive_add: label not read", exc_info=True)
    if name:
        with get_conn(store.db_path) as conn:
            product = _same_name(conn, name)
        await _to_price(state, product, name)
        await _show(message, state, _ASK_PRICE, _price_kb(await state.get_data()))
    else:
        await state.set_state(ReceiveAdd.name)
        await _show(message, state, "Фото есть, а название с него не прочитал.\n" + _ASK_NAME)


@router.callback_query(F.data == "rcv_skipphoto", ReceiveAdd.photo)
async def receive_skip_photo(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _actor_or_alert(callback):
        return
    await state.set_state(ReceiveAdd.name)
    await _edit(callback, state, _ASK_NAME)
    await callback.answer()


@router.callback_query(F.data == "rcv_rename", ReceiveAdd.price)
async def receive_rename(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _actor_or_alert(callback):
        return
    # Другое название — значит, и товар другой: знакомый штрихкод (он
    # назвал тот, найденный товар) к новому уже не относится.
    data = await state.get_data()
    await state.update_data(product_id=None, name=None, current_price=None,
                            sku=None if data.get("product_id") else data.get("sku"))
    await state.set_state(ReceiveAdd.name)
    await _edit(callback, state, _ASK_NAME)
    await callback.answer()


@router.message(ReceiveAdd.photo, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
@router.message(ReceiveAdd.name, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def receive_name(message: Message, state: FSMContext) -> None:
    resolved = _actor(message.from_user.id)
    if not resolved:
        return
    name = " ".join(message.text.split())
    if len(name) < 2:
        await qa._nudge(message, state, "Название слишком короткое.")
        return
    with get_conn(resolved[0].db_path) as conn:
        product = _same_name(conn, name)
    await _to_price(state, product, name)
    await _show(message, state, _ASK_PRICE, _price_kb(await state.get_data()))


def _same_name(conn, name: str):
    """Товар без штрихкода с ровно таким названием — тот же товар."""
    return conn.execute(
        "SELECT * FROM products WHERE name = ? AND is_serial = 0 ORDER BY id LIMIT 1", (name,)
    ).fetchone()


async def _to_price(state: FSMContext, product, name: str) -> None:
    if product:
        await state.update_data(product_id=product["id"], name=product["name"], current_price=product["price"])
    else:
        await state.update_data(product_id=None, name=name, current_price=None)
    await state.set_state(ReceiveAdd.price)


# ---- цена продажи, количество, готово ----

@router.message(ReceiveAdd.price, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def receive_price(message: Message, state: FSMContext) -> None:
    if not _actor(message.from_user.id):
        return
    price = parse_cost(message.text)
    if price is None or round(price) <= 0:
        await qa._nudge(message, state, "Напишите цену продажи числом, например 1200.")
        return
    await state.update_data(price=int(round(price)))
    await state.set_state(ReceiveAdd.qty)
    await _show(message, state, _ASK_QTY, _QTY_KB)


@router.callback_query(F.data == "rcv_price_keep", ReceiveAdd.price)
async def receive_price_keep(callback: CallbackQuery, state: FSMContext) -> None:
    if not await _actor_or_alert(callback):
        return
    data = await state.get_data()
    if not data.get("current_price"):
        await callback.answer("Цены нет — напишите число.", show_alert=True)
        return
    await state.update_data(price=int(data["current_price"]))
    await state.set_state(ReceiveAdd.qty)
    await _edit(callback, state, _ASK_QTY, _QTY_KB)
    await callback.answer()


@router.message(ReceiveAdd.qty, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def receive_qty(message: Message, state: FSMContext) -> None:
    resolved = _actor(message.from_user.id)
    if not resolved:
        return
    match = _QTY_RE.match(message.text)
    if not match or int(match.group(1)) < 1:
        await qa._nudge(message, state, "Напишите количество целым числом, например 4.")
        return
    data = await state.get_data()
    text, markup = await _save(message.bot, resolved, data, int(match.group(1)), key=f"rcv:{message.chat.id}:{data.get('prompt_message_id')}")
    if markup is None:
        await qa._nudge(message, state, text)
        return
    await qa._consume(state, message)
    await qa._repost(message, state, text, markup)
    await _finish(message.bot, message.chat.id, state)


@router.callback_query(F.data.startswith("rcv_qty:"), ReceiveAdd.qty)
async def receive_qty_button(callback: CallbackQuery, state: FSMContext) -> None:
    resolved = await _actor_or_alert(callback)
    if not resolved:
        return
    data = await state.get_data()
    text, markup = await _save(callback.bot, resolved, data, int(callback.data.split(":", 1)[1]),
                               key=f"rcv:{callback.message.chat.id}:{callback.message.message_id}")
    if markup is None:
        await callback.answer(text, show_alert=True)
        return
    await qa._safe_edit(callback.bot, callback.message.chat.id, data.get("prompt_message_id") or callback.message.message_id, text, markup)
    await _finish(callback.bot, callback.message.chat.id, state)
    await callback.answer("Оприходовано")


async def _finish(bot, chat_id: int, state: FSMContext) -> None:
    """Диалог закрыт — карточка ПХ остаётся, недоудалённые ответы уходят."""
    data = await state.get_data()
    await state.clear()
    await qa._safe_delete_many(bot, chat_id, list(data.get("user_message_ids", [])))


async def _save(bot, resolved, data: dict, qty: int, *, key: str) -> tuple[str, InlineKeyboardMarkup | None]:
    """Провести ПХ. (карточка, кнопки) — или (почему нет, None)."""
    store, staff = resolved
    photo_bytes = None
    if data.get("photo_file_id"):
        try:
            file = await bot.get_file(data["photo_file_id"])
            photo_bytes = (await bot.download_file(file.file_path)).read()
        except Exception:  # noqa: BLE001 — без фото товар всё равно оприходуем
            logger.warning("receive_add: product photo not downloaded", exc_info=True)

    def work():
        with get_conn(store.db_path) as conn:
            result = core_purchases.receive_one(
                conn, staff_id=staff["id"], location_id=store.location_id, warehouse_id=data["warehouse_id"],
                qty=qty, price=data["price"], product_id=data.get("product_id"),
                name=data.get("name"), sku=data.get("sku"), key=key,
            )
            if photo_bytes:
                product = core_inventory.get_product(conn, result["product_id"])
                if result["created"] or not product["photo_path"]:
                    product_photos.save(conn, result["product_id"], photo_bytes)
            return result

    try:
        result = await asyncio.to_thread(work)
    except core_purchases.ReceiptError as exc:
        return str(exc), None
    return _done_card(result, data.get("warehouse_name") or ""), _done_kb(result)


def _done_card(result: dict, warehouse_name: str) -> str:
    new = " 🆕" if result["created"] else ""
    return (
        f"✅ <b>{html.escape(result['label'])}</b> · {html.escape(warehouse_name)}\n"
        f"{html.escape(result['name'])}{new}\n"
        f"{result['qty']} шт · цена продажи {_money(result['price'])} грн\n"
        f"На складе теперь: {result['in_warehouse']} шт"
    )


def _done_kb(result: dict) -> InlineKeyboardMarkup:
    return _kb(
        [_btn("➕ Следующий товар", f"rcv_next:{result['warehouse_id']}")],
        [_btn("↩️ Вернуть", f"rcv_undo:{result['doc_id']}")],
    )


@router.callback_query(F.data.startswith("rcv_undo:"))
async def receive_undo(callback: CallbackQuery, state: FSMContext) -> None:
    """Ошибся — ПХ отменяется (товар снимается со склада, документ остаётся
    в журнале отменённым). Свой ПХ — сам сотрудник, любой — владелец/админ."""
    resolved = await _actor_or_alert(callback)
    if not resolved:
        return
    store, staff = resolved
    doc_id = int(callback.data.split(":", 1)[1])
    try:
        with get_conn(store.db_path) as conn:
            doc = core_documents.get(conn, doc_id)
            if not doc or doc["doc_type"] != "receipt":
                raise DocumentError("Документ не найден.")
            if doc["staff_id"] != staff["id"] and staff["role"] not in _UNDO_ROLES:
                raise DocumentError("Вернуть может тот, кто оприходовал, или владелец.")
            core_doc_cancel.cancel_document(conn, doc_id, staff["id"], "Вернуть — кнопка в боте")
            label = core_documents.doc_label(doc)
            warehouse_id = conn.execute(
                """SELECT storage_cells.warehouse_id FROM goods_receipt_items
                   JOIN storage_cells ON storage_cells.id = goods_receipt_items.cell_id
                   WHERE goods_receipt_items.receipt_id = ? LIMIT 1""",
                (doc["ref_id"],),
            ).fetchone()["warehouse_id"]
    except DocumentError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    old_text = callback.message.html_text if callback.message.text else ""
    await qa._safe_edit(
        callback.bot, callback.message.chat.id, callback.message.message_id,
        f"<s>{old_text}</s>\n\n↩️ {html.escape(label)} отменён — товар снят со склада.",
        _kb([_btn("➕ Следующий товар", f"rcv_next:{warehouse_id}")]),
    )
    await callback.answer("Отменено")
