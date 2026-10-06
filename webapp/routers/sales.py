from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse

from core import accounts as core_accounts
from core import cash as core_cash
from core import channel_posts
from core import clients as core_clients
from core import documents as core_documents
from core import inventory as core_inventory
from core import sales as core_sales
from core.inventory import InsufficientStockError
from core.storage import get_conn
from webapp.deps import idem_key, link, loc, require_staff
from webapp.payments import payments_from_form
from webapp.templating import render

router = APIRouter(prefix="/sales")

INITIAL_ROWS = 1


def _list_context(conn, location_id: int | None = None) -> dict:
    """The sale-list/new-sale page context — item_rows is just how many
    empty rows render on first load; "+ Добавить позицию" (sale-rows.js)
    grows the checkout past that with no fixed cap, unlike the old
    hardcoded 3-row form."""
    return {
        "sales": core_sales.list_sales(conn, location_id=location_id),
        "item_rows": range(INITIAL_ROWS),
        "accounts": core_accounts.list_accounts(conn, location_id) if location_id is not None else [],
        **_picker_context(conn, location_id),
    }


def _picker_context(conn, location_id: int | None = None) -> dict:
    """Product-search data for sale-rows.js: same {id, label} picker shape
    purchase-rows.js uses (label includes SKU when there is one, so
    typing/matching by SKU works same as by name), plus each product's
    price so picking/scanning a product can prefill it — the cashier can
    still override, e.g. a discount."""
    products = core_inventory.list_products(conn)
    return {
        "products_for_picker": [
            {"id": p["id"], "label": f"{p['name']} ({p['sku']})" if p["sku"] else p["name"]}
            for p in products
        ],
        "default_price_by_product": {p["id"]: p["price"] for p in products if p["price"] is not None},
        # Serial units on hand at this точка, by product — the IMEI field
        # of a sale row suggests these, and a scanned IMEI resolves here.
        "serial_units": _serial_units(conn, location_id),
    }


def _serial_units(conn, location_id: int | None) -> dict:
    units: dict[int, list[str]] = {}
    if location_id is None:
        return units
    for line in core_inventory.stock_lines(conn, location_id=location_id):
        if line["is_serial"] and line["imei"]:
            units.setdefault(line["product_id"], []).append(line["imei"])
    return units


@router.get("")
def list_view(request: Request, staff=Depends(require_staff)):
    with get_conn() as conn:
        ctx = _list_context(conn, loc(request))
    return render(request, "sales_list.html", staff=staff, **ctx)


@router.post("")
async def create_view(request: Request, staff=Depends(require_staff)):
    form = await request.form()
    client_name = (form.get("client_name") or "").strip()
    client_phone = core_clients.normalize_phone((form.get("client_phone") or "").strip())
    channel = form.get("channel", "offline")
    warranty_until = (form.get("warranty_until") or "").strip() or None
    payment_method = form.get("payment_method") or "cash"
    if payment_method not in core_cash.METHODS:
        payment_method = "cash"
    row_count = int(form.get("row_count") or INITIAL_ROWS)

    items = []
    unresolved = []
    for i in range(row_count):
        product_id = form.get(f"product_id_{i}")
        product_name = (form.get(f"product_name_{i}") or "").strip()
        qty = form.get(f"qty_{i}")
        price = form.get(f"price_{i}")

        if not product_id and not product_name and not qty and not price:
            continue  # untouched row — sparse checkout is fine, same as before
        if not product_id:
            unresolved.append(product_name or f"строка {i + 1}")
            continue
        if not qty or not price:
            continue  # started filling but incomplete — skip rather than 500
        items.append((int(product_id), int(qty), int(price), (form.get(f"imei_{i}") or "").strip()))

    error = None
    if unresolved:
        error = "Не найден в каталоге: " + ", ".join(unresolved) + " — выберите товар из списка или уберите строку."
    elif not items:
        error = "Добавьте хотя бы один товар в чек."

    if error:
        with get_conn() as conn:
            ctx = _list_context(conn, loc(request))
        return render(request, "sales_list.html", staff=staff, error=error, **ctx)

    key = idem_key("sale", form.get("idem"))
    try:
        with get_conn() as conn:
            already = core_documents.find_by_key(conn, key)
            if already:
                return RedirectResponse(link(request, f"/sales/{already['ref_id']}"), status_code=303)
            # A typed/scanned IMEI names the exact unit (its партия).
            resolved_items = []
            for product_id, qty, price, imei in items:
                batch_id = None
                if imei:
                    unit = core_inventory.find_unit_by_imei(conn, imei)
                    if not unit or unit["product_id"] != product_id:
                        raise core_sales.SaleError(f"IMEI {imei} не найден на остатке этого товара.")
                    batch_id = unit["id"]
                resolved_items.append((product_id, qty, price, batch_id))
            items = resolved_items
            client_id = core_clients.get_or_create_by_phone(conn, client_name, client_phone, source=channel) if client_phone else None
            order_id = core_sales.create_sale(
                conn, client_id, channel, staff["id"], items, warranty_until, payment_method,
                location_id=loc(request), key=key, payments=payments_from_form(form) or None,
            )
    except (InsufficientStockError, core_cash.PaymentError, core_sales.SaleError) as exc:
        # Raised out of the `with` on purpose: get_conn() commits on a
        # normal exit, and a sale that failed on its second line must not
        # leave its order row and its first line's stock write-off behind.
        with get_conn() as conn:
            ctx = _list_context(conn, loc(request))
        return render(request, "sales_list.html", staff=staff, error=str(exc), **ctx)
    # Sold out -> the bot takes the product's card off the sales channel
    # (core.channel_posts). After the sale's own commit, and threadpooled:
    # it's blocking httpx to Telegram, this is an async route.
    store = request.state.store
    await run_in_threadpool(
        channel_posts.sync_products, [item[0] for item in items], store.id, store.db_path
    )
    return RedirectResponse(link(request, f"/sales/{order_id}"), status_code=303)


@router.get("/{order_id}")
def detail_view(request: Request, order_id: int, staff=Depends(require_staff)):
    with get_conn() as conn:
        sale = core_sales.get_sale(conn, order_id)
        if not sale:
            return RedirectResponse(link(request, "/sales"), status_code=303)
        items = core_sales.get_sale_items(conn, order_id)
        document = core_documents.get_for(conn, "sale", order_id)
        payments = core_cash.payments_for(conn, "sales_order", order_id)
    return render(
        request, "sale_detail.html", staff=staff, sale=sale, items=items, document=document, payments=payments,
    )
