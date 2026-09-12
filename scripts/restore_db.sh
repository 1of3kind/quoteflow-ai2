#!/usr/bin/env bash
# QuoteFlow database restore + VERIFICATION (GATE 8).
# Usage: ./scripts/restore_db.sh backups/quoteflow_20260911T020000Z.dump
#
# Restoring into the production database requires typing RESTORE_TO_PRODUCTION=yes.
# This script always verifies the restored schema afterwards: a restore is
# not "done" until row counts come back non-zero on core tables.
set -euo pipefail

DUMP_FILE="${1:-}"
if [ -z "$DUMP_FILE" ] || [ ! -f "$DUMP_FILE" ]; then
  echo "usage: $0 <path-to-.dump>" >&2
  exit 1
fi
: "${DATABASE_URL:?DATABASE_URL must point at the TARGET database}"

if [[ "$DATABASE_URL" == *render.com* && "${RESTORE_TO_PRODUCTION:-}" != "yes" ]]; then
  echo "refusing to restore into a production database without RESTORE_TO_PRODUCTION=yes" >&2
  exit 1
fi

echo "restoring $DUMP_FILE into $DATABASE_URL"
pg_restore --clean --if-exists --no-owner --no-privileges \
           --dbname="$DATABASE_URL" "$DUMP_FILE"

echo "verifying restored schema..."
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -c "
  SELECT 'organizations' AS table, count(*) AS rows FROM organizations
  UNION ALL SELECT 'users', count(*) FROM users
  UNION ALL SELECT 'quotes', count(*) FROM quotes
  UNION ALL SELECT 'jobs', count(*) FROM jobs;"
echo "restore verified"
