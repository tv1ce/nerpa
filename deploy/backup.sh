#!/usr/bin/env bash
# Ежедневный бэкап tms.db
# Добавить в cron (crontab -e):
#   0 3 * * * /opt/tms/deploy/backup.sh >> /var/log/tms-backup.log 2>&1

set -euo pipefail

APP_DIR="/opt/tms"
BACKUP_DIR="/var/backups/tms"
KEEP_DAYS=30

mkdir -p "$BACKUP_DIR"

STAMP=$(date +"%Y%m%d_%H%M%S")
DEST="$BACKUP_DIR/tms_${STAMP}.db"

# sqlite3 .backup — онлайн-бэкап без блокировки WAL
sqlite3 "$APP_DIR/tms.db" ".backup '$DEST'"
gzip "$DEST"

echo "[$(date)] Бэкап создан: ${DEST}.gz"

# Удаляем старые бэкапы
find "$BACKUP_DIR" -name "tms_*.db.gz" -mtime +"$KEEP_DAYS" -delete
echo "[$(date)] Хранится бэкапов: $(ls "$BACKUP_DIR"/tms_*.db.gz 2>/dev/null | wc -l)"
