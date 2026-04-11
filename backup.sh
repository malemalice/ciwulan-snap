#!/usr/bin/env bash
# db-backup/backup.sh
#
# Production-grade database backup script.
# Pipeline: dump → compress → checksum → upload → retention → cleanup
#
# Usage:
#   bash backup.sh
#
# All configuration is read from .env (see .env.example).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/logs"
LOG_FILE="$LOG_DIR/backup.log"
TMP_BASE="$SCRIPT_DIR/tmp"

# ── Logging ────────────────────────────────────────────────────────────────────

mkdir -p "$LOG_DIR"

_file_size() {
  stat -f%z "$1" 2>/dev/null || stat -c%s "$1" 2>/dev/null || echo 0
}

rotate_logs() {
  local max_bytes=$(( 10 * 1024 * 1024 ))
  [ ! -f "$LOG_FILE" ] && return
  local size
  size=$(_file_size "$LOG_FILE")
  if [ "$size" -gt "$max_bytes" ]; then
    for i in 4 3 2 1; do
      [ -f "${LOG_FILE}.$i" ] && mv "${LOG_FILE}.$i" "${LOG_FILE}.$((i+1))"
    done
    mv "$LOG_FILE" "${LOG_FILE}.1"
  fi
}

rotate_logs

log() {
  local level="$1"; shift
  local msg="$*"
  local ts
  ts=$(date -u +"%Y-%m-%dT%H:%M:%SZ")
  local line="$ts [$level] $msg"
  echo "$line"
  echo "$line" >> "$LOG_FILE"
}

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

  local required=(
    DB_TYPE DB_HOST DB_PORT DB_NAME DB_USER DB_PASSWORD
    S3_BUCKET AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
  )
  local missing=()
  for var in "${required[@]}"; do
    [ -z "${!var:-}" ] && missing+=("$var")
  done
  if [ ${#missing[@]} -gt 0 ]; then
    log ERROR "Missing required env vars: ${missing[*]}"
    exit 1
  fi

  DB_TYPE="${DB_TYPE,,}"  # lowercase
  if [[ ! "$DB_TYPE" =~ ^(postgres|mysql|mariadb)$ ]]; then
    log ERROR "DB_TYPE must be postgres, mysql, or mariadb. Got: $DB_TYPE"
    exit 1
  fi

  RETENTION_DAYS="${RETENTION_DAYS:-30}"
  COMPRESSION_ALGO="${COMPRESSION_ALGO:-gzip}"
  # Normalise prefix: strip trailing slashes then add one
  S3_PREFIX="${S3_PREFIX%/}/"
  # A prefix of "/" means no prefix — treat empty input as no prefix
  [ "$S3_PREFIX" = "/" ] && S3_PREFIX=""

  log INFO "Config loaded. DB_TYPE=$DB_TYPE DB_NAME=$DB_NAME S3_BUCKET=$S3_BUCKET PREFIX=${S3_PREFIX:-<none>}"
}

# ── S3 helper ──────────────────────────────────────────────────────────────────

s3_cmd() {
  if [ -n "${S3_ENDPOINT_URL:-}" ]; then
    aws --endpoint-url "$S3_ENDPOINT_URL" "$@"
  else
    aws "$@"
  fi
}

# ── Pre-flight checks ──────────────────────────────────────────────────────────

check_connectivity() {
  log INFO "Checking connectivity…"

  if timeout 10 bash -c "echo > /dev/tcp/$DB_HOST/$DB_PORT" 2>/dev/null; then
    log INFO "DB reachable at $DB_HOST:$DB_PORT"
  else
    log ERROR "Cannot reach DB at $DB_HOST:$DB_PORT"
    exit 1
  fi

  if s3_cmd s3api head-bucket --bucket "$S3_BUCKET" 2>/dev/null; then
    log INFO "S3 bucket '$S3_BUCKET' is accessible."
  else
    log ERROR "Cannot access S3 bucket '$S3_BUCKET'"
    exit 1
  fi
}

# ── Dump ───────────────────────────────────────────────────────────────────────

run_dump() {
  local tmp_dir="$1"
  local dump_path="$tmp_dir/$DB_NAME.sql"

  log INFO "Running $DB_TYPE dump for database '$DB_NAME'…"

  if [ "$DB_TYPE" = "postgres" ]; then
    PGPASSWORD="$DB_PASSWORD" pg_dump \
      -h "$DB_HOST" \
      -p "$DB_PORT" \
      -U "$DB_USER" \
      --no-tablespaces \
      --no-owner \
      --no-privileges \
      -F p \
      -f "$dump_path" \
      "$DB_NAME"
  else
    mysqldump \
      -h"$DB_HOST" \
      -P"$DB_PORT" \
      -u"$DB_USER" \
      -p"$DB_PASSWORD" \
      --single-transaction \
      --routines \
      --triggers \
      --result-file="$dump_path" \
      "$DB_NAME"
  fi

  local size_mb
  size_mb=$(echo "scale=2; $(_file_size "$dump_path") / 1048576" | bc)
  log INFO "Dump complete: $DB_NAME.sql (${size_mb} MB)"
  echo "$dump_path"
}

# ── Compress ───────────────────────────────────────────────────────────────────

compress() {
  local path="$1"
  local out_path

  if [ "$COMPRESSION_ALGO" = "zstd" ]; then
    if ! command -v zstd >/dev/null 2>&1; then
      log ERROR "zstd is not installed. Install it or set COMPRESSION_ALGO=gzip."
      exit 1
    fi
    out_path="${path}.zst"
    log INFO "Compressing with zstd…"
    zstd -3 -q "$path" -o "$out_path"
    rm "$path"
  else
    out_path="${path}.gz"
    log INFO "Compressing with gzip…"
    gzip -6 "$path"
    # gzip replaces the file in-place
  fi

  local size_mb
  size_mb=$(echo "scale=2; $(_file_size "$out_path") / 1048576" | bc)
  log INFO "Compressed: $(basename "$out_path") (${size_mb} MB)"
  echo "$out_path"
}

# ── Checksum ───────────────────────────────────────────────────────────────────

compute_checksum() {
  local path="$1"
  local sha_path="${path}.sha256"
  sha256sum "$path" > "$sha_path"
  local hash
  hash=$(awk '{print $1}' "$sha_path")
  log INFO "SHA-256: $hash"
  echo "$sha_path"
}

# ── Upload ─────────────────────────────────────────────────────────────────────

upload_to_s3() {
  local compressed_path="$1"
  local sha_path="$2"

  for local_path in "$compressed_path" "$sha_path"; do
    local key="${S3_PREFIX}$(basename "$local_path")"
    log INFO "Uploading s3://$S3_BUCKET/$key…"
    s3_cmd s3 cp "$local_path" "s3://$S3_BUCKET/$key"
    log INFO "Uploaded: $key"
  done
}

# ── Retention ──────────────────────────────────────────────────────────────────

apply_retention() {
  log INFO "Applying retention policy ($RETENTION_DAYS days)…"

  local now_epoch
  now_epoch=$(date +%s)
  local cutoff_epoch=$(( now_epoch - RETENTION_DAYS * 86400 ))

  local response
  response=$(s3_cmd s3api list-objects-v2 \
    --bucket "$S3_BUCKET" \
    ${S3_PREFIX:+--prefix "$S3_PREFIX"} \
    --output json 2>/dev/null || echo '{}')

  local deleted=0

  while IFS=$'\t' read -r last_modified key; do
    [ -z "$key" ] && continue

    # Parse ISO8601 date to epoch (handle both Linux and macOS date)
    local obj_epoch
    if date --version >/dev/null 2>&1; then
      # GNU date (Linux)
      obj_epoch=$(date -d "$last_modified" +%s 2>/dev/null || echo 0)
    else
      # BSD date (macOS)
      obj_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%S+00:00" "$last_modified" +%s 2>/dev/null || \
                  date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_modified" +%s 2>/dev/null || echo 0)
    fi

    local age_days=$(( (now_epoch - obj_epoch) / 86400 ))

    if [ "$obj_epoch" -gt 0 ] && [ "$age_days" -gt "$RETENTION_DAYS" ]; then
      log INFO "Deleting (age=${age_days}d): $key"
      s3_cmd s3api delete-object --bucket "$S3_BUCKET" --key "$key"
      deleted=$(( deleted + 1 ))
    fi
  done < <(echo "$response" | jq -r '.Contents[]? | "\(.LastModified)\t\(.Key)"')

  if [ "$deleted" -gt 0 ]; then
    log INFO "Deleted $deleted expired object(s)."
  else
    log INFO "No expired backups found."
  fi
}

# ── Alerting ───────────────────────────────────────────────────────────────────

notify_failure() {
  local error_msg="$1"
  local hostname
  hostname=$(hostname)
  local message="[db-backup] FAILED for ${DB_NAME:-?} on $hostname: $error_msg"
  log ERROR "$message"

  if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
    local payload
    # Escape double quotes in message for JSON
    local escaped_msg="${message//\"/\\\"}"
    payload="{\"text\": \"$escaped_msg\"}"
    if curl -sS -X POST \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 10 \
        "$ALERT_WEBHOOK_URL" >/dev/null 2>&1; then
      log INFO "Failure alert sent to webhook."
    else
      log WARN "Webhook alert failed."
    fi
  fi

  if [ -n "${ALERT_SMTP_HOST:-}" ]; then
    local subject="[db-backup] FAILED — ${DB_NAME:-?}"
    local smtp_url="smtp://$ALERT_SMTP_HOST:${ALERT_SMTP_PORT:-587}"
    if printf "From: %s\r\nTo: %s\r\nSubject: %s\r\n\r\n%s\r\n" \
        "$ALERT_SMTP_FROM" "$ALERT_SMTP_TO" "$subject" "$message" | \
       curl -sS \
        --url "$smtp_url" \
        --ssl-reqd \
        --user "$ALERT_SMTP_USER:$ALERT_SMTP_PASSWORD" \
        --mail-from "$ALERT_SMTP_FROM" \
        --mail-rcpt "$ALERT_SMTP_TO" \
        --upload-file - \
        --max-time 30 >/dev/null 2>&1; then
      log INFO "Failure alert sent via SMTP."
    else
      log WARN "SMTP alert failed."
    fi
  fi
}

# ── Main ───────────────────────────────────────────────────────────────────────

main() {
  log INFO "=== db-backup starting ==="

  local timestamp
  timestamp=$(date -u +"%Y%m%dT%H%M%SZ")
  local TMP_RUN_DIR="$TMP_BASE/$timestamp"

  # Cleanup tmp dir on exit (success or failure)
  trap 'rm -rf "$TMP_RUN_DIR"' EXIT

  local exit_code=0

  load_config

  check_connectivity

  mkdir -p "$TMP_RUN_DIR"

  local dump_path compressed_path sha_path

  if ! dump_path=$(run_dump "$TMP_RUN_DIR") || \
     ! compressed_path=$(compress "$dump_path") || \
     ! sha_path=$(compute_checksum "$compressed_path"); then
    notify_failure "Backup pipeline failed (dump/compress/checksum step)"
    exit 1
  fi

  # Rename files to include timestamp in the final name
  # compressed_path is like: tmp/.../mydb.sql.gz  →  tmp/.../mydb_2024-01-15_02-37-00.sql.gz
  local ext="${compressed_path#*"$DB_NAME.sql"}"  # .gz or .zst
  local final_compressed="$TMP_RUN_DIR/${DB_NAME}_$(date -u +"%Y-%m-%d_%H-%M-%S").sql${ext}"
  mv "$compressed_path" "$final_compressed"
  mv "$sha_path" "${final_compressed}.sha256"
  sha_path="${final_compressed}.sha256"
  compressed_path="$final_compressed"

  if ! upload_to_s3 "$compressed_path" "$sha_path"; then
    notify_failure "S3 upload failed"
    exit 1
  fi

  if ! apply_retention; then
    log WARN "Retention policy failed (non-fatal)."
  fi

  log INFO "=== db-backup completed successfully ==="
}

# Wrap main so we can catch errors and send alerts
if ! main "$@"; then
  exit 1
fi
