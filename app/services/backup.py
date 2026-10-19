"""Database automated backup service for PostgreSQL with gzip compression,
atomic replacement, rolling retention, and admin error notifications.
"""

import asyncio
from datetime import datetime, timezone
import gzip
import html
import logging
import os
from pathlib import Path
import shutil
from typing import List, Optional, Tuple

from aiogram import Bot

from app.config import settings

logger = logging.getLogger(__name__)

DEFAULT_BACKUP_DIR = Path("backups")


def ensure_backup_dir(backup_dir: Path) -> Path:
    """Ensure the backup directory exists with appropriate permissions."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backup_dir, 0o755)
    except Exception as e:
        logger.debug("Could not chmod backup directory %s: %s", backup_dir, e)
    return backup_dir


def prune_old_backups(backup_dir: Path, keep_last_n: int = 24) -> List[str]:
    """Retain the most recent `keep_last_n` timestamped backup archives and delete older ones."""
    pruned: List[str] = []
    try:
        files = list(backup_dir.glob("backup_*.sql.gz"))
        # Sort descending by filename (backup_YYYYMMDD_HHMMSS.sql.gz sorts chronologically)
        files.sort(key=lambda p: p.name, reverse=True)

        if len(files) > keep_last_n:
            for old_file in files[keep_last_n:]:
                try:
                    old_file.unlink(missing_ok=True)
                    pruned.append(old_file.name)
                    logger.info("Pruned old backup snapshot: %s", old_file.name)
                except Exception as del_err:
                    logger.warning("Could not delete old backup %s: %s", old_file, del_err)
    except Exception as err:
        logger.error("Error during backup pruning: %s", err)
    return pruned


async def create_database_backup(
    backup_dir_path: Optional[Path] = None,
    keep_last_n: int = 24,
) -> Tuple[bool, str, int]:
    """Execute a full PostgreSQL database dump, compress with gzip, and atomically update latest.sql.gz.
    
    Returns:
        (success: bool, filename_or_error: str, size_bytes: int)
    """
    backup_dir = ensure_backup_dir(backup_dir_path or DEFAULT_BACKUP_DIR)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_dump_file = backup_dir / f".dump_{timestamp}.tmp.gz"

    # Determine command execution strategy:
    # 1. Native pg_dump inside container or system PATH
    # 2. Host fallback to `docker exec aua_postgres pg_dump` if run locally outside container
    if shutil.which("pg_dump"):
        cmd = [
            "pg_dump",
            "-h", settings.POSTGRES_HOST,
            "-p", str(settings.POSTGRES_PORT),
            "-U", settings.POSTGRES_USER,
            "-d", settings.POSTGRES_DB,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
        ]
        env = os.environ.copy()
        env["PGPASSWORD"] = settings.POSTGRES_PASSWORD
    elif shutil.which("docker"):
        # Host fallback using docker exec against PostgreSQL container
        cmd = [
            "docker", "exec",
            "-e", f"PGPASSWORD={settings.POSTGRES_PASSWORD}",
            "aua_postgres",
            "pg_dump",
            "-U", settings.POSTGRES_USER,
            "-d", settings.POSTGRES_DB,
            "--clean",
            "--if-exists",
            "--no-owner",
            "--no-privileges",
        ]
        env = os.environ.copy()
    else:
        err_msg = "pg_dump toolchain unavailable (neither pg_dump nor docker found in PATH)"
        logger.error(err_msg)
        return False, err_msg, 0

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )

        # Stream stdout through gzip writer
        with gzip.open(temp_dump_file, "wb") as gz_file:
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                gz_file.write(chunk)

        _, stderr_bytes = await proc.communicate()

        if proc.returncode != 0:
            if temp_dump_file.exists():
                temp_dump_file.unlink()
            err_str = stderr_bytes.decode("utf-8", errors="replace").strip()
            logger.error("pg_dump process failed with code %d: %s", proc.returncode, err_str)
            return False, f"pg_dump error ({proc.returncode}): {err_str}", 0

        # Validate file size (empty gzip container is ~25-30 bytes)
        file_size = temp_dump_file.stat().st_size
        if file_size < 100:
            if temp_dump_file.exists():
                temp_dump_file.unlink()
            logger.error("Backup file validation failed: archive is too small (%d bytes)", file_size)
            return False, f"Dump failed: empty or truncated archive ({file_size} bytes)", 0

        # Verify gzip integrity and basic SQL header
        try:
            with gzip.open(temp_dump_file, "rb") as gz_check:
                header = gz_check.read(512)
                if not header:
                    raise ValueError("Empty uncompressed stream")
        except Exception as check_err:
            if temp_dump_file.exists():
                temp_dump_file.unlink()
            logger.error("Backup gzip verification failed: %s", check_err)
            return False, f"Corrupted backup archive: {check_err}", 0

        # 1. Create timestamped archive snapshot
        archive_name = f"backup_{timestamp}.sql.gz"
        archive_path = backup_dir / archive_name
        shutil.copy2(temp_dump_file, archive_path)
        try:
            os.chmod(archive_path, 0o644)
        except Exception:
            pass

        # 2. Atomically update primary `latest.sql.gz`
        latest_path = backup_dir / "latest.sql.gz"
        latest_tmp = backup_dir / ".latest.tmp.gz"
        shutil.copy2(temp_dump_file, latest_tmp)
        os.replace(latest_tmp, latest_path)
        try:
            os.chmod(latest_path, 0o644)
        except Exception:
            pass

        # Clean up temporary workfile
        temp_dump_file.unlink(missing_ok=True)

        # 3. Prune old rolling snapshots
        prune_old_backups(backup_dir, keep_last_n=keep_last_n)

        logger.info(
            "Database backup successful: %s and latest.sql.gz (size: %d bytes)",
            archive_name,
            file_size,
        )
        return True, archive_name, file_size

    except Exception as e:
        if temp_dump_file.exists():
            temp_dump_file.unlink(missing_ok=True)
        logger.exception("Unexpected error while generating database backup: %s", e)
        return False, f"Unexpected backup error: {e}", 0


def get_backup_metadata(backup_dir_path: Optional[Path] = None) -> dict:
    """Retrieve metadata about existing database backups for admin dashboards."""
    backup_dir = backup_dir_path or DEFAULT_BACKUP_DIR
    latest_file = backup_dir / "latest.sql.gz"
    all_snapshots = sorted(backup_dir.glob("backup_*.sql.gz"), key=lambda p: p.name, reverse=True)

    info = {
        "exists": latest_file.exists(),
        "latest_size_bytes": latest_file.stat().st_size if latest_file.exists() else 0,
        "latest_modified": (
            datetime.fromtimestamp(latest_file.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            if latest_file.exists()
            else None
        ),
        "total_snapshots": len(all_snapshots),
        "total_size_bytes": sum(f.stat().st_size for f in all_snapshots) + (latest_file.stat().st_size if latest_file.exists() else 0),
    }
    return info


async def hourly_backup_worker(bot: Optional[Bot] = None, interval_seconds: int = 3600) -> None:
    """Background worker that executes database backup every hour and alerts admins on failure."""
    logger.info("Hourly database backup worker started. Initial backup scheduled in 10 seconds.")
    # Initial settling delay
    await asyncio.sleep(10)

    while True:
        try:
            logger.info("Executing scheduled hourly database backup...")
            success, filename_or_err, size_bytes = await create_database_backup()
            if success:
                size_kb = size_bytes / 1024
                logger.info(
                    "Hourly database backup finished successfully: %s (%.2f KB)",
                    filename_or_err,
                    size_kb,
                )
            else:
                logger.error("Hourly database backup failed: %s", filename_or_err)
                if bot and settings.ADMIN_IDS:
                    safe_err = html.escape(str(filename_or_err))
                    alert_text = (
                        "🚨 <b>Database Backup Failure Alert</b> 🦅🇦🇲\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "The automated hourly database backup failed:\n"
                        f"<code>{safe_err}</code>\n"
                        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                        "Please inspect the server logs or trigger manual /backup."
                    )
                    for admin_id in settings.admin_ids:
                        try:
                            await bot.send_message(
                                chat_id=admin_id,
                                text=alert_text,
                                parse_mode="HTML",
                            )
                        except Exception as alert_err:
                            logger.warning("Could not send backup alert to admin %s: %s", admin_id, alert_err)
        except asyncio.CancelledError:
            logger.info("Hourly database backup worker cancelled.")
            break
        except Exception as e:
            logger.exception("Unexpected error in hourly backup loop: %s", e)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Hourly database backup worker received cancel during sleep.")
            break
