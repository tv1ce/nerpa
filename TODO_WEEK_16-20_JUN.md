# TODO — Неделя 16–20 июня

**Цель:** закрыть три оставшихся `P1`: клиентский портал, контактные лица, инвентаризация.

---

## 📌 Пн 16 июня — Клиентский портал `/track/{token}` (часть 1)

### Модель + миграция
- [ ] В `app/models.py` добавить поле `Order`:
  - `public_token: String(36)` (UUID, nullable=False, unique=True, index)
  - `public_token_created_at: DateTime` (server_default)
  - При создании заказа генерируется UUID через `uuid.uuid4().hex`

### Роутер (публичный эндпоинт)
- [ ] Создать `app/routers/public.py`:
  - `GET /track/{token}` — **вне `login_required`**
  - Найти заказ по `public_token`
  - Вернуть только безопасные поля: номер, статус, дата создания, плановая отгрузка, состав позиций (название + кол-во, БЕЗ цен закупки/маржи)
  - Rate-limit (например, 100 req/10min по IP)
  - Обработка: токен не найден → 404

### Шаблон публичного трекинга
- [ ] Создать `app/templates/public/track.html`:
  - Заголовок: «Статус вашего заказа»
  - Таймлайн статусов: Создан → Подтверждён → Собран → Отгружен → Доставлен
  - Текущий статус выделен, пройденные — зелёные
  - Блок информации: номер заказа, дата, контакт менеджера (имя + телефон)
  - Состав: таблица позиций (товар, количество)
  - Плановая дата отгрузки/доставки (если указана)
  - Ссылка на документы (счёт, договор) — если они загружены в заказ
  - Стиль: минималистичный, адаптив под мобильный, без логотипа компании (анон)
  - Кнопка WhatsApp/Telegram менеджеру (если в контрагенте указан номер)

---

## 🌤 Вт 17 июня — Клиентский портал `/track/{token}` (часть 2) + кнопка в UI

### Интеграция в заказ
- [ ] В `app/routers/orders.py` добавить в `view_order`:
  - Генерация токена при первом обращении (если не существует)
  - Вывод ссылки для копирования: `https://nuttshell.ru/track/{token}`

### UI в карточке заказа
- [ ] В `app/templates/orders/detail.html` добавить:
  - Блок под номером заказа: иконка + текст «Ссылка для клиента»
  - Кнопка с иконкой копирования (copy to clipboard)
  - При клике — копируется полная ссылка + уведомление «скопирована»
  - Опционально: быстрая отправка в Telegram-бот (через кнопку «Отправить в бот»)

### Тестирование
- [ ] Проверить:
  - Генерация токена (видно в БД после первого заказа)
  - Публичная ссылка открывается без логина ✓
  - Rate-limit работает (много запросов с одного IP) ✓
  - Мобильный вид (телефон) ✓
  - Копирование ссылки ✓

---

## ⭐ Ср 18 июня — Контрагенты: несколько контактных лиц

### Модель
- [ ] В `app/models.py` создать `ContactPerson`:
  ```python
  class ContactPerson(Base):
      __tablename__ = "contact_persons"
      id = Column(Integer, primary_key=True)
      counterparty_id = Column(Integer, ForeignKey("counterparties.id"), nullable=False)
      name = Column(String(150), nullable=False)
      position = Column(String(100))  # должность
      phone = Column(String(20))
      email = Column(String(120))
      telegram = Column(String(50))  # @handle или ID
      whatsapp = Column(String(20))
      created_at = Column(DateTime, server_default=func.now())
      counterparty = relationship("Counterparty", back_populates="contact_persons")
  
  # В Counterparty добавить:
  contact_persons = relationship("ContactPerson", back_populates="counterparty", cascade="all, delete-orphan")
  ```
- [ ] В миграции: таблица создаётся через `create_all()`

### Роутер
- [ ] В `app/routers/counterparties.py`:
  - `POST /counterparties/{cp_id}/contacts` — добавить контакт (JSON: name, position, phone, email, telegram, whatsapp)
  - `DELETE /counterparties/{cp_id}/contacts/{contact_id}` — удалить контакт
  - `PUT /counterparties/{cp_id}/contacts/{contact_id}` — редактировать контакт

### Виджет в карточке КА
- [ ] В `app/templates/counterparties/detail.html` добавить вкладку **«Контактные лица»**:
  - Таблица: Имя | Должность | Телефон | Email | Telegram | Действия
  - Кнопка «Добавить контакт» → модальное окно (форма)
  - Кнопка редактирования/удаления в каждой строке
  - Если контактов нет: «Добавьте первого контакта»

### Тестирование
- [ ] Добавить несколько контактов в одного КА ✓
- [ ] Редактировать, удалять ✓
- [ ] Проверить каскадное удаление при удалении КА ✓

---

## 📦 Чт 19 июня — Склад: инвентаризация (часть 1)

### Модель
- [ ] В `app/models.py` создать `StockAdjustment`:
  ```python
  class StockAdjustment(Base):
      __tablename__ = "stock_adjustments"
      id = Column(Integer, primary_key=True)
      adjustment_date = Column(Date, nullable=False, server_default=func.date('now'))
      created_by_id = Column(Integer, ForeignKey("users.id"), nullable=False)
      created_at = Column(DateTime, server_default=func.now())
      reason = Column(String(500))  # "Инвентаризация", например
      status = Column(String(20), default="completed")  # черновик/завершена
      created_by = relationship("User")
  
  class StockAdjustmentLine(Base):
      __tablename__ = "stock_adjustment_lines"
      id = Column(Integer, primary_key=True)
      adjustment_id = Column(Integer, ForeignKey("stock_adjustments.id"), cascade="all, delete-orphan")
      product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
      expected_qty = Column(Integer)  # было в системе
      actual_qty = Column(Integer, nullable=False)  # ввели при инвентаризации
      adjustment_id_fk = relationship("StockAdjustment", back_populates="lines")
      product = relationship("Product")
  ```

### Роутер
- [ ] В `app/routers/logistics.py` (или отдельно `warehouse.py`):
  - `GET /inventory/new` — форма создания инвентаризации (пустая таблица с полями: товар, факт)
  - `POST /inventory` — сохранить запись, создать StockAdjustment + StockAdjustmentLine, применить корректировки
  - `GET /inventory/{id}` — просмотр завершённой инвентаризации (что было, что стало, разницы)
  - `GET /inventory` — список инвентаризаций (дата, кто создал, кол-во позиций)

### Логика корректировок
- [ ] При сохранении инвентаризации:
  - Для каждой строки: разница = `actual_qty - expected_qty`
  - Если разница > 0 → создать приход на `StockMovement`
  - Если разница < 0 → создать расход на `StockMovement`
  - Обновить `Product.quantity` для каждого товара
  - Записать причину: "Корректировка при инвентаризации от {дата}"

---

## 📦 Пт 20 июня — Склад: инвентаризация (часть 2) + интеграция

### Шаблон формы
- [ ] Создать `app/templates/warehouse/inventory.html`:
  - Заголовок: «Инвентаризация»
  - Дата инвентаризации (дефолт = сегодня)
  - Таблица: Товар (select) | Текущий остаток (read-only) | Факт (input number) | Разница (auto-calc)
  - Кнопка «Добавить строку»
  - Кнопка «Удалить строку» для каждой
  - Кнопка «Применить корректировки» (валидация: все поля заполнены)
  - После сохранения → редирект на просмотр инвентаризации

### Интеграция в меню
- [ ] В `app/templates/base.html` добавить ссылку на `/inventory` в меню складa (рядом с приходами/расходами)

### Отчёт после инвентаризации
- [ ] В просмотре инвентаризации показать:
  - Таблица: Товар | Было | Стало | Разница | Операция (Приход/Расход)
  - Итоговая сводка: всего позиций, создано приходов, создано расходов
  - Кнопка «Вернуться в список»

### Тестирование
- [ ] Создать инвентаризацию с 5–10 товарами ✓
- [ ] Проверить, что разницы рассчитались правильно ✓
- [ ] Проверить, что корректировки применились (товары обновились в системе) ✓
- [ ] Проверить, что в истории складских движений появились новые записи ✓
- [ ] Мобильный вид (таблица адаптивна) ✓

---

## 🧪 Пт 20 июня — Буфер & релиз

- [ ] Прогонка основных сценариев в браузере:
  - Публичная ссылка заказа ✓
  - Добавление/удаление контактных лиц ✓
  - Полный цикл инвентаризации ✓
- [ ] Проверка на наявность SQL-ошибок, 500-ок ✓
- [ ] Коммит: объединяет все три фичи
- [ ] Деплой на прод

---

## 📊 Резюме по дням

| День | Фича | Готовность | Зависимости |
|---|---|---|---|
| **Пн–Вт** | Клиентский портал | 100% UI + логика | модель `Order.public_token` |
| **Ср** | Контакты в КА | 100% CRUD + UI | модель `ContactPerson` |
| **Чт–Пт** | Инвентаризация | 100% логика + форма | модель `StockAdjustment(Line)` |
| **Пт** | Тестирование + деплой | готово | все выше |

**Коммит:** `feat: клиентский портал /track, контактные лица в КА, инвентаризация`  
**Деплой:** при 100% готовности (в пт вечер или пн следующей недели)
