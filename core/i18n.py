"""Minimal in-house i18n — a per-staff language preference (ru/uk) with a
flat key->string dictionary per language. Wired into every page via the
`t()` helper webapp.templating.render() injects into the template
context, bound to the current staff's saved language.

Scope (19.08): app shell (tabbar, page titles, home dashboard, "Ещё") +
"Ремонты" (intake form, repair list/cards, repair detail) translated so
far — deliberately incremental per Павел: infrastructure + most-visible
screens first, rest of the app filled in one section at a time rather
than as one huge pass. Everything else (Продажи, Склад, Клиенты, Отчёты
forms/tables, flash messages) still renders in Russian regardless of the
chosen language until it gets its own pass.

A key missing from a language's dict falls back to Russian, then to the
key itself — a half-translated language must never render blank text."""
from __future__ import annotations

LANGUAGES = {"ru": "Русский", "uk": "Українська"}
DEFAULT_LANGUAGE = "ru"

_STRINGS: dict[str, dict[str, str]] = {
    "ru": {
        "nav_home": "Главная",
        "nav_repairs": "Ремонты",
        "nav_sales": "Продажи",
        "nav_warehouse": "Склад",
        "nav_more": "Ещё",
        "app_subtitle": "Сервис-центр",

        "dash_repairs_in_progress": "ремонтов в работе",
        "dash_sales_total": "продаж всего",
        "dash_low_stock": "низкий остаток",
        "dash_clients_total": "клиентов в базе",
        "dash_products_total": "товарных позиций",

        "more_clients": "Клиенты",
        "more_clients_sub": "База клиентов магазина",
        "more_reports": "Отчёты",
        "more_reports_sub": "Ремонты, продажи, склад",
        "more_settings": "Настройки",
        "more_settings_sub": "Язык приложения",

        "settings_title": "Настройки",
        "settings_language": "Язык приложения",
        "settings_language_hint": "Пока переведены главная, навигация, «Ещё» и «Ремонты» — остальные разделы постепенно тоже переведём.",

        "status_new": "Новый",
        "status_in_progress": "В работе",
        "status_ready": "Готов к выдаче",
        "status_issued": "Выдан",
        "status_cancelled": "Отменён",
        "channel_online": "Онлайн",
        "channel_offline": "Офлайн",
        "channel_offline_full": "Офлайн (в точке)",
        "filter_all": "Все",

        "repairs_intake_title": "Принять устройство в ремонт",
        "client_name_label": "Имя клиента",
        "client_phone_label": "Телефон клиента",
        "channel_label": "Канал",
        "master_label": "Мастер",
        "master_unassigned": "Не назначен",
        "master_unassigned_meta": "Мастер не назначен",
        "device_type_label": "Тип устройства",
        "device_type_placeholder": "Смартфон, ноутбук…",
        "device_brand_label": "Бренд",
        "device_brand_placeholder": "Apple, Samsung…",
        "device_model_label": "Модель",
        "device_model_placeholder": "Начните вводить бренд — появятся модели",
        "device_serial_label": "Серийный №/IMEI",
        "defect_description_label": "Описание неисправности",
        "device_photo_label": "Фото устройства (необязательно)",
        "price_estimate_label": "Оценка стоимости",
        "add_device_btn": "+ Добавить ещё устройство",
        "intake_submit_btn": "Принять",
        "repairs_empty": "Ремонтов пока нет.",

        "repair_detail_title": "Ремонт",
        "device_photo_empty": "Фото устройства ещё не добавлено.",
        "photo_replace": "Заменить фото",
        "photo_add": "Добавить фото",
        "upload_btn": "Загрузить",
        "client_device_title": "Клиент и устройство",
        "serial_number_prefix": "Серийный №:",
        "defect_prefix": "Неисправность:",
        "channel_prefix": "Канал:",
        "accepted_at_prefix": "Принят",
        "status_title": "Статус",
        "new_status_label": "Новый статус",
        "payment_method_label": "Способ оплаты",
        "payment_method_cash": "Наличные",
        "payment_method_card": "Карта/перевод",
        "payment_method_hint": "Нужен только при отметке «Выдан», если указана итоговая цена — попадёт в кассу.",
        "comment_label": "Комментарий",
        "update_status_btn": "Обновить статус",
        "assign_btn": "Назначить",
        "price_warranty_title": "Цена и гарантия",
        "price_final_label": "Итоговая цена",
        "warranty_until_label": "Гарантия до",
        "save_btn": "Сохранить",
        "used_parts_title": "Использованные детали",
        "part_col": "Деталь",
        "qty_col": "Кол-во",
        "when_col": "Когда",
        "no_parts_used": "Детали не списывались.",
        "writeoff_btn": "Списать",
        "attachments_title": "Вложения",
        "attachment_alt": "Вложение",
        "history_title": "История",
        "system_label": "система",
    },
    "uk": {
        "nav_home": "Головна",
        "nav_repairs": "Ремонти",
        "nav_sales": "Продажі",
        "nav_warehouse": "Склад",
        "nav_more": "Ще",
        "app_subtitle": "Сервіс-центр",

        "dash_repairs_in_progress": "ремонтів у роботі",
        "dash_sales_total": "продажів усього",
        "dash_low_stock": "низький залишок",
        "dash_clients_total": "клієнтів у базі",
        "dash_products_total": "товарних позицій",

        "more_clients": "Клієнти",
        "more_clients_sub": "База клієнтів магазину",
        "more_reports": "Звіти",
        "more_reports_sub": "Ремонти, продажі, склад",
        "more_settings": "Налаштування",
        "more_settings_sub": "Мова застосунку",

        "settings_title": "Налаштування",
        "settings_language": "Мова застосунку",
        "settings_language_hint": "Поки перекладені головна, навігація, «Ще» та «Ремонти» — решту розділів перекладемо поступово.",

        "status_new": "Новий",
        "status_in_progress": "У роботі",
        "status_ready": "Готовий до видачі",
        "status_issued": "Видано",
        "status_cancelled": "Скасовано",
        "channel_online": "Онлайн",
        "channel_offline": "Офлайн",
        "channel_offline_full": "Офлайн (у точці)",
        "filter_all": "Усі",

        "repairs_intake_title": "Прийняти пристрій у ремонт",
        "client_name_label": "Ім'я клієнта",
        "client_phone_label": "Телефон клієнта",
        "channel_label": "Канал",
        "master_label": "Майстер",
        "master_unassigned": "Не призначено",
        "master_unassigned_meta": "Майстер не призначений",
        "device_type_label": "Тип пристрою",
        "device_type_placeholder": "Смартфон, ноутбук…",
        "device_brand_label": "Бренд",
        "device_brand_placeholder": "Apple, Samsung…",
        "device_model_label": "Модель",
        "device_model_placeholder": "Почніть вводити бренд — з'являться моделі",
        "device_serial_label": "Серійний №/IMEI",
        "defect_description_label": "Опис несправності",
        "device_photo_label": "Фото пристрою (необов'язково)",
        "price_estimate_label": "Оцінка вартості",
        "add_device_btn": "+ Додати ще пристрій",
        "intake_submit_btn": "Прийняти",
        "repairs_empty": "Ремонтів поки немає.",

        "repair_detail_title": "Ремонт",
        "device_photo_empty": "Фото пристрою ще не додано.",
        "photo_replace": "Замінити фото",
        "photo_add": "Додати фото",
        "upload_btn": "Завантажити",
        "client_device_title": "Клієнт і пристрій",
        "serial_number_prefix": "Серійний №:",
        "defect_prefix": "Несправність:",
        "channel_prefix": "Канал:",
        "accepted_at_prefix": "Прийнято",
        "status_title": "Статус",
        "new_status_label": "Новий статус",
        "payment_method_label": "Спосіб оплати",
        "payment_method_cash": "Готівка",
        "payment_method_card": "Карта/переказ",
        "payment_method_hint": "Потрібно лише при позначці «Видано», якщо вказана підсумкова ціна — потрапить у касу.",
        "comment_label": "Коментар",
        "update_status_btn": "Оновити статус",
        "assign_btn": "Призначити",
        "price_warranty_title": "Ціна та гарантія",
        "price_final_label": "Підсумкова ціна",
        "warranty_until_label": "Гарантія до",
        "save_btn": "Зберегти",
        "used_parts_title": "Використані деталі",
        "part_col": "Деталь",
        "qty_col": "К-сть",
        "when_col": "Коли",
        "no_parts_used": "Деталі не списувались.",
        "writeoff_btn": "Списати",
        "attachments_title": "Вкладення",
        "attachment_alt": "Вкладення",
        "history_title": "Історія",
        "system_label": "система",
    },
}


def t(key: str, lang: str) -> str:
    lang_dict = _STRINGS.get(lang) or _STRINGS[DEFAULT_LANGUAGE]
    return lang_dict.get(key) or _STRINGS[DEFAULT_LANGUAGE].get(key) or key
