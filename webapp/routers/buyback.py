from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from core import buyback as core_buyback
from core import cash as core_cash
from core import clients as core_clients
from core.storage import get_conn
from webapp.deps import link, optional_int, require_role, require_staff
from webapp.templating import render

router = APIRouter(prefix="/buyback")

# Mirrors webapp.routers.purchases._STOCK_WRITE_ROLES — buyback touches
# both cash (money out) and stock (a resale item entering the catalog),
# same trust boundary as receiving from a supplier.
_BUYBACK_ROLES = ("owner", "admin", "storekeeper")

_PHOTO_EXT_BY_CONTENT_TYPE = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_MAX_PHOTO_BYTES = 15 * 1024 * 1024  # comfortably under nginx's client_max_body_size (20M)


async def _validate_intake_photo(upload) -> tuple[bytes, str] | None:
    """Same shape as webapp.routers.repairs._validate_intake_photo — None
    if the file input was left empty, raises ValueError on a real but
    invalid upload (wrong type, too big)."""
    if not isinstance(upload, StarletteUploadFile) or not upload.filename:
        return None
    ext = _PHOTO_EXT_BY_CONTENT_TYPE.get(upload.content_type)
    if not ext:
        raise ValueError("Фото устройства должно быть JPEG, PNG или WebP.")
    data = await upload.read(_MAX_PHOTO_BYTES + 1)
    if len(data) > _MAX_PHOTO_BYTES:
        raise ValueError("Фото устройства слишком большое (максимум 15 МБ).")
    return data, ext


def _list_context(purpose: str | None) -> dict:
    with get_conn() as conn:
        return {
            "orders": core_buyback.list_buyback_orders(conn, purpose=purpose),
            "purpose": purpose,
            "purposes": core_buyback.PURPOSES,
            "payment_methods": core_cash.METHODS,
        }


@router.get("")
def list_view(request: Request, purpose: str | None = None, staff=Depends(require_staff)):
    return render(request, "buyback_list.html", staff=staff, **_list_context(purpose))


@router.post("")
async def create_view(request: Request, staff=Depends(require_role(*_BUYBACK_ROLES))):
    """Every field arrives as a plain form value (not typed Form(...)) so a
    submission missing something never hits FastAPI's raw 422 — it
    re-renders the same page with a plain-Russian error instead (same
    convention as webapp.routers.repairs.create_view)."""
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = core_clients.normalize_phone((form.get("client_phone") or "").strip())
    device_type = (form.get("device_type") or "").strip()
    brand = (form.get("brand") or "").strip()
    model = (form.get("model") or "").strip()
    serial_number = (form.get("serial_number") or "").strip()
    condition_note = (form.get("condition_note") or "").strip()
    purchase_price = optional_int(form.get("purchase_price") or "")
    payment_method = form.get("payment_method") or "cash"
    purpose = form.get("purpose") or "parts"
    resale_price = optional_int(form.get("resale_price") or "")

    def _error(message: str):
        return render(request, "buyback_list.html", staff=staff, error=message, **_list_context(None))

    if not client_name or not client_phone:
        return _error("Заполните имя и телефон клиента.")
    if not device_type:
        return _error("Укажите тип устройства.")
    if not model:
        return _error("Укажите модель устройства.")
    if not purchase_price or purchase_price <= 0:
        return _error("Укажите сумму, которую платим клиенту.")
    if payment_method not in core_cash.METHODS:
        return _error("Укажите способ оплаты.")
    if purpose not in core_buyback.PURPOSES:
        return _error("Укажите назначение — на запчасти или на продажу.")
    if purpose == "resale" and (not resale_price or resale_price <= 0):
        return _error("Укажите цену продажи — она нужна, чтобы товар сразу можно было продать.")

    try:
        photo = await _validate_intake_photo(form.get("photo"))
    except ValueError as exc:
        return _error(str(exc))
    if not photo:
        return _error("Загрузите фото устройства.")

    with get_conn() as conn:
        order_id = core_buyback.create_buyback_intake(
            conn,
            client_name=client_name, client_phone=client_phone,
            device_type=device_type, brand=brand or None, model=model,
            serial_number=serial_number or None, condition_note=condition_note or None,
            purchase_price=purchase_price, payment_method=payment_method,
            purpose=purpose, resale_price=resale_price,
            staff_id=staff["id"], photo=photo,
        )

    return RedirectResponse(link(request, f"/buyback/{order_id}"), status_code=303)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        order = core_buyback.get_buyback_order(conn, order_id)
    if not order:
        return RedirectResponse(link(request, "/buyback"), status_code=303)
    return render(request, "buyback_detail.html", staff=staff, order=order)
