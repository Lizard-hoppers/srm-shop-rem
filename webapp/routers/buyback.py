"""Покупка телефона у клиента (core.buyback.create_purchase, Заход 4) —
the Mini App side of the same document the bot's «🛒 Покупка» creates:
seller by phone number, model, IMEI, поломки, price in any currency, the
payout split across the точка's accounts, up to six photos. The покупка's
own page is its card: photos, the IMEI as a scannable barcode with a
printable label, how it was paid, where the phone is now.
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from starlette.datastructures import UploadFile as StarletteUploadFile

from core import accounts as core_accounts
from core import barcode_label
from core import buyback as core_buyback
from core import cash as core_cash
from core import documents as core_documents
from core import inventory as core_inventory
from core import production as core_production
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, require_role, require_staff
from webapp.payments import payments_from_form
from webapp.templating import render

router = APIRouter(prefix="/buyback")

# Buying touches both cash (money out) and stock (a unit entering it) —
# same trust boundary as receiving from a supplier.
_BUYBACK_ROLES = ("owner", "admin", "storekeeper")

_PHOTO_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)


async def _read_photo(upload) -> tuple[bytes, str] | None:
    """None if this file input was left empty; ValueError (user-facing) on
    a real but invalid upload (wrong type, too big)."""
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        return None
    ext = _PHOTO_EXT_BY_CONTENT_TYPE.get(upload.content_type)
    if not ext:
        raise ValueError("Фото устройства должно быть JPEG, PNG или WebP.")
    data = await upload.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        raise ValueError("Фото устройства слишком большое (максимум 15 МБ).")
    return data, ext


def _list_context(location_id: int) -> dict:
    with get_conn() as conn:
        return {
            "orders": core_buyback.list_buyback_orders(conn, location_id=location_id),
            "accounts": core_accounts.list_accounts(conn, location_id),
            "currencies": core_accounts.CURRENCIES,
            "photo_slots": list(enumerate(core_buyback.PHOTO_SLOTS)),
            "model_suggestions": sorted({
                *core_buyback.recent_models(conn, limit=30),
                *(p["name"] for p in core_inventory.list_products(conn) if p["is_serial"]),
            }),
        }


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    return render(request, "buyback_list.html", staff=staff, **_list_context(loc(request)))


@router.post("")
async def create_view(
    request: Request, background_tasks: BackgroundTasks, staff=Depends(require_role(*_BUYBACK_ROLES)),
):
    """Every field arrives as a plain form value so a submission missing
    something re-renders the page with a plain-Russian error instead of a
    raw 422."""
    form = await request.form()

    def _error(message: str):
        return render(request, "buyback_list.html", staff=staff, error=message, **_list_context(loc(request)))

    photos = []
    try:
        for position, label in enumerate(core_buyback.PHOTO_SLOTS):
            photo = await _read_photo(form.get(f"photo_{position}"))
            if photo:
                photos.append((photo[0], photo[1], label))
    except ValueError as exc:
        return _error(str(exc))
    if not photos:
        return _error("Загрузите хотя бы одно фото устройства.")

    key = idem_key("buyback", form.get("idem"))
    try:
        with get_conn() as conn:
            already = core_documents.find_by_key(conn, key)
            if already:
                return RedirectResponse(link(request, f"/buyback/{already['ref_id']}"), status_code=303)
            order_id = core_buyback.create_purchase(
                conn,
                seller_phone=form.get("seller_phone") or "", seller_name=form.get("seller_name"),
                model=form.get("model") or "", imei=form.get("imei") or "", comment=form.get("comment"),
                price=form.get("price"), currency=form.get("currency") or "UAH", rate=form.get("rate"),
                payments=payments_from_form(form) or None, photos=photos,
                staff_id=staff["id"], location_id=loc(request), key=key,
            )
    except (core_buyback.PurchaseError, core_cash.PaymentError, core_inventory.DuplicateImeiError) as exc:
        return _error(str(exc))

    store = request.state.store
    background_tasks.add_task(core_buyback.post_card_to_group, store, order_id)
    return RedirectResponse(link(request, f"/buyback/{order_id}"), status_code=303)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        order = core_buyback.get_buyback_order(conn, order_id)
        if not order:
            return RedirectResponse(link(request, "/buyback"), status_code=303)
        document = core_documents.get_for(conn, "buyback", order_id)
        ctx = {
            "order": order,
            "photos": core_buyback.get_photos(conn, order_id),
            "payments": core_cash.payments_for(conn, "buyback_order", order_id),
            "document": document,
            "document_label": core_documents.doc_label(document) if document else None,
            # Where the phone is right now (None once sold / written off).
            "unit": core_inventory.find_unit_by_imei(conn, order["imei"]) if order["imei"] else None,
            "production_order": (production_order := core_production.active_order_for_batch(conn, order["batch_id"])
                                 if order["batch_id"] else None),
            "production_label": core_documents.label("production", production_order["id"]) if production_order else None,
            "can_produce": staff["role"] in _BUYBACK_ROLES,
            "where": next(
                (b["where_text"] for b in core_inventory.list_batches(conn, order["product_id"])
                 if b["id"] == order["batch_id"]), None,
            ) if order["product_id"] and order["batch_id"] else None,
        }
    return render(request, "buyback_detail.html", staff=staff, **ctx)


@router.get("/{order_id}/barcode.png")
def barcode_view(order_id: int, compact: bool = False, staff=Depends(require_staff)):
    """The phone's IMEI as a Code128 label — model on top, the покупка's
    number underneath. Scanning it anywhere a code is accepted (Склад,
    Продажа, Перемещение) finds this exact unit."""
    with get_conn() as conn:
        order = core_buyback.get_buyback_order(conn, order_id)
    if not order or not (order["imei"] or order["serial_number"]):
        raise HTTPException(status_code=404, detail="У этой покупки нет IMEI для штрих-кода.")
    png = barcode_label.generate_label_png(
        order["imei"] or order["serial_number"], order["model"] or order["device_type"], None,
        compact=compact, footer=core_documents.label("buyback", order_id),
    )
    return Response(content=png, media_type="image/png")
