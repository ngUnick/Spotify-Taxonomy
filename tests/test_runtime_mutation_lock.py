from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.runtime import RuntimeMutationLock


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"

HOLDER = """
import sys
import time
from pathlib import Path
from spotify_playlist_taxonomy.runtime import RuntimeMutationLock
with RuntimeMutationLock(Path(sys.argv[1]), 'synthetic_holder'):
    print('held', flush=True)
    time.sleep(60)
"""

CONTENDER = """
import sys
from pathlib import Path
from spotify_playlist_taxonomy.runtime import MutationLockError, RuntimeMutationLock
try:
    with RuntimeMutationLock(Path(sys.argv[1]), 'synthetic_contender'):
        print('acquired')
except MutationLockError:
    print('blocked')
"""


class RuntimeMutationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temp.name) / "runtime"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + environment.get("PYTHONPATH", "")
        return environment

    def _holder(self) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(self.runtime)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self._environment(),
        )
        self.assertEqual("held", process.stdout.readline().strip())
        return process

    def test_live_process_blocks_same_runtime_and_releases_after_exit(self) -> None:
        holder = self._holder()
        try:
            status = RuntimeMutationLock.inspect(self.runtime)
            self.assertEqual("held", status.state)
            self.assertEqual("synthetic_holder", status.holder_purpose)
            contender = subprocess.run(
                [sys.executable, "-c", CONTENDER, str(self.runtime)],
                capture_output=True,
                text=True,
                env=self._environment(),
                check=True,
            )
            self.assertEqual("blocked", contender.stdout.strip())
            other = subprocess.run(
                [sys.executable, "-c", CONTENDER, str(Path(self.temp.name) / "other-runtime")],
                capture_output=True,
                text=True,
                env=self._environment(),
                check=True,
            )
            self.assertEqual("acquired", other.stdout.strip())
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()

        # Metadata remains on disk, but only the live OS lock is authoritative.
        self.assertEqual("free", RuntimeMutationLock.inspect(self.runtime).state)
        with RuntimeMutationLock(self.runtime, "synthetic_after_crash"):
            reacquire_contender = subprocess.run(
                [sys.executable, "-c", CONTENDER, str(self.runtime)],
                capture_output=True,
                text=True,
                env=self._environment(),
                check=True,
            )
            self.assertEqual("blocked", reacquire_contender.stdout.strip())

    def test_status_reports_live_writer_and_locked_mutation_never_reaches_provider(self) -> None:
        from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database

        (self.runtime / "state").mkdir(parents=True)
        connection = connect_database(self.runtime / "state" / "app.db")
        apply_migrations(connection)
        connection.close()
        holder = self._holder()
        try:
            runner = CliRunner()
            status = runner.invoke(app, [
                "catalogue", "operator", "status", "--config", str(CONFIG), "--data-dir", str(self.runtime),
            ])
            self.assertEqual(0, status.exit_code, status.output)
            self.assertIn("mutation_lock_state: held", status.output)
            self.assertIn("mutation_lock_purpose: synthetic_holder", status.output)
            self.assertIn("next_action: wait_for_active_writer", status.output)
            with patch("spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter", side_effect=AssertionError("provider access is forbidden")):
                blocked = runner.invoke(app, [
                    "populate", "apply", "output.vibe.chill", "canonical.synthetic.locked",
                    "ADD:output.vibe.chill:canonical.synthetic.locked",
                    "--config", str(CONFIG), "--data-dir", str(self.runtime),
                ])
            self.assertNotEqual(0, blocked.exit_code)
            self.assertIn("mutation lock already held", blocked.output)
        finally:
            holder.terminate()
            holder.wait(timeout=10)
            holder.stdout.close()
            holder.stderr.close()

    def test_cli_keeps_lock_for_entire_guarded_population_operation(self) -> None:
        from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
        from spotify_playlist_taxonomy.workflows.phase11 import PopulationStatus

        (self.runtime / "state").mkdir(parents=True)
        connection = connect_database(self.runtime / "state" / "app.db")
        apply_migrations(connection)
        connection.close()
        observed: list[str] = []

        def guarded_operation(*args: object, **kwargs: object) -> PopulationStatus:
            observed.append(RuntimeMutationLock.inspect(self.runtime).state)
            return PopulationStatus("output.vibe.chill", "canonical.synthetic.locked", "confirmed_added", "synthetic")

        with patch("spotify_playlist_taxonomy.cli._populate_operation", side_effect=guarded_operation):
            result = CliRunner().invoke(app, [
                "populate", "apply", "output.vibe.chill", "canonical.synthetic.locked",
                "ADD:output.vibe.chill:canonical.synthetic.locked",
                "--config", str(CONFIG), "--data-dir", str(self.runtime),
            ])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertEqual(["held"], observed)
        self.assertEqual("free", RuntimeMutationLock.inspect(self.runtime).state)


if __name__ == "__main__":
    unittest.main()
