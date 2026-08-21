"""Shared photo-compression helper for every place this app writes an
uploaded/received image to disk — product photos, repair device photos,
repair attachments posted in the staff groups. Product/repair photos are
only ever viewed at thumbnail/card size in the CRM, but a phone camera
photo comes in at several MB and 3000+px; storing it at full resolution
does nothing for legibility there and just makes every page that renders
a grid of these photos slower to load as the catalog grows into the
thousands.
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

_MAX_DIMENSION = 1600
_JPEG_QUALITY = 85


def compress_photo(data: bytes) -> bytes | None:
    """Re-encode image bytes as a size-capped JPEG, auto-rotated by EXIF
    orientation (phone cameras tag rotation rather than physically
    rotating pixels — this is the only chance to get it right, since a
    plain re-saved JPEG loses that tag). Returns None if `data` can't be
    decoded as an image at all — callers should fall back to storing the
    original bytes/extension unchanged in that case, not fail the whole
    upload over a compression nicety."""
    try:
        im = Image.open(io.BytesIO(data))
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
    except Exception:
        return None

    im.thumbnail((_MAX_DIMENSION, _MAX_DIMENSION), Image.LANCZOS)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buf.getvalue()
