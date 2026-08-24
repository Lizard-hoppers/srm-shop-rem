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

from PIL import Image, ImageOps
from pyzbar.pyzbar import decode as _zbar_decode


def decode_barcode(photo_bytes: bytes) -> str | None:
    """First barcode's decoded text, or None if zbar found nothing
    readable. A few cheap variants are tried in order (each decode is
    milliseconds, so trying 3 costs nothing next to a single OpenAI call)
    to cover the photos that fail on a plain first attempt: low-contrast
    phone-camera lighting, or the barcode label rotated 90°."""
    try:
        im = Image.open(io.BytesIO(photo_bytes))
        im = ImageOps.exif_transpose(im)
        gray = im.convert("L")
    except Exception:
        return None

    for candidate in (gray, ImageOps.autocontrast(gray), gray.rotate(90, expand=True)):
        try:
            results = _zbar_decode(candidate)
        except Exception:
            continue
        if results:
            text = results[0].data.decode("utf-8", errors="ignore").strip()
            if text:
                return text
    return None
