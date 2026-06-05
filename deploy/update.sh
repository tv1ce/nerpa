#!/usr/bin/env bash
# =============================================================================
# TMS — быстрое обновление продакшн-сервера из ветки main
#
# Использование (на сервере от root):
#   sudo bash /opt/tms/deploy/update.sh
#
# Что делает:
#   1. git pull origin main в /opt/tms
#   2. Обновляет Python-зависимости если изменились requirements
#   3. Перезапускает tms и tms-bot
# =============================================================================

set -euo pipefail

APP_DIR="/opt/tms"
VENV="$APP_DIR/.venv"
REPO_DIR="/root/tms-repo"   # git-репозиторий отдельно от рабочей папки

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()    { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()   { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && die "Запустите от root: sudo bash deploy/update.sh"

echo ""
echo "  ╔══════════════════════════════════════════════╗"
echo "  ║   TMS — деплой релиза из ветки main          ║"
echo "  ╚══════════════════════════════════════════════╝"
echo ""

# 1. Тянем свежий main в репо
info "git pull origin main..."
cd "$REPO_DIR"
git fetch origin
git checkout main
git pull origin main --ff-only
ok "Код обновлён до $(git rev-parse --short HEAD) ($(git log -1 --format='%s'))"

# 2. Синхронизируем файлы в /opt/tms (исключаем данные и служебное)
info "Синхронизация файлов в $APP_DIR..."
rsync -a --delete \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='.env' \
    --exclude='logs/' \
    --exclude='generated/' \
    --exclude='app_dist/' \
    --exclude='uploads/' \
    --exclude='tests/' \
    --exclude='android/' \
    "$REPO_DIR/" "$APP_DIR/"
ok "Файлы синхронизированы"

# 3. Обновляем зависимости
info "Проверка Python-зависимостей..."
"$VENV/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet
"$VENV/bin/pip" install -r "$APP_DIR/requirements-bot.txt" --quiet
ok "Зависимости актуальны"

# 4. Права на новые файлы
chown -R tms:tms "$APP_DIR"
chmod -R 750 "$APP_DIR"
chmod 600 "$APP_DIR/.env" 2>/dev/null || true
chmod -R 755 "$APP_DIR/app/static"
chmod o+x "$APP_DIR" "$APP_DIR/app"

# 4. Перезапуск сервисов
info "Перезапуск tms..."
systemctl restart tms
systemctl is-active --quiet tms \
    && ok "tms запущен" \
    || die "tms не запустился — проверьте: journalctl -u tms -n 50"

if grep -q "^TMS_BOT_TOKEN=.\+" "$APP_DIR/.env" 2>/dev/null; then
    info "Перезапуск tms-bot..."
    systemctl restart tms-bot
    systemctl is-active --quiet tms-bot \
        && ok "tms-bot запущен" \
        || warn "tms-bot не запустился — journalctl -u tms-bot -n 50"
fi

echo ""
echo "  ┌─────────────────────────────────────────────┐"
echo "  │  Деплой завершён ✓                           │"
echo "  │  Версия: $(git rev-parse --short HEAD)                          │"
echo "  │  Логи:   journalctl -u tms -f                │"
echo "  └─────────────────────────────────────────────┘"
echo ""
