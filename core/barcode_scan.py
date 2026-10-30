"""Fast local barcode decoding via zbar (pyzbar) — tried first, before
ever touching the network/LLM path in core.vision_ocr. Reading an actual
barcode symbol off a photo is a solved, near-instant problem (tens of
milliseconds); it never needs an OpenAI round trip, which is what made
scanning feel slow. vision_ocr stays as the fallback for anything zbar
can't decode — a blurry shot, an unsupported symbology, or a label with
no barcode symbol at all (just printed text), which is a real case for
generic Chinese-sourced parts.
"""
from __future__ import annotations

import io
import time

from PIL import Image, ImageFilter, ImageOps
from pyzbar.pyzbar import ZBarSymbol
from pyzbar.pyzbar import decode as _zbar_decode

# A photo sent through Telegram as a photo is squeezed to ~1280 px — a
# barcode that is a small part of the frame ends up with bars ~1 px wide,
# below what zbar resolves. Blowing it up is what brings it back.
_UPSCALE_BELOW = 1600
# Every extra pass over a 2× frame costs up to ~2 s on a busy/noisy shot
# with no barcode at all — past this the photo is given up on (the caller
# falls back to reading the digits / asking the person).
_BUDGET_SECONDS = 4.0
# The extra passes look for linear barcodes only — the QR search is the
# expensive part of a zbar pass, and QR codes read fine on the plain one.
_LINEAR = [ZBarSymbol.EAN13, ZBarSymbol.EAN8, ZBarSymbol.UPCA, ZBarSymbol.UPCE,
           ZBarSymbol.CODE128, ZBarSymbol.CODE39, ZBarSymbol.CODE93, ZBarSymbol.I25, ZBarSymbol.CODABAR]


def _variants(gray: Image.Image):
    """(image, symbols) — cheapest and likeliest first."""
    yield gray, None
    contrast = ImageOps.autocontrast(gray)
    yield contrast, None
    yield contrast.rotate(90, expand=True), _LINEAR
    if max(gray.size) >= _UPSCALE_BELOW:
        return
    big = ImageOps.autocontrast(gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS).filter(ImageFilter.SHARPEN))
    yield big, _LINEAR
    yield big.point(lambda p: 255 if p > 140 else 0), _LINEAR
    yield big.rotate(90, expand=True), _LINEAR
    # A barcode in one part of a busy shot: the halves on their own.
    w, h = big.size
    for box in ((0, 0, w, h // 2), (0, h // 2, w, h), (0, 0, w // 2, h), (w // 2, 0, w, h)):
        yield big.crop(box), _LINEAR


def decode_barcode(photo_bytes: bytes) -> str | None:
    """First barcode's decoded text, or None if zbar found nothing
    readable in the variants (contrast, rotation, 2× upscale, threshold,
    halves of the frame) within the time budget."""
    try:
        im = Image.open(io.BytesIO(photo_bytes))
        im = ImageOps.exif_transpose(im)
        gray = im.convert("L")
    except Exception:
        return None

    started = time.monotonic()
    for candidate, symbols in _variants(gray):
        if time.monotonic() - started > _BUDGET_SECONDS:
            break
        try:
            results = _zbar_decode(candidate, symbols=symbols)
        except Exception:
            continue
        for result in results:
            text = result.data.decode("utf-8", errors="ignore").strip()
            if text:
                return text
    return None


def ean_checksum_ok(digits: str) -> bool:
    """EAN-8 / UPC-A (12) / EAN-13 / GTIN-14 check digit is right — a
    string of digits read off a photo by a model, not by zbar, is trusted
    without asking only when this holds."""
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check
