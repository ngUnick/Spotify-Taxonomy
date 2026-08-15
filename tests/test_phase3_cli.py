from io import BytesIO, TextIOWrapper
from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import _render_cli_text, _safe_echo, app
from spotify_playlist_taxonomy.persistence import apply_migrations, connect_database
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class Phase3CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "runtime"
        result = RUNNER.invoke(app, [
            "db", "init", "--config", str(CONFIG), "--data-dir", str(self.data_dir)
        ])
        self.assertEqual(0, result.exit_code, result.output)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, *values: str) -> list[str]:
        return [*values, "--config", str(CONFIG), "--data-dir", str(self.data_dir)]

    def test_phase3_command_groups_are_exposed_without_network(self) -> None:
        spotify = RUNNER.invoke(app, ["spotify", "--help"])
        catalogue = RUNNER.invoke(app, ["catalogue", "--help"])
        self.assertEqual(0, spotify.exit_code, spotify.output)
        self.assertEqual(0, catalogue.exit_code, catalogue.output)
        for command in ("account", "source", "ingest"):
            self.assertIn(command, spotify.output)
        self.assertIn("match", catalogue.output)
        self.assertIn("link", catalogue.output)

    def test_local_status_and_review_commands_do_not_need_client_id(self) -> None:
        database = self.data_dir / "state" / "app.db"
        connection = connect_database(database)
        apply_migrations(connection)
        Phase3Store(connection).bind_account(
            "binding-review", "synthetic-review-account", "synthetic-review-user"
        )
        connection.close()
        account = RUNNER.invoke(app, self.args("spotify", "account", "status"))
        sources = RUNNER.invoke(app, self.args("spotify", "source", "list"))
        scans = RUNNER.invoke(app, self.args("spotify", "ingest", "status"))
        catalogue = RUNNER.invoke(app, self.args("catalogue", "status"))
        matches = RUNNER.invoke(app, self.args("catalogue", "match", "list"))
        links = RUNNER.invoke(app, self.args("catalogue", "link", "list"))
        self.assertEqual(0, account.exit_code, account.output)
        self.assertEqual(0, sources.exit_code, sources.output)
        self.assertEqual(0, scans.exit_code, scans.output)
        self.assertEqual(0, catalogue.exit_code, catalogue.output)
        self.assertEqual(0, matches.exit_code, matches.output)
        self.assertEqual(0, links.exit_code, links.output)
        self.assertIn("bound: true", account.output)
        self.assertIn("published_identities: 0", catalogue.output)

    def test_account_status_masks_persisted_identifiers(self) -> None:
        database = self.data_dir / "state" / "app.db"
        connection = connect_database(database)
        apply_migrations(connection)
        Phase3Store(connection).bind_account(
            "binding-synthetic", "synthetic-account-identifier", "synthetic-user-identifier"
        )
        connection.close()
        result = RUNNER.invoke(app, self.args("spotify", "account", "status"))
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("bound: true", result.output)
        self.assertNotIn("synthetic-account-identifier", result.output)
        self.assertNotIn("synthetic-user-identifier", result.output)

    def test_unicode_metadata_renders_unchanged_on_utf8_output(self) -> None:
        original = "Synthetic 💯 — Ελληνικά — Café — 東京"
        output = BytesIO()
        stream = TextIOWrapper(output, encoding="utf-8", errors="strict")
        try:
            _safe_echo(original, file=stream)
            stream.flush()
            rendered = output.getvalue().decode("utf-8")
        finally:
            stream.detach()
        self.assertEqual([original], rendered.splitlines())
        self.assertEqual("Synthetic 💯 — Ελληνικά — Café — 東京", original)

    def test_restrictive_windows_output_uses_readable_deterministic_fallback(self) -> None:
        original = "Synthetic 💯 — Ελληνικά — Café — 東京"
        output = BytesIO()
        stream = TextIOWrapper(output, encoding="cp1252", errors="strict")
        try:
            _safe_echo(original, file=stream)
            stream.flush()
            rendered = output.getvalue().decode("cp1252")
        finally:
            stream.detach()
        expected = _render_cli_text(original, "cp1252")
        self.assertEqual([expected], rendered.splitlines())
        self.assertIn(r"\U0001f4af", rendered)
        self.assertIn(r"\u0395", rendered)
        self.assertIn("Café", rendered)
        self.assertIn(r"\u6771\u4eac", rendered)
        self.assertEqual("Synthetic 💯 — Ελληνικά — Café — 東京", original)


if __name__ == "__main__":
    unittest.main()
