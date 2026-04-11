#!/usr/bin/env bash
# db-backup/install.sh
#
# One-shot setup: checks dependencies and registers a daily cron job
# with a randomized minute to avoid load spikes.
#
# Usage:
#   chmod +x install.sh
#   ./install.sh
#
# Run as the user whose crontab should own the job (not root unless necessary).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="$SCRIPT_DIR/logs/backup.log"

echo "=== db-backup installer ==="
echo "Script dir: $SCRIPT_DIR"

# ── 1. Make scripts executable ─────────────────────────────────────────────────
echo "[1/3] Setting permissions…"
chmod +x "$SCRIPT_DIR/backup.sh" "$SCRIPT_DIR/restore.sh"
echo "      backup.sh and restore.sh are now executable."

# ── 2. Check dependencies ──────────────────────────────────────────────────────
echo "[2/3] Checking dependencies…"

MISSING=()

for cmd in aws jq curl gzip sha256sum bc; do
  if ! command -v "$cmd" >/dev/null 2>&1; then
    MISSING+=("$cmd")
  fi
done

# Check at least one DB client is present
if ! command -v pg_dump >/dev/null 2>&1 && ! command -v mysqldump >/dev/null 2>&1; then
  MISSING+=("pg_dump or mysqldump")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
  echo ""
  echo "      WARNING: The following required tools are missing:"
  for m in "${MISSING[@]}"; do
    echo "        - $m"
  done
  echo "      Install them before running backup.sh."
  echo ""
else
  echo "      All required dependencies found."
fi

# ── 3. Ensure .env exists ─────────────────────────────────────────────────────
echo "[3/4] Checking .env…"
if [ ! -f "$SCRIPT_DIR/.env" ]; then
  cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
  chmod 600 "$SCRIPT_DIR/.env"
  echo "      Created .env from .env.example."
  echo "      *** IMPORTANT: Edit $SCRIPT_DIR/.env before the first backup runs! ***"
else
  echo "      .env already exists, not overwriting."
fi

# ── 4. Register cron job ───────────────────────────────────────────────────────
echo "[4/4] Registering cron job…"

mkdir -p "$SCRIPT_DIR/logs"

# Randomize minute (0-59) so all servers don't hit S3 at the same second.
CRON_MINUTE=$(( RANDOM % 60 ))
# Default: daily at 02:XX — adjust the hour here if needed.
CRON_HOUR=2
CRON_CMD="$SCRIPT_DIR/backup.sh >> $LOG_FILE 2>&1"
CRON_ENTRY="${CRON_MINUTE} ${CRON_HOUR} * * * ${CRON_CMD}"
CRON_MARKER="# db-backup managed cron"

# Remove any existing db-backup cron entry, then add the new one.
( crontab -l 2>/dev/null | grep -v "$CRON_MARKER" ; echo "${CRON_ENTRY} ${CRON_MARKER}" ) | crontab -

echo "      Cron job registered: daily at ${CRON_HOUR}:$(printf '%02d' $CRON_MINUTE) UTC"
echo "      Full entry: $CRON_ENTRY"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "Post-install checklist:"
echo "  1. Edit .env with your DB credentials and S3 settings."
echo ""
echo "  2. Set up a least-privilege DB backup user:"
echo "     PostgreSQL: CREATE USER backup_user WITH PASSWORD '...'; GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_user;"
echo "     MySQL/MariaDB: GRANT SELECT, LOCK TABLES, SHOW VIEW ON mydb.* TO 'backup_user'@'localhost';"
echo ""
echo "  3. Set up a least-privilege S3 IAM/API policy (allow only on your bucket/prefix):"
echo "     Actions: s3:PutObject, s3:GetObject, s3:DeleteObject, s3:ListBucket"
echo ""
echo "  4. Test manually:  $SCRIPT_DIR/backup.sh"
echo "  5. Verify restore: bash $SCRIPT_DIR/restore.sh --dry-run --s3 <s3_key>"
echo "  6. Check crontab:  crontab -l"
