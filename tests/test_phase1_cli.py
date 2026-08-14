from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class CliTests(unittest.TestCase):
    def test_help_and_config_validation_are_local_and_nonmutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
                help_result = RUNNER.invoke(app, ["--help"])
                result = RUNNER.invoke(app, ["config", "validate", "--config", str(CONFIG)])
            self.assertEqual(0, help_result.exit_code)
            self.assertEqual(0, result.exit_code, result.output)
            self.assertIn("playlists=36", result.output)
            self.assertFalse(runtime.exists())

    def test_runtime_paths_command_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            result = RUNNER.invoke(app, [
                "runtime", "paths", "--config", str(CONFIG), "--data-dir", str(runtime),
            ])
            self.assertEqual(0, result.exit_code, result.output)
            self.assertIn("exists=False", result.output)
            self.assertFalse(runtime.exists())

    def test_db_init_is_idempotent_and_only_creates_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            args = ["db", "init", "--config", str(CONFIG), "--data-dir", str(runtime)]
            with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
                first = RUNNER.invoke(app, args)
                second = RUNNER.invoke(app, args)
                status = RUNNER.invoke(app, [
                    "db", "status", "--config", str(CONFIG), "--data-dir", str(runtime),
                ])
            self.assertEqual(0, first.exit_code, first.output)
            self.assertIn("synchronized=True", first.output)
            self.assertEqual(0, second.exit_code, second.output)
            self.assertIn("synchronized=False", second.output)
            self.assertEqual(0, status.exit_code, status.output)
            self.assertIn("taxonomy_in_sync: True", status.output)
            self.assertTrue((runtime / "state" / "app.db").is_file())
            self.assertFalse((runtime / "backups").exists())
            self.assertFalse((runtime / "batches").exists())
            self.assertFalse((runtime / "exports").exists())
            self.assertFalse((runtime / "logs").exists())

    def test_backup_cli_creates_only_backup_workflow_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            init = RUNNER.invoke(app, [
                "db", "init", "--config", str(CONFIG), "--data-dir", str(runtime),
            ])
            self.assertEqual(0, init.exit_code, init.output)
            result = RUNNER.invoke(app, [
                "backup", "create", "--config", str(CONFIG), "--data-dir", str(runtime),
            ])
            self.assertEqual(0, result.exit_code, result.output)
            self.assertTrue((runtime / "backups").is_dir())
            self.assertFalse((runtime / "batches").exists())
            self.assertFalse((runtime / "exports").exists())


if __name__ == "__main__":
    unittest.main()
