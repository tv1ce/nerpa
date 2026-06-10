#!/usr/bin/env bash
# =============================================================================
# TMS — скрипт первичного развёртывания на чистом VDS (Ubuntu 22.04 / Debian 12)
#
# Использование:
#   git clone <repo> /tmp/tms-src
#   cd /tmp/tms-src/tms
#   sudo bash deploy/deploy.sh
#
# Что делает скрипт:
#   1. Устанавливает системные зависимости (Python 3, nginx, sqlite3, certbot)
#   2. Создаёт системного пользователя tms
#   3. Копирует файлы приложения в /opt/tms
#   4. Создаёт виртуальное окружение и ставит зависимости
#   5. Настраивает .env (если не существует)
#   6. Устанавливает и включает systemd-сервисы (tms, tms-bot)
#   7. Настраивает nginx и logrotate
#   8. Регистрирует cron-задание для резервного копирования БД
#   9. Запускает сервисы
#
# Повторный запуск (обновление):
#   sudo bash deploy/deploy.sh update
# =============================================================================

set -euo pipefail

# ── Параметры ─────────────────────────────────────────────────────────────────
APP_DIR="/opt/tms"
APP_USER="tms"
APP_GROUP="tms"
VENV="$APP_DIR/.venv"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"   # корень tms/
MODE="${1:-install}"                           # install | update

# ── Цвета ─────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── Проверки ──────────────────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && die "Запустите скрипт от root: sudo bash deploy/deploy.sh"

OS_ID=$(. /etc/os-release && echo "$ID")
[[ "$OS_ID" =~ ^(ubuntu|debian)$ ]] || warn "Тестировалось на Ubuntu/Debian. Продолжаем на свой страх и риск."

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   TMS — развёртывание на Linux VDS           ║"
echo "  ║   Режим: $MODE                                ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# ─────────────────────────────────────────────────────────────────────────────
# 1. Системные зависимости
# ─────────────────────────────────────────────────────────────────────────────
info "Обновление пакетов..."
apt-get update -qq

PKGS=(
    python3 python3-venv python3-pip
    nginx
    sqlite3
    certbot python3-certbot-nginx
    curl wget git
    logrotate
)

info "Установка: ${PKGS[*]}"
apt-get install -y -qq "${PKGS[@]}"
ok "Системные зависимости установлены"

# ─────────────────────────────────────────────────────────────────────────────
# 2. Пользователь и директория
# ─────────────────────────────────────────────────────────────────────────────
if ! id "$APP_USER" &>/dev/null; then
    info "Создание пользователя $APP_USER..."
    useradd --system --no-create-home --shell /usr/sbin/nologin "$APP_USER"
    ok "Пользователь $APP_USER создан"
else
    info "Пользователь $APP_USER уже существует"
fi

mkdir -p "$APP_DIR"/{logs,generated,app_dist,document_templates}

# ─────────────────────────────────────────────────────────────────────────────
# 3. Копирование файлов приложения
# ─────────────────────────────────────────────────────────────────────────────
info "Синхронизация файлов приложения → $APP_DIR..."

rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.db' \
    --exclude='.env' \
    --exclude='logs/' \
    --exclude='generated/' \
    --exclude='document_templates/' \
    --exclude='app_dist/' \
    --exclude='tests/' \
    --exclude='android/' \
    "$SRC_DIR/" "$APP_DIR/"

ok "Файлы скопированы"

# ─────────────────────────────────────────────────────────────────────────────
# 4. Виртуальное окружение и зависимости
# ─────────────────────────────────────────────────────────────────────────────
if [[ ! -d "$VENV" ]]; then
    info "Создание виртуального окружения..."
    python3 -m venv "$VENV"
fi

info "Установка Python-зависимостей..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements-bot.txt" --quiet
ok "Python-зависимости установлены"

# ─────────────────────────────────────────────────────────────────────────────
# 5. Файл окружения .env
# ─────────────────────────────────────────────────────────────────────────────
if [[ ! -f "$APP_DIR/.env" ]]; then
    warn ".env не найден — создаю из шаблона."
    warn "ОБЯЗАТЕЛЬНО заполните $APP_DIR/.env перед запуском сервисов!"
    cp "$APP_DIR/.env.example" "$APP_DIR/.env"

    # Генерируем безопасные ключи автоматически
    SECRET_KEY=$("$VENV/bin/python3" -c "import secrets; print(secrets.token_hex(32))")
    ENCRYPT_KEY=$("$VENV/bin/python3" -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
    sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$SECRET_KEY|" "$APP_DIR/.env"
    sed -i "s|^ENCRYPT_KEY=.*|ENCRYPT_KEY=$ENCRYPT_KEY|" "$APP_DIR/.env"

    ok "Ключи безопасности сгенерированы"
    echo ""
    echo "  ┌─────────────────────────────────────────────┐"
    echo "  │  Отредактируйте /opt/tms/.env:              │"
    echo "  │    nano /opt/tms/.env                        │"
    echo "  │                                              │"
    echo "  │  Минимум укажите:                            │"
    echo "  │    TMS_BOT_TOKEN  — токен Telegram-бота      │"
    echo "  │    TMS_CHAT_IDS   — ваш Telegram chat_id     │"
    echo "  └─────────────────────────────────────────────┘"
    echo ""
else
    info ".env уже существует, не перезаписываем"
fi

# Ограничиваем доступ к .env
chmod 600 "$APP_DIR/.env"

# ─────────────────────────────────────────────────────────────────────────────
# 6. Права на файлы
# ─────────────────────────────────────────────────────────────────────────────
info "Установка прав на файлы..."
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod -R 750 "$APP_DIR"
chmod 600 "$APP_DIR/.env"
# Статика и шаблоны читаются nginx напрямую
chmod -R 755 "$APP_DIR/app/static"
# nginx (www-data) должен иметь возможность ПРОЙТИ в каталоги до статики.
# Без o+x на /opt/tms и /opt/tms/app nginx получает 403 на /static/* (Permission denied).
chmod o+x "$APP_DIR" "$APP_DIR/app"
ok "Права установлены"

# ─────────────────────────────────────────────────────────────────────────────
# 7. Systemd-сервисы
# ─────────────────────────────────────────────────────────────────────────────
info "Установка systemd-сервисов..."

cp "$APP_DIR/deploy/tms.service"     /etc/systemd/system/tms.service
cp "$APP_DIR/deploy/tms-bot.service" /etc/systemd/system/tms-bot.service

systemctl daemon-reload
systemctl enable tms tms-bot
ok "Сервисы tms и tms-bot зарегистрированы"

# ─────────────────────────────────────────────────────────────────────────────
# 8. Nginx
# ─────────────────────────────────────────────────────────────────────────────
info "Настройка nginx..."

NGINX_CONF="/etc/nginx/sites-available/tms"
cp "$APP_DIR/deploy/nginx.conf" "$NGINX_CONF"

# Удаляем дефолтный сайт, если он мешает
if [[ -L /etc/nginx/sites-enabled/default ]]; then
    rm -f /etc/nginx/sites-enabled/default
    info "Удалён дефолтный nginx-сайт"
fi

if [[ ! -L /etc/nginx/sites-enabled/tms ]]; then
    ln -s "$NGINX_CONF" /etc/nginx/sites-enabled/tms
fi

# Минимальная проверка конфига
if nginx -t 2>/dev/null; then
    ok "nginx конфиг корректен"
else
    warn "nginx -t: ошибки в конфиге (возможно, SSL ещё не настроен — это нормально)"
    warn "Настройте домен и запустите: sudo certbot --nginx -d ВАШ_ДОМЕН"
fi

# ─────────────────────────────────────────────────────────────────────────────
# 9. Logrotate
# ─────────────────────────────────────────────────────────────────────────────
info "Установка logrotate..."
cp "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/tms
ok "Logrotate настроен"

# ─────────────────────────────────────────────────────────────────────────────
# 10. Cron — ежедневный бэкап БД
# ─────────────────────────────────────────────────────────────────────────────
info "Настройка cron-бэкапа..."

BACKUP_SCRIPT="$APP_DIR/deploy/backup.sh"
chmod +x "$BACKUP_SCRIPT"

CRON_LINE="0 3 * * * $BACKUP_SCRIPT >> /var/log/tms-backup.log 2>&1"
CRON_TMP=$(mktemp)

# Добавляем только если строки ещё нет
crontab -l 2>/dev/null | grep -qF "$BACKUP_SCRIPT" \
    && info "Cron-задание уже существует" \
    || { crontab -l 2>/dev/null > "$CRON_TMP" || true
         echo "$CRON_LINE" >> "$CRON_TMP"
         crontab "$CRON_TMP"
         ok "Cron-задание добавлено (03:00 каждый день)"
       }
rm -f "$CRON_TMP"

# ─────────────────────────────────────────────────────────────────────────────
# 11. Запуск / перезапуск сервисов
# ─────────────────────────────────────────────────────────────────────────────
info "Запуск сервисов..."

systemctl restart tms
systemctl is-active --quiet tms && ok "tms запущен" || warn "tms не запустился — проверьте: journalctl -u tms -n 50"

# Бот запускаем только если TMS_BOT_TOKEN задан в .env
if grep -q "^TMS_BOT_TOKEN=.\+" "$APP_DIR/.env" 2>/dev/null; then
    systemctl restart tms-bot
    systemctl is-active --quiet tms-bot && ok "tms-bot запущен" || warn "tms-bot не запустился — проверьте: journalctl -u tms-bot -n 50"
else
    warn "TMS_BOT_TOKEN не задан — tms-bot не запущен. Добавьте токен в .env и запустите: sudo systemctl start tms-bot"
fi

# Nginx перезагружаем только если конфиг валиден
if nginx -t 2>/dev/null; then
    systemctl reload nginx || systemctl restart nginx
    ok "nginx перезагружен"
fi

# ─────────────────────────────────────────────────────────────────────────────
# Итог
# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "  ┌─────────────────────────────────────────────┐"
echo "  │            Развёртывание завершено           │"
echo "  ├─────────────────────────────────────────────┤"
echo "  │  Приложение: http://$(hostname -I | awk '{print $1}'):8080      │"
echo "  │  Файлы:      /opt/tms                        │"
echo "  │  Логи:       journalctl -u tms -f            │"
echo "  │              tail -f /opt/tms/logs/tms.log   │"
echo "  ├─────────────────────────────────────────────┤"
echo "  │  Следующие шаги:                             │"
echo "  │  1. nano /opt/tms/.env  — заполните секреты  │"
echo "  │  2. Укажите домен в nginx.conf               │"
echo "  │  3. sudo certbot --nginx -d ВАШ_ДОМЕН        │"
echo "  └─────────────────────────────────────────────┘"
echo ""
