#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOST="${VPS_HOST:-ubuntu@43.153.196.115}"
KEY="${VPS_SSH_KEY:-$HOME/Documents/Theo/theo1.pem}"
REMOTE="${VPS_REMOTE_DIR:-~/yc-monitor}"

cd "$ROOT"
if [[ -x "$ROOT/.venv/bin/pytest" ]]; then
  "$ROOT/.venv/bin/ruff" check src tests
  "$ROOT/.venv/bin/mypy" src
  "$ROOT/.venv/bin/pytest" -q tests
fi

rsync -avz -e "ssh -i $KEY" \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude '.env' --exclude 'data' --exclude '*.db' --exclude '*.pem' \
  --exclude '.pytest_cache' --exclude '.mypy_cache' --exclude '.ruff_cache' \
  "$ROOT/" "$HOST:$REMOTE/"

ssh -i "$KEY" "$HOST" "bash -s" <<REMOTE
set -euo pipefail
cd $REMOTE
mkdir -p data/backups
if [[ -f data/monitor.db ]]; then
  cp -p data/monitor.db "data/backups/monitor-\$(date +%F-%H%M%S).db"
fi
sudo docker compose build --no-cache
sudo docker compose up -d --force-recreate
sleep 2
curl -fsS --max-time 10 http://127.0.0.1/healthz
echo
REMOTE
