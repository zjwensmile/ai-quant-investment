import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path

from ai_quant.backup import BackupService
from ai_quant.config import Settings


class BackupTests(unittest.TestCase):
    def test_backup_is_consistent_and_verifiable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                workspace=root, runtime_dir=root / "runtime",
                database_path=root / "runtime" / "audit.sqlite3",
                market_database_path=root / "runtime" / "market.sqlite3",
            )
            settings.runtime_dir.mkdir()
            for path in (settings.database_path, settings.resolved_market_database_path):
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE sample(value TEXT)")
                    connection.execute("INSERT INTO sample VALUES ('ok')")
                    connection.commit()
            snapshot = settings.runtime_dir / "snapshots" / "snapshot.json"
            snapshot.parent.mkdir()
            snapshot.write_text('{"snapshot_id":"test"}', encoding="utf-8")
            schedule_manifest = settings.runtime_dir / "scheduler" / "install-manifest.json"
            schedule_manifest.parent.mkdir()
            schedule_manifest.write_text('{"version":"test"}', encoding="utf-8")
            config = root / "config" / "system.json"
            config.parent.mkdir()
            config.write_text('{"real_trading_enabled":false}', encoding="utf-8")
            target = BackupService(settings).create()
            verification = BackupService.verify_and_record(target)
            self.assertTrue(verification["valid"])
            self.assertEqual("backup-manifest-v2", verification["manifest_version"])
            self.assertTrue((target / "runtime" / "snapshots" / "snapshot.json").exists())
            self.assertTrue(
                (target / "runtime" / "scheduler" / "install-manifest.json").exists()
            )
            self.assertTrue((target / "config" / "system.json").exists())
            self.assertTrue(BackupService.verification_status(target)["valid"])
            restored = root / "restored"
            report = BackupService.restore(target, restored)
            self.assertTrue(report["valid"])
            self.assertTrue((restored / "runtime" / "ai_quant_v2.sqlite3").exists())
            self.assertTrue((restored / "runtime" / "market.sqlite3").exists())
            self.assertEqual(
                '{"snapshot_id":"test"}',
                (restored / "runtime" / "snapshots" / "snapshot.json").read_text(
                    encoding="utf-8"
                ),
            )
            self.assertTrue((restored / "restore-report.json").exists())
            restore_verification = BackupService.verify_restore(restored)
            self.assertTrue(restore_verification["valid"])
            self.assertEqual("restore-report-v1", restore_verification["restore_version"])
            self.assertTrue(BackupService.restore_status(restored)["valid"])

    def test_restore_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            backup = root / "backup"
            backup.mkdir()
            (backup / "manifest.json").write_text(
                '{"version":"backup-manifest-v2","files":{}}', encoding="utf-8"
            )
            destination = root / "existing"; destination.mkdir()
            with self.assertRaises(FileExistsError):
                BackupService.restore(backup, destination)


if __name__ == "__main__":
    unittest.main()
