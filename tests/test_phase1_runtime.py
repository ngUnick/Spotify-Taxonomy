from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.runtime import RuntimePathError, RuntimePaths


class RuntimePathTests(unittest.TestCase):
    def test_precedence_is_cli_then_environment_then_localappdata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            env = {
                "SPOTIFY_TAXONOMY_DATA_DIR": str(base / "environment"),
                "LOCALAPPDATA": str(base / "local"),
            }
            self.assertEqual((base / "cli").resolve(), RuntimePaths.resolve(
                cli_data_dir=base / "cli", environment=env
            ).root)
            self.assertEqual((base / "environment").resolve(), RuntimePaths.resolve(environment=env).root)
            self.assertEqual((base / "local" / "SpotifyPlaylistTaxonomy").resolve(),
                             RuntimePaths.resolve(environment={"LOCALAPPDATA": str(base / "local")}).root)

    def test_resolving_paths_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "runtime"
            paths = RuntimePaths.resolve(cli_data_dir=root, environment={})
            _ = (paths.database_path, paths.backup_dir, paths.outbound_batch_dir, paths.export_dir, paths.log_dir)
            self.assertFalse(root.exists())

    def test_database_backup_and_log_directories_are_lazy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = RuntimePaths.resolve(cli_data_dir=Path(directory) / "runtime", environment={})
            paths.ensure_database_parent()
            self.assertTrue(paths.state_dir.is_dir())
            self.assertFalse(paths.backup_dir.exists())
            self.assertFalse(paths.outbound_batch_dir.exists())
            self.assertFalse(paths.export_dir.exists())
            self.assertFalse(paths.log_dir.exists())
            paths.ensure_backup_dir()
            self.assertTrue(paths.backup_dir.is_dir())
            paths.ensure_log_dir()
            self.assertTrue(paths.log_dir.is_dir())
            self.assertFalse(paths.outbound_batch_dir.exists())

    def test_runtime_inside_repository_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimePathError):
                RuntimePaths.resolve(cli_data_dir=root / "runtime", environment={}, repository_root=root)


if __name__ == "__main__":
    unittest.main()
