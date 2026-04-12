"""
db-backup/lib.py

Shared utilities for backup.py and restore.py:
  - Config loading (.env)
  - Logging (rotating file + stdout, UTC timestamps)
  - boto3 S3 client construction
  - DB and S3 connectivity pre-flight checks
  - Failure alerting (webhook + SMTP)
"""

import json
import logging
import logging.handlers
import os
import smtplib
import socket
import time
import urllib.request
from email.mime.text import MIMEText
from pathlib import Path

import boto3
import botocore.exceptions
from dotenv import load_dotenv

SCRIPT_DIR = Path(__file__).parent.resolve()


# ── Exceptions ────────────────────────────────────────────────────────────────

class ConfigError(Exception):
    pass


# ── Logging ───────────────────────────────────────────────────────────────────

def _utc_converter(*args):  # noqa: ANN002
    return time.gmtime()


def get_logger(name: str) -> logging.Logger:
    log_dir = SCRIPT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "backup.log"

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%SZ",
    )
    fmt.converter = _utc_converter

    # Rotating file handler: 10 MB × 5 rotations
    fh = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=10 * 1024 * 1024, backupCount=5
    )
    fh.setFormatter(fmt)

    # stdout handler
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(sh)

    return logger


# ── Config ────────────────────────────────────────────────────────────────────

def load_config() -> dict:
    env_path = SCRIPT_DIR / ".env"
    if not env_path.exists():
        raise ConfigError(f".env not found at {env_path}")
    load_dotenv(env_path)

    required = [
        "DB_TYPE", "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "S3_BUCKET", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        raise ConfigError(f"Missing required env vars: {', '.join(missing)}")

    db_type = os.environ["DB_TYPE"].lower()
    if db_type not in ("postgres", "mysql", "mariadb"):
        raise ConfigError(f"DB_TYPE must be postgres, mysql, or mariadb. Got: {db_type}")

    # Normalize S3 prefix: ensure single trailing slash; empty if blank
    raw_prefix = os.environ.get("S3_PREFIX", "").rstrip("/")
    s3_prefix = f"{raw_prefix}/" if raw_prefix else ""

    return {
        # Database
        "db_type": db_type,
        "db_host": os.environ["DB_HOST"],
        "db_port": int(os.environ["DB_PORT"]),
        "db_name": os.environ["DB_NAME"],
        "db_user": os.environ["DB_USER"],
        "db_password": os.environ["DB_PASSWORD"],
        # S3
        "s3_bucket": os.environ["S3_BUCKET"],
        "s3_prefix": s3_prefix,
        "s3_endpoint_url": os.environ.get("S3_ENDPOINT_URL") or None,
        "aws_access_key_id": os.environ["AWS_ACCESS_KEY_ID"],
        "aws_secret_access_key": os.environ["AWS_SECRET_ACCESS_KEY"],
        "aws_region": os.environ.get("AWS_DEFAULT_REGION") or "auto",
        # Backup settings
        "retention_days": int(os.environ.get("RETENTION_DAYS") or 30),
        "compression_algo": os.environ.get("COMPRESSION_ALGO") or "gzip",
        # Alerting
        "alert_webhook_url": os.environ.get("ALERT_WEBHOOK_URL") or None,
        "alert_smtp_host": os.environ.get("ALERT_SMTP_HOST") or None,
        "alert_smtp_port": int(os.environ.get("ALERT_SMTP_PORT") or 587),
        "alert_smtp_user": os.environ.get("ALERT_SMTP_USER") or None,
        "alert_smtp_password": os.environ.get("ALERT_SMTP_PASSWORD") or None,
        "alert_smtp_from": os.environ.get("ALERT_SMTP_FROM") or None,
        "alert_smtp_to": os.environ.get("ALERT_SMTP_TO") or None,
        # Encryption key (loaded but not yet used — placeholder for future AES-256-GCM step)
        "encryption_key_hex": os.environ.get("ENCRYPTION_KEY_HEX") or None,
        # Optional explicit path to pg_dump / mysqldump binaries
        "pgdump_path": os.environ.get("PGDUMP_PATH") or None,
        "mysqldump_path": os.environ.get("MYSQLDUMP_PATH") or None,
    }


# ── S3 client ─────────────────────────────────────────────────────────────────

def get_s3_client(config: dict):
    kwargs = {
        "region_name": config["aws_region"],
        "aws_access_key_id": config["aws_access_key_id"],
        "aws_secret_access_key": config["aws_secret_access_key"],
    }
    if config["s3_endpoint_url"]:
        kwargs["endpoint_url"] = config["s3_endpoint_url"]
    return boto3.client("s3", **kwargs)


# ── Pre-flight checks ─────────────────────────────────────────────────────────

def check_db_connectivity(config: dict, logger: logging.Logger) -> None:
    host = config["db_host"]
    port = config["db_port"]
    try:
        with socket.create_connection((host, port), timeout=10):
            pass
        logger.info("DB reachable at %s:%s", host, port)
    except OSError as exc:
        raise RuntimeError(f"Cannot reach DB at {host}:{port}") from exc


def check_s3_connectivity(s3, config: dict, logger: logging.Logger) -> None:
    bucket = config["s3_bucket"]
    try:
        s3.head_bucket(Bucket=bucket)
        logger.info("S3 bucket '%s' is accessible.", bucket)
    except botocore.exceptions.ClientError as exc:
        code = exc.response["Error"]["Code"]
        raise RuntimeError(
            f"Cannot access S3 bucket '{bucket}' (HTTP {code})"
        ) from exc


# ── Alerting ──────────────────────────────────────────────────────────────────

def notify_failure(config: dict, logger: logging.Logger, message: str) -> None:
    hostname = socket.gethostname()
    full_msg = f"[db-backup] FAILED for {config.get('db_name', '?')} on {hostname}: {message}"

    if config.get("alert_webhook_url"):
        try:
            payload = json.dumps({"text": full_msg}).encode()
            req = urllib.request.Request(
                config["alert_webhook_url"],
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10):
                pass
            logger.info("Failure alert sent to webhook.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Webhook alert failed: %s", exc)

    if config.get("alert_smtp_host"):
        try:
            msg = MIMEText(full_msg)
            msg["Subject"] = f"[db-backup] FAILED — {config.get('db_name', '?')}"
            msg["From"] = config["alert_smtp_from"]
            msg["To"] = config["alert_smtp_to"]

            with smtplib.SMTP(config["alert_smtp_host"], config["alert_smtp_port"], timeout=30) as server:
                server.starttls()
                server.login(config["alert_smtp_user"], config["alert_smtp_password"])
                server.sendmail(
                    config["alert_smtp_from"],
                    [config["alert_smtp_to"]],
                    msg.as_string(),
                )
            logger.info("Failure alert sent via SMTP.")
        except Exception as exc:  # noqa: BLE001
            logger.warning("SMTP alert failed: %s", exc)
