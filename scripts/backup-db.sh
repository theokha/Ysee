#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DB="${DATABASE_PATH:-$ROOT/data/monitor.db}"
DEST="${1:-$ROOT/data/backups}"
mkdir -p "$DEST"
STAMP="$(date +%F-%H%M%S)"
if [[ ! -f "$DB" ]]; then
  echo "No database at $DB" >&2
  exit 1
fi
cp -p "$DB" "$DEST/monitor-$STAMP.db"
echo "Wrote $DEST/monitor-$STAMP.db"
find "$DEST" -name 'monitor-*.db' -mtime +14 -delete
