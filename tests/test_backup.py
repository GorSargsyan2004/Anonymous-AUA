"""Test suite for automated database backup system."""

import asyncio
import gzip
import logging
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from app.services.backup import (
    create_database_backup,
    get_backup_metadata,
    prune_old_backups,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_backup")

TEST_BACKUP_DIR = Path("test_backups_tmp")


class TestDatabaseBackup(unittest.IsolatedAsyncioTestCase):
    """Test suite for backup creation, atomic updating, gzip compression, and rotation."""

    async def asyncSetUp(self):
        if TEST_BACKUP_DIR.exists():
            shutil.rmtree(TEST_BACKUP_DIR)
        TEST_BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    async def asyncTearDown(self):
        if TEST_BACKUP_DIR.exists():
            shutil.rmtree(TEST_BACKUP_DIR)

    async def test_01_create_backup_success(self):
        """Test full backup creation and gzip validation."""
        success, filename, size_bytes = await create_database_backup(
            backup_dir_path=TEST_BACKUP_DIR,
            keep_last_n=5,
        )
        self.assertTrue(success, f"Backup creation failed: {filename}")
        self.assertGreater(size_bytes, 100, "Backup file should be non-empty")
        self.assertTrue(filename.startswith("backup_") and filename.endswith(".sql.gz"))

        latest_file = TEST_BACKUP_DIR / "latest.sql.gz"
        archive_file = TEST_BACKUP_DIR / filename
        self.assertTrue(latest_file.exists(), "latest.sql.gz must exist")
        self.assertTrue(archive_file.exists(), "Archive snapshot must exist")

        # Verify it can be decompressed as valid SQL
        with gzip.open(latest_file, "rt", encoding="utf-8", errors="ignore") as gz:
            sql_content = gz.read(2048)
            self.assertIn("PostgreSQL database dump", sql_content)
            self.assertTrue(
                "TABLE" in sql_content or "CREATE" in sql_content or "SET" in sql_content,
                "SQL dump should contain PostgreSQL DDL statements",
            )
        logger.info("test_01_create_backup_success passed (%d bytes)", size_bytes)

    async def test_02_atomic_overwrite(self):
        """Test that successive backups overwrite latest.sql.gz atomically."""
        success1, file1, size1 = await create_database_backup(backup_dir_path=TEST_BACKUP_DIR)
        self.assertTrue(success1)
        latest_file = TEST_BACKUP_DIR / "latest.sql.gz"
        mtime1 = latest_file.stat().st_mtime

        # Short sleep to guarantee distinct timestamp
        await asyncio.sleep(1.1)

        success2, file2, size2 = await create_database_backup(backup_dir_path=TEST_BACKUP_DIR)
        self.assertTrue(success2)
        mtime2 = latest_file.stat().st_mtime

        self.assertNotEqual(file1, file2)
        self.assertGreaterEqual(mtime2, mtime1)
        self.assertTrue(latest_file.exists())
        logger.info("test_02_atomic_overwrite passed")

    async def test_03_prune_old_backups(self):
        """Test that retention policy keeps only the N newest snapshots."""
        # Create dummy snapshots with ascending timestamps
        for i in range(1, 8):
            dummy = TEST_BACKUP_DIR / f"backup_20261019_12000{i}.sql.gz"
            dummy.write_bytes(b"dummy_data_" + str(i).encode())

        # Verify 7 dummy files exist
        self.assertEqual(len(list(TEST_BACKUP_DIR.glob("backup_*.sql.gz"))), 7)

        # Prune keeping only last 3
        pruned = prune_old_backups(TEST_BACKUP_DIR, keep_last_n=3)
        self.assertEqual(len(pruned), 4)

        remaining = sorted([p.name for p in TEST_BACKUP_DIR.glob("backup_*.sql.gz")])
        self.assertEqual(len(remaining), 3)
        self.assertEqual(remaining, [
            "backup_20261019_120005.sql.gz",
            "backup_20261019_120006.sql.gz",
            "backup_20261019_120007.sql.gz",
        ])
        logger.info("test_03_prune_old_backups passed")

    async def test_04_metadata_retrieval(self):
        """Test get_backup_metadata reporting."""
        meta_empty = get_backup_metadata(TEST_BACKUP_DIR)
        self.assertFalse(meta_empty["exists"])
        self.assertEqual(meta_empty["total_snapshots"], 0)

        success, filename, size_bytes = await create_database_backup(backup_dir_path=TEST_BACKUP_DIR)
        self.assertTrue(success)

        meta = get_backup_metadata(TEST_BACKUP_DIR)
        self.assertTrue(meta["exists"])
        self.assertEqual(meta["total_snapshots"], 1)
        self.assertGreater(meta["latest_size_bytes"], 0)
        self.assertIsNotNone(meta["latest_modified"])
        logger.info("test_04_metadata_retrieval passed: %s", meta)

    async def test_05_gitignore_security(self):
        """Verify that backups/ directory and sql dump extensions are ignored by git."""
        gitignore_path = Path(".gitignore")
        self.assertTrue(gitignore_path.exists(), ".gitignore file must exist")
        content = gitignore_path.read_text(encoding="utf-8")

        self.assertIn("backups/", content, ".gitignore must contain 'backups/'")
        self.assertIn("*.sql", content, ".gitignore must contain '*.sql'")
        self.assertIn("*.sql.gz", content, ".gitignore must contain '*.sql.gz'")
        self.assertIn("*.dump", content, ".gitignore must contain '*.dump'")

        if shutil.which("git"):
            result = subprocess.run(
                ["git", "check-ignore", "backups/latest.sql.gz", "backups/backup_20261019_120000.sql.gz", "test.dump"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, "Git check-ignore should match backup paths")
            ignored = result.stdout.strip().splitlines()
            self.assertIn("backups/latest.sql.gz", ignored)
            self.assertIn("backups/backup_20261019_120000.sql.gz", ignored)
            self.assertIn("test.dump", ignored)

        logger.info("test_05_gitignore_security passed: backups are strictly git-ignored")


if __name__ == "__main__":
    unittest.main()
