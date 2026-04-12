#!/usr/bin/env python3
"""
db-backup/backup.py

Production-grade database backup script.
Pipeline: dump → compress → checksum → upload → retention → cleanup

Usage:
    .venv/bin/python backup.py

All configuration is read from .env (see .env.example).
"""

import gzip
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import botocore.exceptions

import lib

SCRIPT_DIR = lib.SCRIPT_DIR


# ── Dump ──────────────────────────────────────────────────────────────────────

def _find_pg_dump(config: dict, logger) -> str:
    """Return a pg_dump binary compatible with the remote server.

    Resolution order:
      1. PGDUMP_PATH env var (explicit override)
      2. Auto-detect: query server major version via psql, then search
         versioned pg_dump paths installed by Homebrew / apt / yum.
      3. Fall back to whatever `pg_dump` is on PATH.
    """
    if config.get("pgdump_path"):
        if not Path(config["pgdump_path"]).is_file():
            raise RuntimeError(
                f"PGDUMP_PATH '{config['pgdump_path']}' does not exist or is not a file."
            )
        return config["pgdump_path"]

    env = {**os.environ, "PGPASSWORD": config["db_password"]}
    try:
        result = subprocess.run(
            [
                "psql",
                "-h", config["db_host"],
                "-p", str(config["db_port"]),
                "-U", config["db_user"],
                "-d", config["db_name"],
                "-t", "-A", "-c", "SHOW server_version_num;",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            major = int(result.stdout.strip()) // 10_000
            candidates = [
                f"/opt/homebrew/opt/postgresql@{major}/bin/pg_dump",  # macOS Homebrew (Apple Silicon)
                f"/usr/local/opt/postgresql@{major}/bin/pg_dump",      # macOS Homebrew (Intel)
                f"/usr/lib/postgresql/{major}/bin/pg_dump",             # Debian/Ubuntu
                f"/usr/pgsql-{major}/bin/pg_dump",                      # RHEL/CentOS
            ]
            for path in candidates:
                if Path(path).is_file():
                    logger.info("Auto-selected pg_dump for PostgreSQL %d: %s", major, path)
                    return path
            logger.warning(
                "Server is PostgreSQL %d but no matching pg_dump found in common paths; "
                "using default. Set PGDUMP_PATH in .env to override.",
                major,
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("Server version detection failed (%s); using default pg_dump.", exc)

    return "pg_dump"


def run_dump(config: dict, tmp_dir: Path, logger) -> Path:
    dump_path = tmp_dir / f"{config['db_name']}.sql"
    db_type = config["db_type"]

    logger.info("Running %s dump for database '%s'…", db_type, config["db_name"])

    if db_type == "postgres":
        cmd = [
            _find_pg_dump(config, logger),
            "-h", config["db_host"],
            "-p", str(config["db_port"]),
            "-U", config["db_user"],
            "--no-tablespaces",
            "--no-owner",
            "--no-privileges",
            "-F", "p",
            "-f", str(dump_path),
            config["db_name"],
        ]
        env = {**os.environ, "PGPASSWORD": config["db_password"]}
    else:
        cmd = [
            config.get("mysqldump_path") or "mysqldump",
            f"-h{config['db_host']}",
            f"-P{config['db_port']}",
            f"-u{config['db_user']}",
            f"-p{config['db_password']}",
            "--single-transaction",
            "--routines",
            "--triggers",
            f"--result-file={dump_path}",
            config["db_name"],
        ]
        env = os.environ.copy()

    try:
        result = subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"pg_dump/mysqldump failed:\n{exc.stderr.strip()}") from exc

    size_mb = dump_path.stat().st_size / 1_048_576
    logger.info("Dump complete: %s.sql (%.2f MB)", config["db_name"], size_mb)
    return dump_path


# ── Compress ──────────────────────────────────────────────────────────────────

def compress(path: Path, algo: str, logger) -> Path:
    if algo == "zstd":
        try:
            import zstandard as zstd  # noqa: PLC0415
        except ImportError:
            raise RuntimeError("zstandard package not installed. Install it or set COMPRESSION_ALGO=gzip.")

        out_path = path.with_suffix(path.suffix + ".zst")
        logger.info("Compressing with zstd…")
        cctx = zstd.ZstdCompressor(level=3)
        with path.open("rb") as src, out_path.open("wb") as dst:
            cctx.copy_stream(src, dst)
        path.unlink()
    else:
        out_path = path.with_suffix(path.suffix + ".gz")
        logger.info("Compressing with gzip…")
        with path.open("rb") as src, gzip.open(out_path, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        path.unlink()

    size_mb = out_path.stat().st_size / 1_048_576
    logger.info("Compressed: %s (%.2f MB)", out_path.name, size_mb)
    return out_path


# ── Checksum ──────────────────────────────────────────────────────────────────

def compute_checksum(path: Path, logger) -> Path:
    sha256 = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    digest = sha256.hexdigest()

    # Format matches `sha256sum` output: "{hash}  {basename}\n" (two spaces)
    sha_path = path.parent / (path.name + ".sha256")
    sha_path.write_text(f"{digest}  {path.name}\n")

    logger.info("SHA-256: %s", digest)
    return sha_path


# ── Upload ────────────────────────────────────────────────────────────────────

def upload_to_s3(s3, config: dict, compressed_path: Path, sha_path: Path, logger) -> None:
    for local_path in (compressed_path, sha_path):
        key = config["s3_prefix"] + local_path.name
        logger.info("Uploading s3://%s/%s…", config["s3_bucket"], key)
        s3.upload_file(str(local_path), config["s3_bucket"], key)
        logger.info("Uploaded: %s", key)


# ── Retention ─────────────────────────────────────────────────────────────────

def apply_retention(s3, config: dict, logger) -> None:
    retention_days = config["retention_days"]
    logger.info("Applying retention policy (%d days)…", retention_days)

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0

    paginator = s3.get_paginator("list_objects_v2")
    page_kwargs = {"Bucket": config["s3_bucket"]}
    if config["s3_prefix"]:
        page_kwargs["Prefix"] = config["s3_prefix"]

    for page in paginator.paginate(**page_kwargs):
        for obj in page.get("Contents", []):
            last_modified = obj["LastModified"]  # already a timezone-aware datetime
            age_days = (datetime.now(timezone.utc) - last_modified).days
            if last_modified < cutoff:
                logger.info("Deleting (age=%dd): %s", age_days, obj["Key"])
                s3.delete_object(Bucket=config["s3_bucket"], Key=obj["Key"])
                deleted += 1

    if deleted:
        logger.info("Deleted %d expired object(s).", deleted)
    else:
        logger.info("No expired backups found.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    logger = lib.get_logger("backup")
    logger.info("=== db-backup starting ===")

    try:
        config = lib.load_config()
    except lib.ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    logger.info(
        "Config loaded. DB_TYPE=%s DB_NAME=%s S3_BUCKET=%s PREFIX=%s",
        config["db_type"], config["db_name"], config["s3_bucket"],
        config["s3_prefix"] or "<none>",
    )

    s3 = lib.get_s3_client(config)

    logger.info("Checking connectivity…")
    try:
        lib.check_db_connectivity(config, logger)
        lib.check_s3_connectivity(s3, config, logger)
    except RuntimeError as exc:
        logger.error("%s", exc)
        lib.notify_failure(config, logger, str(exc))
        sys.exit(1)

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    tmp_base = SCRIPT_DIR / "tmp"
    tmp_base.mkdir(exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=tmp_base))

    try:
        # Determine final filename extension before compressing
        ext = ".zst" if config["compression_algo"] == "zstd" else ".gz"
        final_name = f"{config['db_name']}_{timestamp}.sql{ext}"

        dump_path = run_dump(config, tmp_dir, logger)
        compressed_path = compress(dump_path, config["compression_algo"], logger)

        # Rename to timestamped final name
        final_path = tmp_dir / final_name
        compressed_path.rename(final_path)
        compressed_path = final_path

        sha_path = compute_checksum(compressed_path, logger)
        upload_to_s3(s3, config, compressed_path, sha_path, logger)

    except Exception as exc:
        logger.error("Backup pipeline failed: %s", exc)
        lib.notify_failure(config, logger, str(exc))
        sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    try:
        apply_retention(s3, config, logger)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Retention policy failed (non-fatal): %s", exc)

    logger.info("=== db-backup completed successfully ===")


if __name__ == "__main__":
    main()
