#!/usr/bin/env bash
# QuoteFlow automated database backup (GATE 8).
# Usage:   ./scripts/backup_db.sh
# Cron:    0 2 * * *  /app/scripts/backup_db.sh >> /var/log/quoteflow_backup.log 2>&1
#
# Retention: BACKUP_RETENTION_DAYS (default 30). Run scripts/restore_db.sh
# after any restore drill; a backup that has never been restored is not a
# backup strategy.
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-30}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p "$BACKUP_DIR"

if [ -n "${DATABASE_URL:-}" ]; then
  TARGET="$BACKUP_DIR/quoteflow_${TIMESTAMP}.dump"
  pg_dump --format=custom --no-owner --no-privileges \
          --dbname="$DATABASE_URL" --file="$TARGET"
  echo "[$(date -u +%FT%TZ)] backup written: $TARGET ($(du -h "$TARGET" | cut -f1))"
else
  echo "DATABASE_URL is not set — refusing to run" >&2
  exit 1
fi

# Prune backups older than the retention window
find "$BACKUP_DIR" -name 'quoteflow_*.dump' -mtime +"$RETENTION_DAYS" -delete
echo "[$(date -u +%FT%TZ)] pruned backups older than ${RETENTION_DAYS} days"
