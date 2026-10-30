"""Фото карточки товара на диске — одно на товар, `products.photo_path`.

Общее для Mini App (загрузка на карточке товара) и бота («📦 Приход →
➕ Добавить товар», 30.10). Имя файла всегда своё (id товара + uuid),
никогда не клиентское — от path traversal; старое фото удаляется при замене.
"""
from __future__ import annotations

import os
import sqlite3
import uuid

from core import inventory as _inventory
from core import photos as _photos

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "webapp", "static", "product_photos")


def save(conn: sqlite3.Connection, product_id: int, data: bytes, ext: str = ".jpg") -> str:
    """Write the photo and make it the product's. Returns the file name."""
    compressed = _photos.compress_photo(data)
    if compressed is not None:
        data, ext = compressed, ".jpg"
    os.makedirs(PHOTO_DIR, exist_ok=True)
    filename = f"{product_id}_{uuid.uuid4().hex}{ext}"
    with open(os.path.join(PHOTO_DIR, filename), "wb") as f:
        f.write(data)
    old = _inventory.get_product(conn, product_id)
    _inventory.set_product_photo(conn, product_id, filename)
    if old and old["photo_path"] and old["photo_path"] != filename:
        old_path = os.path.join(PHOTO_DIR, old["photo_path"])
        if os.path.exists(old_path):
            os.remove(old_path)
    return filename
