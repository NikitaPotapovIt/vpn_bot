#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="$REPO_ROOT/backup_config"
SCRIPT_PATH="$REPO_ROOT/scripts/backup_vpn_configs.py"
LOG_PATH="$BACKUP_DIR/backup.log"
PYTHON_BIN="$REPO_ROOT/venv/bin/python"

mkdir -p "$BACKUP_DIR"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="/usr/bin/env python3"
fi

CRON_LINE="CRON_TZ=Europe/Moscow 0 4 * * * cd \"$REPO_ROOT\" && $PYTHON_BIN \"$SCRIPT_PATH\" >> \"$LOG_PATH\" 2>&1"

CURRENT_CRON="$(crontab -l 2>/dev/null || true)"
{
  printf '%s\n' "$CURRENT_CRON" | grep -Fv "backup_vpn_configs.py" || true
  printf '%s\n' "$CRON_LINE"
} | crontab -

echo "Cron installed:"
echo "$CRON_LINE"
