"""«➕ Новая запчасть» on the part picker (15.10, Павел): the part isn't on
the master's склад — he bought it himself. He sends a photo of its label
or barcode (or just types the name), says what it cost, and the part
comes onto his склад as its own партия at that price and goes straight
into the repair — so the repair's profit carries the real purchase cost
(core.repairs.buy_part).

Recognition: zbar reads the barcode locally first; a штрихкод the base
already knows names the product outright. Otherwise the model reads the
label (core.vision_ocr.extract_product_label) for the name. Nothing read
at all — the master is asked to type the name.

One screen all the way: the picker message turns into the dialog (a
button tap edits in place), each typed answer reposts it to the bottom of
the chat, and at the end it is the picker again with the part written
off. The states (PartAdd) are declared in bot.quick_actions so its shared
❌ Отмена and fallback handlers cover this flow too."""
from __future__ import annotations

import asyncio
import html
import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot import quick_actions as qa
from bot import repair_actions as ra
from bot.quick_actions import PartAdd
from core import auth as core_auth
from core import barcode_scan
from core import documents as core_documents
from core import inventory as core_inventory
from core import repairs as core_repairs
from core import vision_ocr
from core.storage import get_conn
from core.stores import get_store, registry_db_path

router = Router()

_ASK_PHOTO = (
    "📷 Пришлите фото запчасти — этикетку или штрихкод покрупнее.\n"
    "Этикетки нет — просто напишите название."
)
_ASK_NAME = "✏️ Напишите название запчасти:"
_COST_RE = re.compile(r"^\s*(\d[\d\s]*(?:[.,]\d{1,2})?)\s*(?:грн\.?|гривен|гривень|uah|₴)?\s*$", re.IGNORECASE)
_SKU_RE = re.compile(r"^\d{6,14}$")


def _money(value) -> str:
    value = float(value)
    return str(int(value)) if value == int(value) else f"{value:.2f}"


def parse_cost(text: str | None):
    """«1200», «1 200 грн», «350,50» → число; anything else → None."""
    match = _COST_RE.match(text or "")
    if not match:
        return None
    value = float(match.group(1).replace(" ", "").replace(",", "."))
    return value if value > 0 else None


def _context(user_id: int, order_id: int):
    """((store, staff, repair), None) or (None, why not)."""
    with get_conn(registry_db_path()) as conn:
        repair = core_repairs.get_repair(conn, order_id)
        staff = core_auth.get_staff_by_telegram_id(conn, user_id)
    if not repair or not staff or staff["role"] not in ra._ACTOR_ROLES:
        return None, "Ремонт не найден или у вас нет доступа."
    if not ra._may_touch_parts(repair, staff):
        return None, "Ремонт не за вами."
    if repair["status"] not in ("in_progress", "ready"):
        return None, "Ремонт уже закрыт — запчасть не добавить."
    try:
        store = get_store(str(repair["location_id"]))
    except KeyError:
        return None, "Точка ремонта больше не настроена."
    return (store, staff, repair), None


def _head(repair) -> str:
    device = " ".join(x for x in (repair["device_type"], repair["brand"], repair["model"]) if x)
    return f"⚙️ <b>{core_documents.label('repair', repair['id'])}</b> · {html.escape(device)}\n➕ <b>Новая запчасть</b>\n\n"


def _back(order_id: int) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text="↩️ К списку деталей", callback_data=f"rp_back:{order_id}")]


def _cost_screen(repair, data: dict) -> tuple[str, InlineKeyboardMarkup]:
    lines = [_head(repair) + f"Запчасть: <b>{html.escape(data['name'])}</b>"]
    if data.get("product_id"):
        lines[0] += " (уже есть в базе)"
    if data.get("sku"):
        lines.append(f"Штрихкод: {html.escape(data['sku'])}")
    lines.append("")
    lines.append("💰 Сколько она стоила при закупке, грн за 1 шт? Напишите число.")
    rows = []
    if data.get("last_cost"):
        rows.append([InlineKeyboardButton(
            text=f"💰 {_money(data['last_cost'])} грн, как в прошлый раз", callback_data=f"rp_cost_last:{repair['id']}",
        )])
    rows.append([InlineKeyboardButton(text="✏️ Другое название", callback_data=f"rp_rename:{repair['id']}")])
    rows.append(_back(repair["id"]))
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


def _known(conn, *, sku: str | None = None, name: str | None = None):
    if sku:
        product = core_inventory.get_product_by_sku(conn, sku)
        if product:
            return product
    if name:
        return conn.execute("SELECT * FROM products WHERE name = ? AND is_serial = 0 ORDER BY id LIMIT 1", (name,)).fetchone()
    return None


async def _to_cost(state: FSMContext, store, product=None, *, name: str | None = None, sku: str | None = None) -> None:
    """Remember what the part is and move to the price question."""
    if product:
        with get_conn(store.db_path) as conn:
            last = core_repairs.last_part_cost(conn, product["id"])
        await state.update_data(product_id=product["id"], name=product["name"], sku=product["sku"], last_cost=last)
    else:
        await state.update_data(product_id=None, name=name, sku=sku, last_cost=None)
    await state.set_state(PartAdd.cost)


async def _show(message: Message, state: FSMContext, text: str, markup=None) -> None:
    """A typed answer was read: the screen goes to the bottom of the chat."""
    await qa._repost(message, state, text, markup)


async def _finish(bot, chat_id: int, state: FSMContext) -> None:
    """Close the dialog — the screen stays (it is the picker again), the
    master's typed answers that couldn't be deleted on the spot go."""
    data = await state.get_data()
    await state.clear()
    await qa._safe_delete_many(bot, chat_id, list(data.get("user_message_ids", [])))


async def _abort(message: Message, state: FSMContext, why: str) -> None:
    await qa._consume(state, message)
    data = await state.get_data()
    if data.get("prompt_message_id"):
        await qa._safe_edit(message.bot, message.chat.id, data["prompt_message_id"], f"⚠️ {html.escape(why)}")
    await _finish(message.bot, message.chat.id, state)


def _save(store, staff, order_id: int, data: dict, cost) -> tuple[str, InlineKeyboardMarkup]:
    """Post the part; the picker screen to show after. Raises RepairPartError."""
    with get_conn(store.db_path) as conn:
        result = core_repairs.buy_part(
            conn, order_id, staff["id"], unit_cost=cost,
            product_id=data.get("product_id"), name=data.get("name"), sku=data.get("sku"),
        )
        text, keyboard = ra._part_screen(conn, order_id)
    ra._sync_after_change(order_id, store.db_path)
    done = f"✅ {html.escape(result['name'])} — {_money(result['unit_cost'])} грн: оприходована на ваш склад и списана в ремонт."
    return f"{done}\n\n{text}", keyboard


# ---- entry ----

@router.callback_query(F.data.startswith("rp_new:"))
async def part_new(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = ra._parse_order_id(callback.data)
    ctx, error = _context(callback.from_user.id, order_id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    _store, _staff, repair = ctx
    # Whatever dialog was open is dropped with its traces — this message
    # (the picker) becomes the screen of this one.
    old = await state.get_data()
    leftovers = list(old.get("user_message_ids", []))
    if old.get("prompt_message_id") and old["prompt_message_id"] != callback.message.message_id:
        leftovers.append(old["prompt_message_id"])
    await state.clear()
    await qa._safe_delete_many(callback.bot, callback.message.chat.id, leftovers)
    await state.set_state(PartAdd.photo)
    await state.update_data(order_id=order_id, prompt_message_id=callback.message.message_id)
    await qa._advance_callback(callback, state, _head(repair) + _ASK_PHOTO, InlineKeyboardMarkup(inline_keyboard=[_back(order_id)]))
    await callback.answer()


@router.callback_query(F.data.startswith("rp_back:"))
async def part_back(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = ra._parse_order_id(callback.data)
    ctx = await ra._dm_context(callback, order_id)
    if not ctx:
        return
    store, _staff, _repair = ctx
    with get_conn(store.db_path) as conn:
        text, keyboard = ra._part_screen(conn, order_id)
    await qa._safe_edit(callback.bot, callback.message.chat.id, callback.message.message_id, text, keyboard)
    await _finish(callback.bot, callback.message.chat.id, state)
    await callback.answer()


# ---- what the part is ----

@router.message(PartAdd.photo, F.photo)
async def part_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ctx, error = _context(message.from_user.id, data["order_id"])
    if error:
        await _abort(message, state, error)
        return
    store, _staff, repair = ctx
    await qa._consume(state, message)
    if data.get("prompt_message_id"):
        await qa._safe_edit(message.bot, message.chat.id, data["prompt_message_id"], _head(repair) + "⏳ Распознаю фото…")
    file = await message.bot.get_file(message.photo[-1].file_id)
    photo_bytes = (await message.bot.download_file(file.file_path)).read()

    sku = await asyncio.to_thread(barcode_scan.decode_barcode, photo_bytes)
    with get_conn(store.db_path) as conn:
        product = _known(conn, sku=sku)
    name = None
    if not product:
        try:
            read = await asyncio.to_thread(vision_ocr.extract_product_label, photo_bytes)
            name, sku = read["name"], sku or read["sku"]
        except vision_ocr.VisionOcrError:
            pass
        with get_conn(store.db_path) as conn:
            product = _known(conn, sku=sku)

    if product or name:
        await _to_cost(state, store, product, name=name, sku=sku)
        await _show(message, state, *_cost_screen(repair, await state.get_data()))
    elif sku:
        await state.update_data(product_id=None, sku=sku)
        await state.set_state(PartAdd.name)
        await _show(message, state, _head(repair) + f"Штрихкод {html.escape(sku)} прочитал, а название — нет.\n\n{_ASK_NAME}",
                    InlineKeyboardMarkup(inline_keyboard=[_back(repair["id"])]))
    else:
        await _show(message, state, _head(repair) + "⚠️ Не разобрал фото. Снимите этикетку ближе или напишите название текстом.\n\n" + _ASK_PHOTO,
                    InlineKeyboardMarkup(inline_keyboard=[_back(repair["id"])]))


@router.message(PartAdd.photo, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
@router.message(PartAdd.name, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def part_name(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    ctx, error = _context(message.from_user.id, data["order_id"])
    if error:
        await _abort(message, state, error)
        return
    store, _staff, repair = ctx
    text = " ".join(message.text.split())
    if len(text) < 2:
        await qa._nudge(message, state, "Название слишком короткое.")
        return
    await qa._consume(state, message)
    asking_name = await state.get_state() == PartAdd.name.state
    sku = data.get("sku") if asking_name else None
    with get_conn(store.db_path) as conn:
        if not asking_name and _SKU_RE.match(text):
            # Typed the barcode digits instead of a name.
            product = _known(conn, sku=text)
            if not product:
                await state.update_data(product_id=None, sku=text)
                await state.set_state(PartAdd.name)
                await _show(message, state, _head(repair) + f"Штрихкод {html.escape(text)} в базе не найден.\n\n{_ASK_NAME}",
                            InlineKeyboardMarkup(inline_keyboard=[_back(repair["id"])]))
                return
        else:
            product = _known(conn, name=text)
    await _to_cost(state, store, product, name=text, sku=sku)
    await _show(message, state, *_cost_screen(repair, await state.get_data()))


@router.callback_query(F.data.startswith("rp_rename:"), PartAdd.cost)
async def part_rename(callback: CallbackQuery, state: FSMContext) -> None:
    order_id = ra._parse_order_id(callback.data)
    ctx, error = _context(callback.from_user.id, order_id)
    if error:
        await callback.answer(error, show_alert=True)
        return
    await state.update_data(product_id=None, sku=None, last_cost=None)
    await state.set_state(PartAdd.name)
    await qa._advance_callback(callback, state, _head(ctx[2]) + _ASK_NAME, InlineKeyboardMarkup(inline_keyboard=[_back(order_id)]))
    await callback.answer()


# ---- the price, and done ----

@router.message(PartAdd.cost, F.text, ~F.text.in_(qa._ENTRY_BUTTONS))
async def part_cost(message: Message, state: FSMContext) -> None:
    cost = parse_cost(message.text)
    if cost is None:
        await qa._nudge(message, state, "Напишите цену закупки числом, например 1200.")
        return
    data = await state.get_data()
    ctx, error = _context(message.from_user.id, data["order_id"])
    if error:
        await _abort(message, state, error)
        return
    store, staff, _repair = ctx
    try:
        text, keyboard = _save(store, staff, data["order_id"], data, cost)
    except core_repairs.RepairPartError as exc:
        await qa._nudge(message, state, str(exc))
        return
    await qa._consume(state, message)
    await _show(message, state, text, keyboard)
    await _finish(message.bot, message.chat.id, state)


@router.callback_query(F.data.startswith("rp_cost_last:"), PartAdd.cost)
async def part_cost_last(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    ctx, error = _context(callback.from_user.id, data["order_id"])
    if error or not data.get("last_cost"):
        await callback.answer(error or "Прошлой цены нет — напишите число.", show_alert=True)
        return
    store, staff, _repair = ctx
    try:
        text, keyboard = _save(store, staff, data["order_id"], data, data["last_cost"])
    except core_repairs.RepairPartError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await qa._safe_edit(callback.bot, callback.message.chat.id, data.get("prompt_message_id") or callback.message.message_id, text, keyboard)
    await _finish(callback.bot, callback.message.chat.id, state)
    await callback.answer("Добавлено")
