import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import db


class LegacyMigrationScanTests(unittest.TestCase):
    def test_unchanged_sources_are_scanned_once(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "registrations.db"
            source.write_bytes(b"legacy")
            with patch.object(db, "_LEGACY_SQLITE", source), \
                 patch.object(db, "_PROJECT_ROOT", Path(folder)), \
                 patch.object(db, "_OUTLOOK_TXT", Path(folder) / "outlook.txt"), \
                 patch.object(db, "_migrate_legacy_sqlite", return_value={}) as migrate:
                db._LEGACY_MIGRATION_SIGNATURE = None
                db.migrate_legacy_files()
                db.migrate_legacy_files()
                self.assertEqual(migrate.call_count, 1)
                source.write_bytes(b"changed")
                db.migrate_legacy_files()
                self.assertEqual(migrate.call_count, 2)
                db._LEGACY_MIGRATION_SIGNATURE = None


if __name__ == "__main__":
    unittest.main()
