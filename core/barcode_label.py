"""Printable barcode labels for products.

The barcode encodes the product's own SKU verbatim — never an
app-invented id. Павел's point: a part usually already carries a real
manufacturer barcode (e.g. SKU "2716140063024"), and the CRM's label
should scan back to the same digits, not a different code — so scanning
either the original packaging or a label printed from here resolves to
the same product (core.inventory.get_product_by_sku).

Code128 (not EAN-13) — it encodes arbitrary ASCII, so it works whether
the SKU happens to be a 13-digit EAN already on the box or a short
alphanumeric code someone typed by hand; EAN-13 would reject anything
that isn't exactly 13 valid digits.

The label is generated fresh from the database on every request (see
webapp.routers.inventory.product_barcode_view) — there is no cached/
pre-rendered image to go stale, so editing the name or price and hitting
Сохранить is the only "reissue" step that's needed; the very next time
anyone views or prints the label it already reflects the new values.
"""
from __future__ import annotations

import io

import barcode as barcode_lib
from barcode.writer import ImageWriter
from PIL import Image, ImageDraw, ImageFont

_FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _load_font(path: str, size: int) -> ImageFont.ImageFont:
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _wrap_text(text: str, font: ImageFont.ImageFont, max_width: int, draw: ImageDraw.ImageDraw) -> list[str]:
    """Greedy word-wrap so a long product name doesn't run off the label."""
    words = text.split()
    if not words:
        return [""]
    lines = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if draw.textlength(candidate, font=font) <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def generate_label_png(sku: str, name: str, price: int | None, *, compact: bool = False) -> bytes:
    """A printable label: a Code128 barcode of `sku` (with the digits/
    text printed under the bars) and the current sale price, plus the
    product name above it — unless `compact`, which drops the name.

    compact=True is for physical thermal-label printing (Xprinter
    XP-420B, 30x20mm labels — see webapp.routers.inventory's
    ?compact=1 barcode.png param and the print view on the product
    card): at that size a wrapped name just crowds out the barcode
    without being legible anyway, so the label only carries what a
    30x20mm sticker can actually show clearly. The full version stays
    the on-screen default."""
    code128 = barcode_lib.get_barcode_class("code128")
    barcode_obj = code128(sku, writer=ImageWriter())
    barcode_buf = io.BytesIO()
    barcode_obj.write(barcode_buf, options={
        "module_height": 12.0, "quiet_zone": 2.0,
        "font_size": 9, "text_distance": 4.0, "write_text": True,
    })
    barcode_buf.seek(0)
    barcode_img = Image.open(barcode_buf).convert("RGB")

    name_font = _load_font(_FONT_BOLD, 16)
    price_font = _load_font(_FONT_BOLD, 20)

    padding = 12
    canvas_width = barcode_img.width + 2 * padding

    if compact:
        name_lines: list[str] = []
        name_line_height = 0
    else:
        dummy = Image.new("RGB", (1, 1))
        dummy_draw = ImageDraw.Draw(dummy)
        name_lines = _wrap_text(name, name_font, canvas_width - 2 * padding, dummy_draw)
        name_line_height = name_font.size + 4
    name_block_height = name_line_height * len(name_lines)

    price_text = f"{price} грн" if price is not None else "Цена не указана"
    price_block_height = price_font.size + 12

    canvas_height = padding + name_block_height + barcode_img.height + price_block_height + padding
    canvas = Image.new("RGB", (canvas_width, canvas_height), "white")
    draw = ImageDraw.Draw(canvas)

    y = padding
    for line in name_lines:
        line_width = draw.textlength(line, font=name_font)
        draw.text(((canvas_width - line_width) / 2, y), line, font=name_font, fill="black")
        y += name_line_height

    canvas.paste(barcode_img, (padding, y))
    y += barcode_img.height + 4

    price_width = draw.textlength(price_text, font=price_font)
    draw.text(((canvas_width - price_width) / 2, y), price_text, font=price_font, fill="black")

    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()
