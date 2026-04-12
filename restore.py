#!/usr/bin/env python3
"""
db-backup/restore.py

Restore and integrity verification for database backups.

Modes:
  --dry-run   Verify checksum and decompress without touching the database.
  --restore   Full restore: checksum → decompress → import to DB.

Source:
  <file>          Local backup file path.
  --s3 <key>      Download from S3 using key (e.g. metallica/mydb_2024-01-15.sql.gz).

Usage:
    .venv/bin/python restore.py --dry-run /path/to/backup.sql.gz
    .venv/bin/python restore.py --dry-run --s3 metallica/mydb_2024-01-15_02-37-00.sql.gz
    .venv/bin/python restore.py --restore --s3 metallica/mydb_2024-01-15_02-37-00.sql.gz
"""

import argparse
import gzip
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import lib

SCRIPT_DIR = lib.SCRIPT_DIR


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore or verify a db-backup archive.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Verify integrity only — does not touch the database.",
    )
    mode.add_argument(
        "--restore",
        action="store_true",
        help="Restore the backup into the configured database.",
    )
    parser.add_argument(
        "--s3",
        action="store_true",
        help="Treat TARGET as an S3 key rather than a local file path.",
    )
    parser.add_argument(
        "target",
        help="Local file path or S3 key of the backup file.",
    )
    return parser.parse_args()


# ── S3 download ───────────────────────────────────────────────────────────────

def download_from_s3(s3, config: dict, s3_key: str, dest_dir: Path, logger) -> Path:
    # Strip .sha256 suffix if user accidentally passed the sidecar key
    if s3_key.endswith(".sha256"):
        s3_key = s3_key[: -len(".sha256")]

    backup_filename = Path(s3_key).name
    sha_filename = backup_filename + ".sha256"

    local_backup = dest_dir / backup_filename
    local_sha = dest_dir / sha_filename

    for s3_file, local_file in ((s3_key, local_backup), (s3_key + ".sha256", local_sha)):
        logger.info("Downloading s3://%s/%s…", config["s3_bucket"], s3_file)
        s3.download_file(config["s3_bucket"], s3_file, str(local_file))
        logger.info("Downloaded: %s", local_file.name)

    return local_backup


# ── Checksum verification ─────────────────────────────────────────────────────

def verify_checksum(backup_file: Path, logger) -> None:
    sha_file = Path(str(backup_file) + ".sha256")
    if not sha_file.exists():
        raise RuntimeError(f"Checksum sidecar not found: {sha_file}")

    # Read expected hash from sidecar (format: "{hash}  {filename}\n")
    sidecar_content = sha_file.read_text().strip()
    expected_hash = sidecar_content.split()[0]

    # Compute actual hash
    sha256 = hashlib.sha256()
    with backup_file.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    actual_hash = sha256.hexdigest()

    if actual_hash != expected_hash:
        raise RuntimeError(
            f"Checksum MISMATCH for {backup_file.name}\n"
            f"  expected: {expected_hash}\n"
            f"  actual:   {actual_hash}"
        )

    logger.info("SHA-256 verified: %s", actual_hash)


# ── Decompress ────────────────────────────────────────────────────────────────

def decompress(path: Path, logger) -> Path:
    if path.suffix == ".zst":
        try:
            import zstandard as zstd  # noqa: PLC0415
        except ImportError:
            raise RuntimeError("zstandard package not installed. Install it or set COMPRESSION_ALGO=gzip.")

        out_path = path.with_suffix("")  # strip .zst → .sql.gz or .sql
        logger.info("Decompressing with zstd…")
        dctx = zstd.ZstdDecompressor()
        with path.open("rb") as src, out_path.open("wb") as dst:
            dctx.copy_stream(src, dst)

    elif path.suffix == ".gz":
        out_path = path.with_suffix("")  # strip .gz → .sql
        logger.info("Decompressing with gzip…")
        with gzip.open(path, "rb") as src, out_path.open("wb") as dst:
            shutil.copyfileobj(src, dst)

    else:
        raise RuntimeError(f"Unrecognised backup extension: {path.suffix}")

    size_mb = out_path.stat().st_size / 1_048_576
    logger.info("Decompressed: %s (%.2f MB)", out_path.name, size_mb)
    return out_path


# ── DB restore ────────────────────────────────────────────────────────────────

def restore_to_db(config: dict, sql_path: Path, logger) -> None:
    logger.info("Restoring '%s' to %s database '%s'…", sql_path.name, config["db_type"], config["db_name"])

    if config["db_type"] == "postgres":
        import os
        cmd = [
            "psql",
            "-h", config["db_host"],
            "-p", str(config["db_port"]),
            "-U", config["db_user"],
            "-d", config["db_name"],
            "-f", str(sql_path),
        ]
        env = {**os.environ, "PGPASSWORD": config["db_password"]}
    else:
        cmd = [
            "mysql",
            f"-h{config['db_host']}",
            f"-P{config['db_port']}",
            f"-u{config['db_user']}",
            f"-p{config['db_password']}",
            config["db_name"],
        ]
        env = None  # mysql reads stdin

    with sql_path.open("rb") as stdin_file:
        subprocess.run(
            cmd,
            stdin=stdin_file if config["db_type"] != "postgres" else None,
            env=env,
            check=True,
        )

    logger.info("Restore complete.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    logger = lib.get_logger("restore")

    try:
        config = lib.load_config()
    except lib.ConfigError as exc:
        logger.error("%s", exc)
        sys.exit(1)

    s3 = lib.get_s3_client(config) if args.s3 else None

    tmp_base = SCRIPT_DIR / "tmp"
    tmp_base.mkdir(exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=tmp_base))

    try:
        if args.s3:
            backup_file = download_from_s3(s3, config, args.target, tmp_dir, logger)
        else:
            src = Path(args.target)
            if not src.exists():
                logger.error("File not found: %s", src)
                sys.exit(1)
            sha_src = Path(str(src) + ".sha256")
            if not sha_src.exists():
                logger.error("Checksum sidecar not found: %s", sha_src)
                sys.exit(1)
            # Copy to tmp so working files are co-located
            backup_file = tmp_dir / src.name
            shutil.copy2(src, backup_file)
            shutil.copy2(sha_src, tmp_dir / sha_src.name)

        verify_checksum(backup_file, logger)
        sql_path = decompress(backup_file, logger)

        if args.dry_run:
            size_mb = sql_path.stat().st_size / 1_048_576
            logger.info(
                "Dry-run complete. Backup '%s' is valid. SQL size: %.2f MB",
                backup_file.name, size_mb,
            )
            return

        # Full restore — require explicit confirmation
        print(f"\nWARNING: This will import '{backup_file.name}' into database '{config['db_name']}'.")
        print("This operation cannot be undone.\n")
        confirm = input("Type 'yes' to proceed: ").strip()
        if confirm != "yes":
            logger.info("Restore aborted by user.")
            sys.exit(0)

        restore_to_db(config, sql_path, logger)

    except Exception as exc:
        logger.error("%s", exc)
        sys.exit(1)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
