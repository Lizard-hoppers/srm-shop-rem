"""Device type/brand/model catalog for autocomplete on the repair intake
form (like the model pickers on e-katalog-style electronics sites). Seeded
with common devices up front; every new combo a master actually types gets
remembered too, so the catalog grows from real intake instead of staying
frozen at the seed list.
"""
from __future__ import annotations

import sqlite3

SEED: list[tuple[str, str, str]] = [
    # Смартфоны
    ("Смартфон", "Apple", "iPhone SE"),
    ("Смартфон", "Apple", "iPhone 11"),
    ("Смартфон", "Apple", "iPhone 12"),
    ("Смартфон", "Apple", "iPhone 12 Pro"),
    ("Смартфон", "Apple", "iPhone 13"),
    ("Смартфон", "Apple", "iPhone 13 Pro"),
    ("Смартфон", "Apple", "iPhone 14"),
    ("Смартфон", "Apple", "iPhone 14 Pro"),
    ("Смартфон", "Apple", "iPhone 15"),
    ("Смартфон", "Apple", "iPhone 15 Pro"),
    ("Смартфон", "Apple", "iPhone 16"),
    ("Смартфон", "Apple", "iPhone 16 Pro"),
    ("Смартфон", "Samsung", "Galaxy A14"),
    ("Смартфон", "Samsung", "Galaxy A34"),
    ("Смартфон", "Samsung", "Galaxy A54"),
    ("Смартфон", "Samsung", "Galaxy A55"),
    ("Смартфон", "Samsung", "Galaxy S21"),
    ("Смартфон", "Samsung", "Galaxy S22"),
    ("Смартфон", "Samsung", "Galaxy S23"),
    ("Смартфон", "Samsung", "Galaxy S24"),
    ("Смартфон", "Samsung", "Galaxy Note 20"),
    ("Смартфон", "Samsung", "Galaxy Z Flip"),
    ("Смартфон", "Samsung", "Galaxy Z Fold"),
    ("Смартфон", "Xiaomi", "Redmi 10"),
    ("Смартфон", "Xiaomi", "Redmi 12"),
    ("Смартфон", "Xiaomi", "Redmi Note 11"),
    ("Смартфон", "Xiaomi", "Redmi Note 12"),
    ("Смартфон", "Xiaomi", "Redmi Note 13"),
    ("Смартфон", "Xiaomi", "Mi 11"),
    ("Смартфон", "Xiaomi", "Mi 12"),
    ("Смартфон", "Xiaomi", "Poco X5"),
    ("Смартфон", "Xiaomi", "Poco M5"),
    ("Смартфон", "Huawei", "P30"),
    ("Смартфон", "Huawei", "P40"),
    ("Смартфон", "Huawei", "Mate 40"),
    ("Смартфон", "Huawei", "Nova 9"),
    ("Смартфон", "Honor", "Honor 90"),
    ("Смартфон", "Honor", "Honor X8"),
    ("Смартфон", "OnePlus", "OnePlus 9"),
    ("Смартфон", "OnePlus", "OnePlus 11"),
    ("Смартфон", "Google", "Pixel 6"),
    ("Смартфон", "Google", "Pixel 7"),
    ("Смартфон", "Google", "Pixel 8"),
    ("Смартфон", "Realme", "Realme 9"),
    ("Смартфон", "Realme", "Realme C55"),
    ("Смартфон", "Nokia", "Nokia G21"),
    ("Смартфон", "Motorola", "Moto G73"),

    # Ноутбуки
    ("Ноутбук", "Apple", "MacBook Air M1"),
    ("Ноутбук", "Apple", "MacBook Air M2"),
    ("Ноутбук", "Apple", "MacBook Pro 13"),
    ("Ноутбук", "Apple", "MacBook Pro 14"),
    ("Ноутбук", "Apple", "MacBook Pro 16"),
    ("Ноутбук", "Lenovo", "IdeaPad 3"),
    ("Ноутбук", "Lenovo", "IdeaPad 5"),
    ("Ноутбук", "Lenovo", "ThinkPad E14"),
    ("Ноутбук", "Lenovo", "ThinkPad T14"),
    ("Ноутбук", "Lenovo", "Legion 5"),
    ("Ноутбук", "HP", "Pavilion 15"),
    ("Ноутбук", "HP", "EliteBook 840"),
    ("Ноутбук", "HP", "Omen 16"),
    ("Ноутбук", "Dell", "Inspiron 15"),
    ("Ноутбук", "Dell", "XPS 13"),
    ("Ноутбук", "Dell", "Latitude 5420"),
    ("Ноутбук", "Asus", "VivoBook 15"),
    ("Ноутбук", "Asus", "ZenBook 14"),
    ("Ноутбук", "Asus", "ROG Strix"),
    ("Ноутбук", "Acer", "Aspire 5"),
    ("Ноутбук", "Acer", "Nitro 5"),
    ("Ноутбук", "MSI", "Modern 14"),
    ("Ноутбук", "MSI", "Katana GF66"),

    # Планшеты
    ("Планшет", "Apple", "iPad 9"),
    ("Планшет", "Apple", "iPad 10"),
    ("Планшет", "Apple", "iPad Air"),
    ("Планшет", "Apple", "iPad Pro 11"),
    ("Планшет", "Apple", "iPad Pro 12.9"),
    ("Планшет", "Apple", "iPad mini"),
    ("Планшет", "Samsung", "Galaxy Tab A8"),
    ("Планшет", "Samsung", "Galaxy Tab S8"),
    ("Планшет", "Samsung", "Galaxy Tab S9"),
    ("Планшет", "Lenovo", "Tab M10"),
    ("Планшет", "Huawei", "MatePad 11"),
    ("Планшет", "Xiaomi", "Pad 5"),

    # Умные часы
    ("Умные часы", "Apple", "Apple Watch SE"),
    ("Умные часы", "Apple", "Apple Watch Series 8"),
    ("Умные часы", "Apple", "Apple Watch Series 9"),
    ("Умные часы", "Apple", "Apple Watch Ultra"),
    ("Умные часы", "Samsung", "Galaxy Watch 5"),
    ("Умные часы", "Samsung", "Galaxy Watch 6"),
    ("Умные часы", "Xiaomi", "Mi Band 7"),
    ("Умные часы", "Xiaomi", "Mi Band 8"),
    ("Умные часы", "Huawei", "Watch GT 3"),
    ("Умные часы", "Amazfit", "GTS 4"),

    # Наушники
    ("Наушники", "Apple", "AirPods 2"),
    ("Наушники", "Apple", "AirPods 3"),
    ("Наушники", "Apple", "AirPods Pro"),
    ("Наушники", "Apple", "AirPods Max"),
    ("Наушники", "Samsung", "Galaxy Buds2"),
    ("Наушники", "Samsung", "Galaxy Buds Pro"),
    ("Наушники", "Xiaomi", "Redmi Buds 4"),
    ("Наушники", "JBL", "Tune 230NC"),
    ("Наушники", "Sony", "WF-1000XM4"),

    # Игровые консоли
    ("Игровая консоль", "Sony", "PlayStation 4"),
    ("Игровая консоль", "Sony", "PlayStation 5"),
    ("Игровая консоль", "Microsoft", "Xbox Series S"),
    ("Игровая консоль", "Microsoft", "Xbox Series X"),
    ("Игровая консоль", "Nintendo", "Switch"),
    ("Игровая консоль", "Nintendo", "Switch Lite"),

    # Телевизоры
    ("Телевизор", "Samsung", "Smart TV"),
    ("Телевизор", "LG", "OLED TV"),
    ("Телевизор", "Xiaomi", "Mi TV"),
    ("Телевизор", "Sony", "Bravia"),

    # ПК / моноблоки
    ("ПК/моноблок", "Apple", "iMac"),
    ("ПК/моноблок", "Apple", "Mac mini"),
    ("ПК/моноблок", "HP", "Pavilion All-in-One"),
    ("ПК/моноблок", "Lenovo", "IdeaCentre"),
]


def list_device_types(conn: sqlite3.Connection) -> list[str]:
    return [r["device_type"] for r in conn.execute(
        "SELECT DISTINCT device_type FROM device_catalog ORDER BY device_type"
    ).fetchall()]


def list_brands(conn: sqlite3.Connection) -> list[str]:
    return [r["brand"] for r in conn.execute(
        "SELECT DISTINCT brand FROM device_catalog ORDER BY brand"
    ).fetchall()]


def list_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every (device_type, brand, model) row — small enough to embed whole
    in the page for client-side cascading (brand -> model) autocomplete."""
    return conn.execute(
        "SELECT device_type, brand, model FROM device_catalog ORDER BY device_type, brand, model"
    ).fetchall()


def remember(conn: sqlite3.Connection, device_type: str, brand: str | None, model: str | None) -> None:
    """Add a newly-typed device combo to the catalog so it's suggested next time."""
    device_type = device_type.strip()
    brand = (brand or "").strip()
    model = (model or "").strip()
    if not device_type or not brand or not model:
        return
    conn.execute(
        "INSERT OR IGNORE INTO device_catalog (device_type, brand, model) VALUES (?, ?, ?)",
        (device_type, brand, model),
    )


def seed(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO device_catalog (device_type, brand, model) VALUES (?, ?, ?)", SEED
    )
