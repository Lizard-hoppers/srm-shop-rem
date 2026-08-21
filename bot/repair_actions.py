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
would silently break their button."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery

from core import auth as core_auth
from core import notify as core_notify
from core import repairs as core_repairs
from core.storage import get_conn

router = Router()

_ACTOR_ROLES = ("owner", "admin", "master")


def _parse_order_id(callback_data: str) -> int:
    return int(callback_data.split(":", 1)[1])


async def _resolve_actor(callback: CallbackQuery):
    """Staff row for whoever pressed the button, or None (having already
    answered the callback with an alert) if they're not CRM staff with the
    right role to act on repairs."""
    with get_conn() as conn:
        staff = core_auth.get_staff_by_telegram_id(conn, callback.from_user.id)
    if not staff or staff["role"] not in _ACTOR_ROLES:
        await callback.answer("Вы не подключены как мастер в CRM.", show_alert=True)
        return None
    return staff


async def _sync_after_change(order_id: int) -> None:
    """Re-render every posted card for this order to match its current DB
    state — the same helper the web app calls after a manual status
    change, so both channels always agree."""
    with get_conn() as conn:
        repair = core_repairs.get_repair(conn, order_id)
        messages = core_repairs.get_order_messages(conn, order_id)
    core_notify.sync_repair_cards(
        messages, core_repairs.render_card_text(repair), core_repairs.render_keyboard(order_id, repair["status"])
    )


@router.callback_query(F.data.startswith("repair_take:"))
async def repair_take(callback: CallbackQuery) -> None:
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback)
    if not staff:
        return

    with get_conn() as conn:
        ok = core_repairs.claim_repair(conn, order_id, staff["id"])
        repair = None if ok else core_repairs.get_repair(conn, order_id)

    if not ok:
        if repair["status"] != "new":
            await callback.answer(f"Уже не в очереди (статус: {core_repairs.STATUS_LABELS[repair['status']]}).", show_alert=True)
        else:
            await callback.answer(f"Уже взял: {repair['master_name'] or 'другой мастер'}", show_alert=True)
        return

    await _sync_after_change(order_id)
    await callback.answer("Взяли в работу ✅")


@router.callback_query(F.data.startswith("repair_done:"))
async def repair_done(callback: CallbackQuery) -> None:
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback)
    if not staff:
        return

    override = staff["role"] in ("owner", "admin")
    with get_conn() as conn:
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

    await _sync_after_change(order_id)
    await callback.answer("Готов к выдаче ✅")


@router.callback_query(F.data.startswith("repair_release:"))
async def repair_cancel(callback: CallbackQuery) -> None:
    order_id = _parse_order_id(callback.data)
    staff = await _resolve_actor(callback)
    if not staff:
        return

    override = staff["role"] in ("owner", "admin")
    with get_conn() as conn:
        ok = core_repairs.cancel_repair(conn, order_id, staff["id"], override=override)
        repair = None if ok else core_repairs.get_repair(conn, order_id)

    if not ok:
        if repair["status"] != "in_progress":
            await callback.answer(f"Уже не в работе (статус: {core_repairs.STATUS_LABELS[repair['status']]}).", show_alert=True)
        else:
            await callback.answer(f"Ремонт не за вами (мастер: {repair['master_name'] or '—'}).", show_alert=True)
        return

    await _sync_after_change(order_id)
    await callback.answer("Отмечено как не отремонтированное")
