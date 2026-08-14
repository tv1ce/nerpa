# Интеграция NERPA ↔ 1С:УНФ через OData

## Контекст

1С:УНФ (Управление Нашей Фирмой) — серверная установка.
OData API доступен по адресу: `http://<сервер>/hnf/odata/standard.odata/`

Проверка доступности: открыть `http://<сервер>/hnf/odata/standard.odata/$metadata` — должен вернуть XML.

---

## Направления синхронизации

```
1С → NERPA      Номенклатура (товары + цены)
              Оплаты счетов → обновление Invoice.status = 'paid'

NERPA → 1С      Контрагенты (новые и изменённые)
              Заказы покупателей
              Счета на оплату
              Движения склада: только тип 'in' (поставка) и 'adjustment' (корректировка)
              * движения 'out' с order_id — не пушить, создаются в 1С через заказ автоматически
```

---

## Шаг 1 — Миграция БД

Добавить в `app/database.py` в функцию `_migrate_db()` следующие миграции:

```python
("counterparties",   "external_id_1c", "TEXT"),          # GUID контрагента в 1С
("products",         "external_id_1c", "TEXT"),          # GUID номенклатуры в 1С
("orders",           "external_id_1c", "TEXT"),          # GUID заказа в 1С
("invoices",         "external_id_1c", "TEXT"),          # GUID счёта в 1С
("stock_movements",  "external_id_1c", "TEXT"),          # GUID документа в 1С
("counterparties",   "synced_to_1c_at", "TEXT"),        # datetime последней синхронизации
("products",         "synced_from_1c_at", "TEXT"),
("orders",           "synced_to_1c_at", "TEXT"),
("invoices",         "synced_to_1c_at", "TEXT"),
("stock_movements",  "synced_to_1c_at", "TEXT"),
```

Добавить поля в модели SQLAlchemy (`app/models.py`):

```python
# Counterparty
external_id_1c   = Column(String(36))
synced_to_1c_at  = Column(DateTime)

# Product
external_id_1c     = Column(String(36))
synced_from_1c_at  = Column(DateTime)

# Order
external_id_1c   = Column(String(36))
synced_to_1c_at  = Column(DateTime)

# Invoice
external_id_1c   = Column(String(36))
synced_to_1c_at  = Column(DateTime)

# StockMovement
external_id_1c   = Column(String(36))
synced_to_1c_at  = Column(DateTime)
```

---

## Шаг 2 — Настройки синхронизации

Добавить в `CompanySettings` (`app/models.py`):

```python
onec_url      = Column(String(500))   # http://<сервер>/hnf/odata/standard.odata
onec_user     = Column(String(100))   # логин пользователя 1С
onec_password = Column(String(200))   # пароль (хранить в открытом виде или шифровать)
onec_enabled  = Column(Boolean, default=False)
```

Добавить поля в миграцию `_migrate_db()`:

```python
("company_settings", "onec_url",      "TEXT"),
("company_settings", "onec_user",     "TEXT"),
("company_settings", "onec_password", "TEXT"),
("company_settings", "onec_enabled",  "INTEGER DEFAULT 0"),
```

---

## Шаг 3 — OData клиент

Создать файл `app/services/onec_client.py`:

```python
"""
Клиент к OData API 1С:УНФ.

Базовый URL: {settings.onec_url}
Аутентификация: HTTP Basic (onec_user / onec_password)
Формат: JSON (заголовок Accept: application/json)
"""
import httpx
from datetime import datetime
from sqlalchemy.orm import Session
from app.models import CompanySettings

TIMEOUT = 15  # секунд

def _get_settings(db: Session) -> CompanySettings | None:
    s = db.query(CompanySettings).first()
    if not s or not s.onec_enabled or not s.onec_url:
        return None
    return s

def _client(s: CompanySettings) -> httpx.Client:
    return httpx.Client(
        base_url=s.onec_url.rstrip('/') + '/',
        auth=(s.onec_user, s.onec_password),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=TIMEOUT,
    )

# ── Номенклатура: 1С → NERPA ────────────────────────────────────────────────────

def sync_products_from_1c(db: Session) -> dict:
    """
    Читает Catalog_Номенклатура из 1С, обновляет Products в NERPA.
    Маппинг: Ref_Key → external_id_1c, Code → article, Description → name,
             ЦенаПродажи → price (берётся из регистра ЦеныНоменклатуры).
    Возвращает {"created": N, "updated": N, "errors": [...]}
    """
    pass  # TODO: реализовать

# ── Контрагенты: NERPA → 1С ────────────────────────────────────────────────────

def push_counterparty(cp, db: Session) -> str | None:
    """
    Создаёт или обновляет контрагента в 1С.
    При создании: POST Catalog_Контрагенты
    При обновлении: PATCH Catalog_Контрагенты(guid'...')
    Поиск дубля перед созданием: GET Catalog_Контрагенты?$filter=ИНН eq '{cp.inn}'
    Банковские реквизиты: отдельный POST в Catalog_БанковскиеСчетаКонтрагентов
    Возвращает Ref_Key (GUID) из 1С или None при ошибке.

    Маппинг полей:
      cp.name            → Description
      cp.inn             → ИНН
      cp.kpp             → КПП
      cp.ogrn            → ОГРН
      cp.legal_address   → ЮридическийАдрес
      cp.actual_address  → АдресДляПисем
      cp.phone           → Телефон
      cp.email           → АдресЭлектроннойПочты
      cp.entity_type     → ЮридическоеФизическоеЛицо ('ЮрЛицо'/'ФизЛицо'/'ИндивидуальныйПредприниматель')
    """
    pass  # TODO: реализовать

# ── Заказы: NERPA → 1С ────────────────────────────────────────────────────────

def push_order(order, db: Session) -> str | None:
    """
    Создаёт Document_ЗаказПокупателя в 1С при подтверждении заказа (status='confirmed').
    Обновляет при изменении (если external_id_1c уже есть).
    НЕ вызывать повторно для заказов со статусом 'shipped'/'delivered' — данные уже в 1С.

    Маппинг:
      order.number           → Номер
      order.date             → Дата
      order.counterparty.external_id_1c → Контрагент (Ref_Key)
      order.items[]          → ТоварыУслуги (TabularSection)
        item.product.external_id_1c → Номенклатура
        item.quantity        → Количество
        item.price           → Цена
        item.discount_pct    → ПроцентСкидки
        item.vat_rate        → СтавкаНДС ('20%' / 'Без НДС')
    """
    pass  # TODO: реализовать

# ── Счета: NERPA → 1С ──────────────────────────────────────────────────────────

def push_invoice(invoice, db: Session) -> str | None:
    """
    Создаёт Document_СчётНаОплатуПокупателю в 1С.
    Вызывать при переводе счёта в статус 'issued'.

    Маппинг аналогичен заказу + due_date → ДатаОплаты
    """
    pass  # TODO: реализовать

# ── Оплаты: 1С → NERPA ────────────────────────────────────────────────────────

def sync_payments_from_1c(db: Session) -> dict:
    """
    Читает Document_ПоступлениеДенежныхСредств из 1С за последние N дней.
    Находит счета по external_id_1c или номеру, обновляет status='paid', paid_date.
    Возвращает {"updated": N, "errors": [...]}
    """
    pass  # TODO: реализовать

# ── Склад: NERPA → 1С ──────────────────────────────────────────────────────────

def push_stock_movement(movement, db: Session) -> str | None:
    """
    Пушит только movement_type IN ('in', 'adjustment').
    Движения 'out' с order_id пропускать — они создаются в 1С через заказ.

    'in'  → Document_ПоступлениеТоваров
    'adjustment' → Document_ИнвентаризацияТоваров

    Маппинг для поступления:
      movement.date      → Дата
      movement.product.external_id_1c → Номенклатура
      movement.quantity  → Количество
      movement.notes     → Комментарий
    """
    pass  # TODO: реализовать
```

---

## Шаг 4 — Роутер синхронизации

Создать `app/routers/sync_1c.py`:

- `GET /sync/1c` — страница статуса синхронизации (последний запуск, кол-во объектов, ошибки)
- `POST /sync/1c/products` — ручной запуск импорта номенклатуры из 1С
- `POST /sync/1c/payments` — ручной запуск импорта оплат из 1С
- `POST /sync/1c/counterparty/{id}` — ручной push контрагента
- `POST /sync/1c/order/{id}` — ручной push заказа
- `POST /sync/1c/invoice/{id}` — ручной push счёта
- `POST /sync/1c/stock/{id}` — ручной push движения склада
- `POST /sync/1c/run-all` — полный цикл синхронизации

Добавить роутер в `app/main.py`:
```python
from app.routers import sync_1c
app.include_router(sync_1c.router)
```

---

## Шаг 5 — Автозапуск (polling)

Добавить в `requirements.txt`:
```
apscheduler>=3.10.0
```

В `app/main.py` после создания приложения:
```python
from apscheduler.schedulers.background import BackgroundScheduler
scheduler = BackgroundScheduler()
scheduler.add_job(run_sync_job, 'interval', minutes=15, id='1c_sync')
scheduler.start()
```

`run_sync_job` — вызывает `sync_products_from_1c` и `sync_payments_from_1c`.
Push контрагентов/заказов/счетов — триггерный (при изменении объекта), не по расписанию.

---

## Шаг 6 — Триггеры push при изменениях

В существующих роутерах после сохранения объекта добавить вызов push в фоне:

```python
# app/routers/counterparties.py — в POST /counterparties/new и POST /counterparties/{id}/edit
from app.services.onec_client import push_counterparty
import asyncio, threading
threading.Thread(target=push_counterparty, args=(cp, db), daemon=True).start()

# app/routers/orders.py — при смене статуса на 'confirmed'
# app/routers/invoices.py — при смене статуса на 'issued'
# app/routers/warehouse.py — при создании движения типа 'in' или 'adjustment'
```

---

## Шаг 7 — UI настроек

Добавить блок в `app/templates/settings/index.html`:
- Поля: URL 1С, логин, пароль
- Чекбокс «Синхронизация включена»
- Кнопка «Проверить подключение» (GET `/sync/1c/test`)
- Ссылка на `/sync/1c` (страница статуса)

---

## Риски и решения

| Риск | Решение |
|---|---|
| Дубли контрагентов в 1С | Поиск по ИНН перед созданием |
| 1С недоступна | try/except везде, логировать в AuditLog |
| Конфликт нумерации документов | Хранить номер 1С отдельно, не перезаписывать номер NERPA |
| Единицы измерения | Маппинг unit → код единицы в 1С через CompanySettings или хардкод |
| Двойной push | external_id_1c как идемпотентный ключ |

---

## Порядок реализации

1. Миграция БД (новые поля) — **начинать отсюда**
2. Настройки 1С в CompanySettings + UI
3. OData клиент: тест подключения + чтение номенклатуры
4. Push контрагентов
5. Push заказов
6. Push счетов
7. Push движений склада (только in/adjustment)
8. Импорт оплат из 1С
9. Автополлинг (APScheduler)
10. Страница статуса синхронизации
