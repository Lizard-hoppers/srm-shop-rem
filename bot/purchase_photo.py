"""Staff sends a photo of a paper invoice to the bot in DM -> OpenAI vision
extracts line items -> a draft is saved and offered back for one-tap
confirm or in-app correction. Mirrors bot/repair_actions.py's shape
(resolve Telegram user -> CRM staff, act, sync/report back) but for
purchases instead of repairs.

Never writes to stock without a human confirming — a misread quantity
must not silently corrupt inventory counts. "Оприходовать как есть" only
succeeds when every line has both a qty and a resolvable cell (an
existing product being restocked has one via
core.inventory.default_cell_by_product; a brand-new product never does,
since a photo can't know where it physically goes) — otherwise it's an
all-or-nothing no-op that points back to "Открыть и поправить".
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

from bot.config import MINIAPP_URL
from core import auth as core_auth
from core import inventory as core_inventory
from core import purchase_import as core_purchase_import
from core import purchases as core_purchases
from core import vision_ocr
from core.storage import get_conn

router = Router()

_DRAFT_ROLES = ("owner", "admin", "storekeeper")


def _draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Оприходовать как есть", callback_data=f"draft_apply:{draft_id}")],
        [InlineKeyboardButton(
            text="✏️ Открыть и поправить",
            web_app=WebAppInfo(url=f"{MINIAPP_URL.rstrip('/')}/purchases/draft/{draft_id}"),
        )],
    ])


def _draft_preview_text(items: list[dict]) -> str:
    if not items:
        return "Не нашёл ни одной позиции на фото."
    lines = ["📦 <b>Распознано с фото:</b>", ""]
    for it in items:
        qty = it["qty"] if it["qty"] is not None else "?"
        cost = f" × {it['unit_cost']}" if it["unit_cost"] is not None else ""
        mark = "✅" if it["product_id"] else "🆕"
        lines.append(f"{mark} {it['name_guess']} — {qty} шт{cost}")
    lines.append("")
    lines.append("🆕 — не найдено в каталоге, будет создано новым товаром.")
    return "\n".join(lines)


@router.message(F.photo)
async def photo_invoice(message: Message) -> None:
    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, message.from_user.id)
    if not staff or staff["role"] not in _DRAFT_ROLES:
        return  # not staff with receiving rights — a client's photo, ignore silently in DM

    status_msg = await message.answer("📷 Распознаю накладную…")

    photo = message.photo[-1]
    file = await message.bot.get_file(photo.file_id)
    buf = await message.bot.download_file(file.file_path)

    try:
        raw_items = vision_ocr.extract_invoice_items(buf.read())
    except vision_ocr.VisionOcrError:
        await status_msg.edit_text(
            "Не смог распознать фото — попробуйте более чёткий снимок или введите приход вручную в приложении."
        )
        return

    if not raw_items:
        await status_msg.edit_text("Не нашёл ни одной позиции на фото — попробуйте более чёткий снимок.")
        return

    with get_conn() as conn:
        matched_items = core_purchase_import.match_items(conn, raw_items)
        draft_id = core_purchases.create_draft(conn, staff["id"], matched_items)

    await status_msg.edit_text(_draft_preview_text(matched_items), reply_markup=_draft_keyboard(draft_id))


@router.callback_query(F.data.startswith("draft_apply:"))
async def apply_draft(callback: CallbackQuery) -> None:
    draft_id = int(callback.data.split(":", 1)[1])

    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
        if not staff or staff["role"] not in _DRAFT_ROLES:
            await callback.answer("Вы не подключены как сотрудник склада в CRM.", show_alert=True)
            return

        draft = core_purchases.get_draft(conn, draft_id)
        if not draft or draft["status"] != "pending":
            await callback.answer("Черновик уже обработан.", show_alert=True)
            return

        items = core_purchases.get_draft_items(conn, draft_id)
        cell_by_product = core_inventory.default_cell_by_product(conn)

        receipt_items = []
        all_resolved = bool(items)
        for it in items:
            product_id = it["product_id"]
            cell_id = cell_by_product.get(product_id) if product_id else None
            if not it["qty"] or not cell_id:
                all_resolved = False
                continue
            receipt_items.append((product_id, cell_id, it["qty"], it["unit_cost"]))

        if not all_resolved or not receipt_items:
            await callback.answer(
                "Не для всех позиций есть готовая ячейка/количество — откройте «✏️ Открыть и поправить».",
                show_alert=True,
            )
            return

        core_purchases.create_receipt(conn, None, None, staff["id"], receipt_items)
        core_purchases.mark_draft_applied(conn, draft_id)

    await callback.message.edit_text("✅ Оприходовано.")
    await callback.answer("Готово")
