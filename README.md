# TMS — Система управления поставками

Веб-система для управления заказами, счетами, контрагентами, договорами и складом.  
Включает Android-приложение для кладовщика, PWA и Telegram-бота с ежедневными отчётами.

## Стек

| Компонент | Технология |
|-----------|-----------|
| Backend | Python 3.10+, FastAPI, SQLAlchemy |
| Frontend | Jinja2 + Bootstrap 5, vanilla JS |
| БД | SQLite (WAL-режим) |
| Документы | PDF — ReportLab, Word — python-docx |
| Мобильное | Android (Kotlin, WebView) + PWA |
| Уведомления | Telegram Bot (python-telegram-bot) |

## Модули

| Модуль | Путь | Доступ |
|--------|------|--------|
| Дашборд | `/` | manager+ |
| Заказы | `/orders` | manager+ |
| Счета | `/invoices` | manager+ |
| Контрагенты | `/counterparties` | manager+ |
| Договоры | `/contracts` | manager+ |
| Склад | `/warehouse` | warehouse+ |
| Отчёты | `/reports` | manager+ |
| Лиды | `/leads` | sales+ |
| Скрипты продаж | `/scripts` | все (правка — sales+) |
| Логистика | `/logistics` | manager+ |
| Табло цеха | `/board` | все |
| Настройки | `/settings` | admin |

## Роли пользователей

`admin` > `manager` = `sales` > `warehouse` > `viewer`

---

## Быстрый старт (Windows, разработка)

```powershell
# 1. Клонировать / распаковать проект
cd C:\TMS\tms

# 2. Создать виртуальное окружение
python -m venv .venv
.venv\Scripts\Activate.ps1

# 3. Установить зависимости
pip install -r requirements.txt

# 4. Настроить конфигурацию
copy .env.example .env
# Откройте .env и заполните SECRET_KEY и ENCRYPT_KEY (см. комментарии в файле)

# 5. Запустить сервер
python run.py
```

Приложение доступно на **http://localhost:8080**.  
При первом запуске создаётся пользователь `admin` — при первом входе потребуется сменить пароль.

**Двойной клик по `start.bat`** — альтернативный запуск без терминала.

---

## Развёртывание на Linux (Ubuntu 22.04 / Debian 12)

### 1. Подготовка сервера

```bash
# Системные зависимости
sudo apt update && sudo apt install -y python3.11 python3.11-venv python3-pip nginx certbot python3-certbot-nginx sqlite3

# Создать системного пользователя без домашней директории
sudo useradd --system --no-create-home --shell /usr/sbin/nologin tms

# Разместить приложение
sudo mkdir -p /opt/tms
sudo git clone <URL репозитория> /opt/tms
# или: sudo tar -xzf tms.tar.gz -C /opt/tms

sudo chown -R tms:tms /opt/tms
```

### 2. Виртуальное окружение и зависимости

```bash
cd /opt/tms
sudo -u tms python3.11 -m venv .venv
sudo -u tms .venv/bin/pip install -r requirements.txt
```

### 3. Конфигурация `.env`

```bash
sudo -u tms cp .env.example .env
sudo nano /opt/tms/.env
```

Обязательно заполните:
- `SECRET_KEY` — случайная строка ≥ 32 символов  
  `python3 -c "import secrets; print(secrets.token_hex(32))"`
- `ENCRYPT_KEY` — Fernet-ключ для шифрования секретов в БД  
  `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`

```bash
sudo chmod 600 /opt/tms/.env
```

### 4. Systemd-сервис

```bash
sudo cp /opt/tms/deploy/tms.service /etc/systemd/system/tms.service
sudo systemctl daemon-reload
sudo systemctl enable tms
sudo systemctl start tms

# Проверить статус
sudo systemctl status tms
curl http://localhost:8080/health   # должно вернуть {"status":"ok"}
```

### 5. Nginx + HTTPS (Let's Encrypt)

```bash
# Скопировать конфиг, заменив ВАШ_ДОМЕН на реальный
sudo cp /opt/tms/deploy/nginx.conf /etc/nginx/sites-available/tms
sudo sed -i 's/ВАШ_ДОМЕН/tms.example.ru/g' /etc/nginx/sites-available/tms
sudo ln -s /etc/nginx/sites-available/tms /etc/nginx/sites-enabled/tms

# Проверить синтаксис и перезагрузить
sudo nginx -t && sudo systemctl reload nginx

# Получить TLS-сертификат (заменить email и домен)
sudo certbot --nginx -d tms.example.ru --email admin@example.ru --agree-tos --non-interactive

# certbot автоматически обновит nginx.conf и настроит автообновление сертификата
```

### 6. Автоматический бэкап БД

```bash
sudo chmod +x /opt/tms/deploy/backup.sh

# Добавить в cron (ежедневно в 03:00)
(crontab -l 2>/dev/null; echo "0 3 * * * /opt/tms/deploy/backup.sh >> /var/log/tms-backup.log 2>&1") | crontab -

# Проверить
/opt/tms/deploy/backup.sh
ls /var/backups/tms/
```

### 7. Обновление

```bash
cd /opt/tms
sudo -u tms git pull
sudo -u tms .venv/bin/pip install -r requirements.txt
sudo systemctl restart tms
```

---

## Telegram-бот (опционально)

Бот отправляет ежедневные/еженедельные/ежемесячные отчёты и напоминает о перезвонах.

```bash
# Настроить в .env:
# TMS_BOT_TOKEN=токен от @BotFather
# TMS_CHAT_IDS=ваш_chat_id (узнать: написать @userinfobot)

# Запуск вручную (для теста)
cd /opt/tms && .venv/bin/python bot/main.py

# Как сервис — создайте tms-bot.service по образцу tms.service,
# заменив ExecStart на: /opt/tms/.venv/bin/python bot/main.py
```

Команды бота: `/daily`, `/weekly`, `/monthly`, `/callbacks`, `/status`,
`/metrics_remind` и `/metrics_pending` (пятничные уведомления по метрике
сотрудников — прогнать вручную, не дожидаясь пятницы)

---

## Android-приложение

Приложение (`tms-sklad.apk`) предоставляет кладовщику мобильный интерфейс склада.

**Распространение через сервер:**
1. Соберите release APK (см. `android/README.md`)
2. Положите APK в `app_dist/tms-sklad.apk`
3. Обновите `app_dist/version.json`:
   ```json
   {"versionCode": 2, "versionName": "1.1", "notes": "Описание обновления"}
   ```
4. Приложение автоматически предложит обновление при следующем запуске

---

## API-документация

FastAPI автоматически генерирует документацию:
- Swagger UI: `http://localhost:8080/docs` (только в dev-режиме)
- OpenAPI JSON: `http://localhost:8080/openapi.json`

В production рекомендуется отключить: добавить `docs_url=None, redoc_url=None` в `FastAPI(...)`.

---

## Структура проекта

```
tms/
├── app/
│   ├── main.py          # FastAPI app, middleware, фильтры Jinja2
│   ├── auth.py          # декораторы login_required, role_required, CSRF
│   ├── models.py        # SQLAlchemy модели
│   ├── database.py      # engine, миграции, seed
│   ├── routers/         # маршруты по модулям
│   ├── templates/       # Jinja2 HTML-шаблоны
│   ├── static/          # CSS, JS, иконки PWA
│   └── utils/           # PDF, DOCX, crypto, recon
├── bot/                 # Telegram-бот
├── android/             # Android-приложение (Kotlin)
├── deploy/              # nginx.conf, tms.service, backup.sh
├── document_templates/  # шаблоны договоров (.docx)
├── generated/           # сгенерированные договоры (не коммитить)
├── app_dist/            # APK + version.json для автообновления
├── .env.example         # пример конфигурации
├── requirements.txt     # зависимости сервера
├── requirements-bot.txt # зависимости бота
└── run.py               # точка входа (uvicorn)
```

---

## Переменные окружения

| Переменная | Обязательна | Описание |
|------------|-------------|----------|
| `SECRET_KEY` | Да | Ключ подписи сессионных cookie |
| `ENCRYPT_KEY` | Да | Fernet-ключ для шифрования секретов в БД |
| `DADATA_TOKEN` | Нет | Автозаполнение реквизитов контрагентов |
| `TMS_BOT_TOKEN` | Нет | Токен Telegram-бота |
| `TMS_CHAT_IDS` | Нет | Получатели отчётов (через запятую) |
| `TMS_DAILY_TIME` | Нет | Время ежедневного отчёта (HH:MM, по умолч. 20:00) |
| `TMS_WEEKLY_TIME` | Нет | Время пятничного отчёта (HH:MM) |
| `TMS_MONTHLY_TIME` | Нет | Время месячного отчёта (HH:MM) |
| `TMS_CALLBACK_TIME` | Нет | Время напоминания о перезвонах (HH:MM) |
| `TMS_HR_METRIC_REMIND_TIME` | Нет | Пятничное напоминание руководителям о метрике (HH:MM, по умолч. 12:00) |
| `TMS_HR_METRIC_CHECK_TIME` | Нет | Пятничная сводка «кто не сдал метрику» (HH:MM, по умолч. 17:30) |
| `TMS_TZ` | Нет | Часовой пояс бота (по умолч. Europe/Moscow) |
| `GLIDE_FB_KEY` | Нет | Firebase API-ключ интеграции с Метафорой |
| `GLIDE_APP_ID` | Нет | ID приложения Glide |
| `BITRIX_PUSH_KEY` | Нет | Ключ приёма сделок из Bitrix24 (см. ниже) |

Полные описания и примеры — в [.env.example](.env.example).
