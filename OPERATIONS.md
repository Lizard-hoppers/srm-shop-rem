# OPERATIONS — Electronics CRM

Сверяться с этим файлом в начале и в конце каждой рабочей сессии (правило
Clean & Live Bot Manifest, п.10).

## Что это

CRM для магазина электроники + мастерской по ремонту: клиенты, склад по
ячейкам, ремонты с привязкой к мастеру, приход комплектующих, продажи
(онлайн/офлайн). Веб-панель (FastAPI) для персонала + Telegram-бот для
клиентов, общая SQLite-база (WAL).

## Статус по фазам (план: `.claude/plans/fancy-greeting-pearl.md` у Павла)

- [x] Фаза 0 — каркас: схема БД (`core/storage.py`), staff-логин, скелет
      веб-панели и бота, `simulate_tests.py`.
- [x] Фаза 1 — Клиенты и склад: CRUD клиентов, товары, ячейки, движения
      склада (приход/списание/перемещение), отчёт по низким остаткам.
- [ ] Фаза 2 — Ремонты: приём устройства, назначение мастера, статус-пайплайн,
      списание деталей на заказ, гарантия.
- [ ] Фаза 3 — Приход комплектующих: поставщики, накладные.
- [ ] Фаза 4 — Продажи офлайн (POS-лайт).
- [ ] Фаза 5 — Telegram-бот для клиентов: статус ремонта, каталог, онлайн-заказ.
- [ ] Фаза 6 — Отчётность.
- [ ] Фаза 7 — Прод-деплой на BlueVPS (nginx+certbot, systemd).

## Локальный запуск (разработка)

```
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # заполнить CRM_SECRET_KEY и, для бота, CRM_BOT_TOKEN
export $(cat .env | xargs)
python -m core.bootstrap owner <пароль> "Имя владельца"   # первый аккаунт
uvicorn webapp.main:app --reload --port 8000               # веб-панель
python -m bot.bot                                            # бот (polling)
```

## Тесты

`python simulate_tests.py` — сценарные тесты ядра (клиент→устройство→ремонт,
склад/движения, права/пароли). Работают на временной SQLite-базе во
временной директории, живую базу не трогают. Обязательны перед каждым
деплоем.

## Деплой (Server-First, манифест п.8)

Продовая директория на BlueVPS: `/opt/electronics_crm`.
backup (снимок sqlite) → candidate-директория → `simulate_tests.py` →
rsync в live → systemctl restart `crm_web` и `crm_bot` → проверка
`journalctl -u crm_web -u crm_bot -f` → удалить candidate.

systemd-юниты: `crm_web.service` (uvicorn на 127.0.0.1:<порт>, за nginx+certbot
по паттерну `taki-verify`), `crm_bot.service` (polling).

## Нельзя (манифест п.11 + специфика проекта)

- Ручной `ALTER TABLE` — только через `_ensure_column()` в `core/storage.py`.
- Хранить `CRM_SECRET_KEY` / `CRM_BOT_TOKEN` в репозитории — только в `.env`
  на сервере (в `.gitignore`).
- Списывать/перемещать товар в обход `core/inventory.record_movement()` —
  это единственный путь, который держит `stock` и `stock_movements` в
  согласованном состоянии.
