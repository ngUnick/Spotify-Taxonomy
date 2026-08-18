from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.config import load_taxonomy_document
from spotify_playlist_taxonomy.domain.models import CanonicalTrack
from spotify_playlist_taxonomy.persistence import (
    BackupError, SQLiteStateStore, apply_migrations, connect_database, create_backup,
    load_and_verify_backup, restore_backup, sync_taxonomy,
)
from spotify_playlist_taxonomy.runtime import RuntimePaths

ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = RuntimePaths(Path(self.temporary.name) / "runtime")
        self.paths.ensure_database_parent()
        connection = connect_database(self.paths.database_path)
        apply_migrations(connection)
        sync_taxonomy(connection, load_taxonomy_document(ROOT / "config" / "taxonomy.yaml"))
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_backup_is_lazy_and_verifiable(self) -> None:
        self.assertFalse(self.paths.backup_dir.exists())
        backup = create_backup(self.paths, retention_count=5)
        self.assertTrue(self.paths.backup_dir.is_dir())
        manifest = load_and_verify_backup(self.paths, backup)
        self.assertEqual(14, manifest.schema_version)
        self.assertEqual("taxonomy.v2", manifest.taxonomy_version)

    def test_restore_recovers_prior_database_and_protects_current(self) -> None:
        with SQLiteStateStore.open(self.paths.database_path) as store:
            store.save_canonical_track(CanonicalTrack(
                "track.synthetic.before", "Before Backup", ("Invented Artist",)
            ))
        backup = create_backup(self.paths, retention_count=5)
        with SQLiteStateStore.open(self.paths.database_path) as store:
            store.save_canonical_track(CanonicalTrack(
                "track.synthetic.after", "After Backup", ("Invented Artist",)
            ))
        restore_backup(self.paths, backup, retention_count=5)
        with SQLiteStateStore.open(self.paths.database_path) as store:
            self.assertEqual("Before Backup", store.get_canonical_track("track.synthetic.before").display_title)
            with self.assertRaises(LookupError):
                store.get_canonical_track("track.synthetic.after")
        self.assertGreaterEqual(len(list(self.paths.backup_dir.glob("backup-*.sqlite3"))), 2)

    def test_corruption_and_external_paths_are_rejected(self) -> None:
        backup = create_backup(self.paths, retention_count=5)
        with backup.open("ab") as handle:
            handle.write(b"corruption")
        with self.assertRaisesRegex(BackupError, "digest mismatch"):
            load_and_verify_backup(self.paths, backup)
        external = Path(self.temporary.name) / "backup-external.sqlite3"
        external.write_bytes(b"not a database")
        with self.assertRaisesRegex(BackupError, "runtime backup directory"):
            load_and_verify_backup(self.paths, external)

    def test_retention_only_removes_recognized_application_backups(self) -> None:
        unrelated = self.paths.root / "do-not-delete.txt"
        unrelated.parent.mkdir(parents=True, exist_ok=True)
        unrelated.write_text("synthetic", encoding="utf-8")
        lookalike = self.paths.backup_dir / "backup-unrelated.sqlite3"
        self.paths.backup_dir.mkdir(parents=True, exist_ok=True)
        lookalike.write_text("not an application backup", encoding="utf-8")
        for _ in range(3):
            create_backup(self.paths, retention_count=2)
        managed = [path for path in self.paths.backup_dir.glob("backup-*.sqlite3") if path != lookalike]
        self.assertEqual(2, len(managed))
        self.assertTrue(unrelated.exists())
        self.assertTrue(lookalike.exists())


if __name__ == "__main__":
    unittest.main()
