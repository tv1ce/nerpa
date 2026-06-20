# 1С:УНФ — контекст задачи (продолжение)

## Архитектурные решения (зафиксированы)

| Направление | Описание |
|---|---|
| TMS → 1С | Контрагенты, Заказы (создание + смена статуса), Счета (при выставлении) |
| 1С → TMS | Статус оплаты счетов (`Invoice.status = 'paid'`, `paid_date`) |
| Склад | TMS отдаёт остатки в 1С; движения `out` с `order_id` не пушатся (1С создаёт их сама через заказ) |
| Битрикс | Вне скоупа пока |

## Что уже сделано (коммит `c674d96`)

- `app/database.py` — миграции новых полей
- `app/models.py` — поля в моделях

Новые поля БД:
- `counterparties`: `external_id_1c`, `synced_to_1c_at`
- `products`: `external_id_1c`, `synced_from_1c_at`
- `orders`: `external_id_1c`, `synced_to_1c_at`
- `invoices`: `external_id_1c`, `synced_to_1c_at`
- `stock_movements`: `external_id_1c`, `synced_to_1c_at`
- `company_settings`: `onec_url`, `onec_user`, `onec_password`, `onec_enabled`

## Что делать дальше (по порядку)

### Шаг 3 — OData клиент
Создать `app/services/onec_client.py` (заглушки уже описаны в `docs/1c_unf_integration.md`).
Нужен адрес тестовой 1С: `http://<сервер>/hnf/odata/standard.odata`.
Первое что реализовать: `test_connection()` + `sync_products_from_1c()`.

### Шаг 4 — UI настроек
Добавить блок «1С:УНФ» в `app/templates/settings/index.html`:
- поля URL, логин, пароль
- чекбокс «Синхронизация включена»
- кнопка «Проверить подключение» → `GET /sync/1c/test`

### Шаг 5 — Роутер `/sync/1c`
Создать `app/routers/sync_1c.py` (список эндпоинтов в `docs/1c_unf_integration.md`, Шаг 4).
Подключить в `app/main.py`.

### Шаг 6 — Push контрагентов
В `app/routers/counterparties.py` при POST создания и редактирования —
вызов `push_counterparty()` в фоновом потоке (если `onec_enabled`).

### Шаг 7 — Push заказов
В `app/routers/orders.py` при смене статуса на `confirmed` → `push_order()`.
При каждой следующей смене статуса — PATCH в 1С (обновление статуса).

### Шаг 8 — Push счетов
В `app/routers/invoices.py` при смене статуса на `issued` → `push_invoice()`.

### Шаг 9 — Pull оплат
`sync_payments_from_1c()` — поллинг `Document_ПоступлениеДенежныхСредств` раз в час.
Реализовать через APScheduler (добавить в `requirements.txt`).

### Шаг 10 — Страница статуса `/sync/1c`
Последний запуск, кол-во синхронизированных объектов, список ошибок из `AuditLog`.

## Полная спека
`docs/1c_unf_integration.md` — маппинг полей, примеры OData-запросов, риски.

## Как возобновить сессию с Клодом

Скопируй это сообщение и отправь в новый чат:

---
Продолжаем интеграцию TMS с 1С:УНФ. Прочитай файл `docs/1c_resume.md` в проекте — там весь контекст. БД уже подготовлена (коммит c674d96). Следующий шаг — реализация OData-клиента (`app/services/onec_client.py`) и UI настроек. Адрес 1С: [ВСТАВЬ АДРЕС].
---
