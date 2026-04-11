#!/usr/bin/env bash
# db-backup/restore.sh
#
# Restore or verify a backup produced by backup.sh.
#
# Usage:
#   # Verify integrity only (no DB changes):
#   bash restore.sh --dry-run <backup.sql.gz>
#   bash restore.sh --dry-run --s3 <s3_key>
#
#   # Full restore to database (destructive):
#   bash restore.sh --restore <backup.sql.gz>
#   bash restore.sh --restore --s3 <s3_key>

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Logging ────────────────────────────────────────────────────────────────────

log() {
  local level="$1"; shift
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  echo "$ts [$level] $*"
}

# ── Usage ──────────────────────────────────────────────────────────────────────

usage() {
  cat >&2 <<EOF
Usage:
  $(basename "$0") --dry-run <backup.sql.gz>          Verify integrity, no DB changes
  $(basename "$0") --dry-run --s3 <s3_key>            Download from S3 then verify
  $(basename "$0") --restore <backup.sql.gz>          Full restore to database
  $(basename "$0") --restore --s3 <s3_key>            Download from S3 then restore
EOF
  exit 1
}

# ── Argument parsing ───────────────────────────────────────────────────────────

DRY_RUN=0
RESTORE=0
FROM_S3=0
TARGET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=1
      TARGET="${2:-}"
      [ -z "$TARGET" ] && { echo "ERROR: --dry-run requires a file path or S3 key" >&2; usage; }
      shift 2
      ;;
    --restore)
      RESTORE=1
      TARGET="${2:-}"
      [ -z "$TARGET" ] && { echo "ERROR: --restore requires a file path or S3 key" >&2; usage; }
      shift 2
      ;;
    --s3)
      FROM_S3=1
      shift
      ;;
    *)
      echo "ERROR: Unknown argument: $1" >&2
      usage
      ;;
  esac
done

if [ "$DRY_RUN" -eq 1 ] && [ "$RESTORE" -eq 1 ]; then
  echo "ERROR: --dry-run and --restore are mutually exclusive" >&2
  usage
fi

if [ "$DRY_RUN" -eq 0 ] && [ "$RESTORE" -eq 0 ]; then
  usage
fi

# ── Config ─────────────────────────────────────────────────────────────────────

load_config() {
  if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo "ERROR: .env not found at $SCRIPT_DIR/.env" >&2
    exit 1
  fi
  set -a
  # shellcheck source=/dev/null
  source "$SCRIPT_DIR/.env"
  set +a

  DB_TYPE="${DB_TYPE,,}"
  S3_PREFIX="${S3_PREFIX%/}/"
  [ "$S3_PREFIX" = "/" ] && S3_PREFIX=""
}

# ── S3 helper ──────────────────────────────────────────────────────────────────

s3_cmd() {
  if [ -n "${S3_ENDPOINT_URL:-}" ]; then
    aws --endpoint-url "$S3_ENDPOINT_URL" "$@"
  else
    aws "$@"
  fi
}

# ── Download from S3 ───────────────────────────────────────────────────────────

download_from_s3() {
  local s3_key="$1"
  local dest_dir="$2"

  # Normalise: ensure key ends with the compressed extension, not .sha256
  local base_key="${s3_key%.sha256}"

  local enc_file="$dest_dir/$(basename "$base_key")"
  local sha_file="${enc_file}.sha256"

  log INFO "Downloading s3://$S3_BUCKET/$base_key"
  s3_cmd s3 cp "s3://$S3_BUCKET/$base_key" "$enc_file"

  log INFO "Downloading s3://$S3_BUCKET/${base_key}.sha256"
  s3_cmd s3 cp "s3://$S3_BUCKET/${base_key}.sha256" "$sha_file"

  echo "$enc_file"
}

# ── Checksum verification ──────────────────────────────────────────────────────

verify_checksum() {
  local backup_file="$1"
  local sha_file="${backup_file}.sha256"

  if [ ! -f "$sha_file" ]; then
    log ERROR "Checksum sidecar not found: $sha_file"
    exit 1
  fi

  log INFO "Verifying checksum…"
  # sha256sum -c expects lines like: "<hash>  <filename>"
  # The sidecar was written from the same directory, so cd there first.
  local dir file
  dir="$(dirname "$backup_file")"
  file="$(basename "$backup_file")"

  if (cd "$dir" && sha256sum -c "${file}.sha256" --status); then
    local hash
    hash=$(awk '{print $1}' "$sha_file")
    log INFO "Checksum OK: $hash"
  else
    log ERROR "Checksum MISMATCH — the backup may be corrupted or tampered with."
    exit 1
  fi
}

# ── Decompress ─────────────────────────────────────────────────────────────────

decompress() {
  local path="$1"
  local sql_path

  if [[ "$path" == *.gz ]]; then
    sql_path="${path%.gz}"
    log INFO "Decompressing gzip…"
    gunzip -k "$path"   # -k keeps the original
  elif [[ "$path" == *.zst ]]; then
    if ! command -v zstd >/dev/null 2>&1; then
      log ERROR "zstd is not installed but the backup uses zstd compression."
      exit 1
    fi
    sql_path="${path%.zst}"
    log INFO "Decompressing zstd…"
    zstd -d -q "$path" -o "$sql_path"
  else
    log ERROR "Unknown compression format: $(basename "$path")"
    exit 1
  fi

  log INFO "Decompressed: $(basename "$sql_path")"
  echo "$sql_path"
}

# ── Restore to DB ──────────────────────────────────────────────────────────────

restore_to_db() {
  local sql_path="$1"
  local db_type="${DB_TYPE,,}"

  log INFO "Restoring '$(basename "$sql_path")' to $db_type database '$DB_NAME'…"

  if [ "$db_type" = "postgres" ]; then
    PGPASSWORD="$DB_PASSWORD" psql \
      -h "$DB_HOST" \
      -p "$DB_PORT" \
      -U "$DB_USER" \
      -d "$DB_NAME" \
      -f "$sql_path"
  else
    mysql \
      -h"$DB_HOST" \
      -P"$DB_PORT" \
      -u"$DB_USER" \
      -p"$DB_PASSWORD" \
      "$DB_NAME" < "$sql_path"
  fi

  log INFO "Restore complete."
}

# ── Main ───────────────────────────────────────────────────────────────────────

load_config

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

if [ "$FROM_S3" -eq 1 ]; then
  backup_file=$(download_from_s3 "$TARGET" "$TMP_DIR")
else
  backup_file="$TARGET"
  if [ ! -f "$backup_file" ]; then
    log ERROR "File not found: $backup_file"
    exit 1
  fi
  sha_file="${backup_file}.sha256"
  if [ ! -f "$sha_file" ]; then
    log ERROR "Checksum sidecar not found: $sha_file"
    exit 1
  fi
  # Copy to tmp for a clean working area
  cp "$backup_file" "$TMP_DIR/"
  cp "$sha_file" "$TMP_DIR/"
  backup_file="$TMP_DIR/$(basename "$backup_file")"
fi

verify_checksum "$backup_file"
sql_file=$(decompress "$backup_file")

if [ "$DRY_RUN" -eq 1 ]; then
  local_size_mb=$(echo "scale=2; $(stat -f%z "$sql_file" 2>/dev/null || stat -c%s "$sql_file") / 1048576" | bc)
  log INFO "Dry-run complete. Backup is valid. SQL size: ${local_size_mb} MB"
  log INFO "No changes made to the database."
  exit 0
fi

# Full restore
log WARN "About to restore into database '$DB_NAME' on $DB_HOST. This may overwrite data."
printf "Type 'yes' to proceed: "
read -r confirm
if [ "$confirm" != "yes" ]; then
  log INFO "Aborted."
  exit 0
fi

restore_to_db "$sql_file"
