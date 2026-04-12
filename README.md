# db-backup

Production-grade database backup to S3-compatible object storage. Runs on any Linux VPS via cron.

**Pipeline:** `dump → compress → checksum → upload → retention → cleanup`

---

## Features

- **Multi-database** — PostgreSQL, MySQL, MariaDB
- **Any S3-compatible backend** — AWS S3, Backblaze B2, Cloudflare R2, Wasabi, DigitalOcean Spaces, MinIO
- **SHA-256 integrity sidecar** — detect corruption or tampering before restore
- **Compression** — gzip (default) or zstd
- **Retention policy** — auto-delete backups older than N days from S3
- **Failure alerting** — Slack/Discord webhook or SMTP email
- **Pre-flight checks** — validates DB and S3 connectivity before doing any work
- **Rotating logs** — 10 MB × 5 rotations in `logs/backup.log`
- **Restore script** — `--dry-run` to verify integrity without touching the database

---

## Requirements

- Python 3.9+
- A Python virtual environment (`.venv`) with the following packages:
  - `boto3` — S3 operations
  - `python-dotenv` — `.env` loading
  - `zstandard` — only if `COMPRESSION_ALGO=zstd`
  - `cryptography` — reserved for future encryption support
- `pg_dump` / `psql` (PostgreSQL) or `mysqldump` / `mysql` (MySQL/MariaDB)
- A bucket on any S3-compatible service

---

## Quick Start

```bash
git clone <repo> db-backup
cd db-backup

# Create virtual environment and install dependencies
python3 -m venv .venv
.venv/bin/pip install boto3 python-dotenv zstandard cryptography

# Check dependencies + register daily cron job
./install.sh

# Edit credentials
cp .env.example .env
nano .env

# Run manually to test
.venv/bin/python backup.py
```

---

## Installation

`install.sh` does four things:

1. Makes `backup.py` and `restore.py` executable
2. Checks that Python 3 and the `.venv` are present, and that DB client tools are installed
3. Copies `.env.example` → `.env` if no `.env` exists yet
4. Registers a daily cron job with a **randomized minute** (avoids load spikes across servers)

Default schedule: **daily at 02:XX UTC**. Edit the `CRON_HOUR` variable in `install.sh` to change the hour.

---

## Configuration

All configuration lives in `.env`. Copy the example and fill in your values:

```bash
cp .env.example .env
```

### Database

| Variable | Description |
|---|---|
| `DB_TYPE` | `postgres`, `mysql`, or `mariadb` |
| `DB_HOST` | Database host (default: `127.0.0.1`) |
| `DB_PORT` | Database port (default: `5432` for postgres, `3306` for mysql) |
| `DB_NAME` | Database name to back up |
| `DB_USER` | Backup user (use a read-only account — see below) |
| `DB_PASSWORD` | Backup user password |

### S3-Compatible Storage

| Variable | Description |
|---|---|
| `S3_ENDPOINT_URL` | Custom endpoint for non-AWS providers. Leave empty for native AWS S3. |
| `S3_BUCKET` | Bucket name |
| `S3_PREFIX` | Key prefix, e.g. `vps-hostname/` — useful when one bucket holds multiple servers |
| `AWS_ACCESS_KEY_ID` | Access key |
| `AWS_SECRET_ACCESS_KEY` | Secret key |
| `AWS_DEFAULT_REGION` | Region. Use `auto` for Cloudflare R2 and other non-AWS providers. |

**Provider endpoint examples:**

| Provider | `S3_ENDPOINT_URL` | `AWS_DEFAULT_REGION` |
|---|---|---|
| AWS S3 | *(leave empty)* | e.g. `us-east-1` |
| Backblaze B2 | `https://s3.us-west-004.backblazeb2.com` | `auto` |
| Cloudflare R2 | `https://<account_id>.r2.cloudflarestorage.com` | `auto` |
| DigitalOcean Spaces | `https://<region>.digitaloceanspaces.com` | `auto` |
| Wasabi | `https://s3.wasabisys.com` | `auto` |
| MinIO (self-hosted) | `http://localhost:9000` | `auto` |

### Retention

| Variable | Default | Description |
|---|---|---|
| `RETENTION_DAYS` | `30` | Backups older than this are deleted from S3 automatically |

### Compression

| Variable | Default | Description |
|---|---|---|
| `COMPRESSION_ALGO` | `gzip` | `gzip` (widely supported) or `zstd` (faster, better ratio) |

### Alerting

Configure at least one method to be notified of failures:

| Variable | Description |
|---|---|
| `ALERT_WEBHOOK_URL` | Slack or Discord incoming webhook URL |
| `ALERT_SMTP_HOST` | SMTP server hostname |
| `ALERT_SMTP_PORT` | SMTP port (default: `587`) |
| `ALERT_SMTP_USER` | SMTP username |
| `ALERT_SMTP_PASSWORD` | SMTP password |
| `ALERT_SMTP_FROM` | Sender address |
| `ALERT_SMTP_TO` | Recipient address |

---

## Backup File Naming

Each run produces two files in S3:

```
{prefix}{db_name}_{YYYY-MM-DD_HH-MM-SS}.sql.gz       # compressed backup
{prefix}{db_name}_{YYYY-MM-DD_HH-MM-SS}.sql.gz.sha256 # integrity checksum
```

Example:
```
vps-hostname/mydb_2024-01-15_02-37-00.sql.gz
vps-hostname/mydb_2024-01-15_02-37-00.sql.gz.sha256
```

---

## Restore & Verification

### Verify integrity (no DB changes)

Use this regularly as a restore drill — it decompresses the backup without touching the database.

```bash
# Local file
.venv/bin/python restore.py --dry-run /path/to/backup.sql.gz

# Directly from S3
.venv/bin/python restore.py --dry-run --s3 vps-hostname/mydb_2024-01-15_02-37-00.sql.gz
```

### Full restore to database

```bash
# From a local file (will prompt for confirmation)
.venv/bin/python restore.py --restore /path/to/backup.sql.gz

# From S3
.venv/bin/python restore.py --restore --s3 vps-hostname/mydb_2024-01-15_02-37-00.sql.gz
```

> **Warning:** `--restore` imports SQL into the live database. Ensure you have a target database ready and understand the impact before proceeding. The script will ask you to type `yes` to confirm.

---

## Least-Privilege Setup

### Database backup user

**PostgreSQL:**
```sql
CREATE USER backup_user WITH PASSWORD 'strong-password';
GRANT CONNECT ON DATABASE mydb TO backup_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_user;
```

**MySQL / MariaDB:**
```sql
CREATE USER 'backup_user'@'localhost' IDENTIFIED BY 'strong-password';
GRANT SELECT, LOCK TABLES, SHOW VIEW, EVENT, TRIGGER ON mydb.* TO 'backup_user'@'localhost';
FLUSH PRIVILEGES;
```

### S3 bucket policy

Restrict the API key to only what the backup script needs. Apply this policy to your IAM user or API token:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:ListBucket"],
      "Resource": "arn:aws:s3:::my-db-backups",
      "Condition": {
        "StringLike": {"s3:prefix": ["vps-hostname/*"]}
      }
    },
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject", "s3:GetObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::my-db-backups/vps-hostname/*"
    }
  ]
}
```

---

## File Structure

```
db-backup/
├── backup.py          # backup orchestrator
├── restore.py         # restore + integrity verification
├── lib.py             # shared: config, logging, S3 client, alerting
├── install.sh         # one-shot setup: deps check, cron
├── .env.example       # config template (copy to .env)
├── .gitignore
├── logs/              # rotating logs (auto-created)
└── tmp/               # ephemeral staging (auto-cleaned)
```

---

## Logs

Logs are written to `logs/backup.log` (rotating, max 10 MB × 5 files) and to stdout.

```bash
tail -f logs/backup.log
```

Example output:
```
2024-01-15T02:37:00Z [INFO] === db-backup starting ===
2024-01-15T02:37:00Z [INFO] Config loaded. DB_TYPE=postgres DB_NAME=mydb ...
2024-01-15T02:37:00Z [INFO] Checking connectivity…
2024-01-15T02:37:00Z [INFO] DB reachable at 127.0.0.1:5432
2024-01-15T02:37:00Z [INFO] S3 bucket 'my-db-backups' is accessible.
2024-01-15T02:37:01Z [INFO] Running postgres dump for database 'mydb'…
2024-01-15T02:37:03Z [INFO] Dump complete: mydb.sql (45.23 MB)
2024-01-15T02:37:05Z [INFO] Compressed: mydb_2024-01-15_02-37-00.sql.gz (8.71 MB)
2024-01-15T02:37:05Z [INFO] SHA-256: a3f1c2...
2024-01-15T02:37:07Z [INFO] Uploaded: vps-hostname/mydb_2024-01-15_02-37-00.sql.gz
2024-01-15T02:37:07Z [INFO] Uploaded: vps-hostname/mydb_2024-01-15_02-37-00.sql.gz.sha256
2024-01-15T02:37:07Z [INFO] Applying retention policy (30 days)…
2024-01-15T02:37:08Z [INFO] === db-backup completed successfully ===
```

---

## Security Notes

- The `.env` file contains sensitive credentials. It is excluded from git via `.gitignore`. Set permissions: `chmod 600 .env`.
- Use a dedicated read-only database user for dumps. Never use the root/admin account.
- Use a dedicated S3 API key scoped to the backup bucket and prefix only.
