#!/usr/bin/env python3
"""
db-backup/pick.py

Interactive restore picker — lists available backups and lets you choose
one to dry-run or restore.  Works anywhere Python runs, including inside
a Docker container (no fzf or other extras required).

Sources:
  S3  (default) — lists objects under S3_PREFIX in the configured bucket.
  Local          — pass --local <dir> to list .sql.gz / .sql.zst files
                   in a local directory instead.

Usage:
    python pick.py                  # pick from S3
    python pick.py --local /backups # pick from a local directory
"""

import argparse
import sys
from pathlib import Path

import lib

SCRIPT_DIR = lib.SCRIPT_DIR

# Backup extensions we recognise
BACKUP_EXTS = (".sql.gz", ".sql.zst")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _prompt(msg: str) -> str:
    """Print prompt and read a line, handling EOF gracefully."""
    try:
        return input(msg).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)


# ── List backups ──────────────────────────────────────────────────────────────

def list_s3_backups(s3, config: dict) -> list[dict]:
    """Return list of dicts with keys: key, size, last_modified."""
    prefix = config["s3_prefix"]
    paginator = s3.get_paginator("list_objects_v2")
    items = []

    for page in paginator.paginate(Bucket=config["s3_bucket"], Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            # Skip checksum sidecars
            if key.endswith(".sha256"):
                continue
            # Only include recognised backup extensions
            if not any(key.endswith(ext) for ext in BACKUP_EXTS):
                continue
            items.append({
                "key": key,
                "size": obj["Size"],
                "last_modified": obj["LastModified"],
            })

    # Newest first
    items.sort(key=lambda x: x["last_modified"], reverse=True)
    return items


def list_local_backups(directory: str) -> list[dict]:
    """Return list of dicts with keys: path, size, last_modified."""
    d = Path(directory)
    if not d.is_dir():
        print(f"ERROR: directory not found: {directory}", file=sys.stderr)
        sys.exit(1)

    import datetime
    items = []
    for p in d.iterdir():
        if not any(p.name.endswith(ext) for ext in BACKUP_EXTS):
            continue
        sha = Path(str(p) + ".sha256")
        stat = p.stat()
        items.append({
            "path": p,
            "size": stat.st_size,
            "last_modified": datetime.datetime.fromtimestamp(stat.st_mtime),
            "has_checksum": sha.exists(),
        })

    items.sort(key=lambda x: x["last_modified"], reverse=True)
    return items


# ── Display ───────────────────────────────────────────────────────────────────

def display_s3_backups(items: list[dict]) -> None:
    if not items:
        print("No backups found in S3.")
        sys.exit(0)

    col_key = max(len(i["key"]) for i in items)
    col_key = max(col_key, 8)
    print(f"\n  {'#':>3}  {'Filename':<{col_key}}  {'Size':>8}  Last Modified")
    print(f"  {'─'*3}  {'─'*col_key}  {'─'*8}  {'─'*19}")
    for idx, item in enumerate(items, 1):
        ts = item["last_modified"].strftime("%Y-%m-%d %H:%M UTC")
        size = _human_size(item["size"])
        print(f"  {idx:>3}  {item['key']:<{col_key}}  {size:>8}  {ts}")
    print()


def display_local_backups(items: list[dict]) -> None:
    if not items:
        print("No backup files found in the specified directory.")
        sys.exit(0)

    col_name = max(len(i["path"].name) for i in items)
    col_name = max(col_name, 8)
    print(f"\n  {'#':>3}  {'Filename':<{col_name}}  {'Size':>8}  {'Checksum':^8}  Last Modified")
    print(f"  {'─'*3}  {'─'*col_name}  {'─'*8}  {'─'*8}  {'─'*19}")
    for idx, item in enumerate(items, 1):
        ts = item["last_modified"].strftime("%Y-%m-%d %H:%M")
        size = _human_size(item["size"])
        chk = "  ✓   " if item["has_checksum"] else "  ✗   "
        print(f"  {idx:>3}  {item['path'].name:<{col_name}}  {size:>8}  {chk}  {ts}")
    print()


# ── Interactive prompts ───────────────────────────────────────────────────────

def pick_item(count: int) -> int:
    """Ask user to choose a number; return 0-based index."""
    while True:
        raw = _prompt(f"Enter number [1-{count}] (or q to quit): ")
        if raw.lower() in ("q", "quit", "exit"):
            print("Aborted.")
            sys.exit(0)
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= count:
                return n - 1
        print(f"  Please enter a number between 1 and {count}.")


def pick_mode() -> str:
    """Ask user for dry-run or restore; return 'dry-run' or 'restore'."""
    print("  Mode:")
    print("    1) dry-run  — verify integrity only, no database changes")
    print("    2) restore  — full restore (DESTRUCTIVE)")
    print()
    while True:
        raw = _prompt("Choose mode [1/2] (or q to quit): ")
        if raw.lower() in ("q", "quit", "exit"):
            print("Aborted.")
            sys.exit(0)
        if raw == "1":
            return "dry-run"
        if raw == "2":
            return "restore"
        print("  Please enter 1 or 2.")


# ── Main ──────────────────────────────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Interactive backup picker for db-backup restore.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--local",
        metavar="DIR",
        help="List backups from a local directory instead of S3.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = lib.get_logger("pick")

    try:
        config = lib.load_config()
    except lib.ConfigError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    use_local = bool(args.local)

    # ── List available backups ────────────────────────────────────────────────
    if use_local:
        print(f"\nScanning local directory: {args.local}")
        items = list_local_backups(args.local)
        display_local_backups(items)
    else:
        print(f"\nFetching backup list from s3://{config['s3_bucket']}/{config['s3_prefix']} …")
        s3 = lib.get_s3_client(config)
        items = list_s3_backups(s3, config)
        display_s3_backups(items)

    # ── User picks a backup ───────────────────────────────────────────────────
    idx = pick_item(len(items))
    chosen = items[idx]

    if use_local:
        label = chosen["path"].name
        if not chosen["has_checksum"]:
            print(f"\nWARNING: No checksum sidecar found for '{label}'. Integrity check will fail.")
    else:
        label = chosen["key"]

    print(f"\nSelected: {label}")
    print()

    # ── User picks a mode ─────────────────────────────────────────────────────
    mode = pick_mode()
    print()

    # ── Delegate to restore.py logic ─────────────────────────────────────────
    import shutil
    import tempfile
    from restore import (
        decompress,
        download_from_s3,
        restore_to_db,
        verify_checksum,
    )
    from pathlib import Path as _Path

    tmp_base = SCRIPT_DIR / "tmp"
    tmp_base.mkdir(exist_ok=True)
    tmp_dir = _Path(tempfile.mkdtemp(dir=tmp_base))

    try:
        if use_local:
            src = chosen["path"]
            sha_src = _Path(str(src) + ".sha256")
            backup_file = tmp_dir / src.name
            shutil.copy2(src, backup_file)
            if sha_src.exists():
                shutil.copy2(sha_src, tmp_dir / sha_src.name)
        else:
            backup_file = download_from_s3(s3, config, chosen["key"], tmp_dir, logger)

        verify_checksum(backup_file, logger)
        sql_path = decompress(backup_file, logger)

        if mode == "dry-run":
            size_mb = sql_path.stat().st_size / 1_048_576
            logger.info(
                "Dry-run complete. Backup '%s' is valid. SQL size: %.2f MB",
                backup_file.name,
                size_mb,
            )
            return

        # Full restore — require explicit confirmation
        print(f"\nWARNING: This will import '{backup_file.name}' into database '{config['db_name']}'.")
        print("This operation cannot be undone.\n")
        confirm = _prompt("Type 'yes' to proceed: ")
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
