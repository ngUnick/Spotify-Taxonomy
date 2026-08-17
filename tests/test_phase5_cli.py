from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource,
)
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, connect_database

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class Phase5CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temporary.name) / "runtime"
        result = RUNNER.invoke(app, [
            "db", "init", "--config", str(CONFIG), "--data-dir", str(self.data_dir)
        ])
        self.assertEqual(0, result.exit_code, result.output)
        connection = connect_database(self.data_dir / "state" / "app.db")
        store = SQLiteStateStore(connection)
        store.save_canonical_track(CanonicalTrack(
            "canonical.synthetic.unicode", "Νυχτερινό Σήμα", ("Invented Ω",)
        ))
        store.save_evidence(ResearchEvidence(
            "evidence.synthetic.unicode", "canonical.synthetic.unicode",
            ResearchSource.WIKIDATA, EvidenceKind.SOURCED_FACT,
            '{"claim":"style_tradition","value":"δοκιμή"}',
            "https://www.wikidata.org/wiki/Q123", None,
            provider_id="wikidata", provider_version="wikidata.v1",
            request_id="request.synthetic", query_id="query.synthetic",
            subject_fingerprint="f" * 64, subject_scope="recording",
            subject_match="exact", evidence_type="style_tradition",
            claim_key="style_tradition", quality_band="high",
            normalized_schema_version="research-evidence.v1",
            normalizer_version="evidence-normalizer.v1",
            retrieved_at="2026-01-02T03:04:05Z",
            fresh_until="2099-01-02T03:04:05Z", content_digest="d" * 64,
        ))
        connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def args(self, *values: str) -> list[str]:
        return [*values, "--config", str(CONFIG), "--data-dir", str(self.data_dir)]

    def test_cli_surface_is_minimal(self) -> None:
        result = RUNNER.invoke(app, ["research", "--help"])
        self.assertEqual(0, result.exit_code, result.output)
        for command in ("run", "show", "status"):
            self.assertIn(command, result.output)
        self.assertNotIn("classify", result.output)
        self.assertNotIn("playlist", result.output.casefold())

    def test_run_requires_explicit_network_before_opening_provider(self) -> None:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
            result = RUNNER.invoke(app, self.args(
                "research", "run", "canonical.synthetic.unicode"
            ))
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("requires explicit --network", result.output)

    def test_show_and_status_are_offline_account_independent_and_unicode_safe(self) -> None:
        database = self.data_dir / "state" / "app.db"
        before = database.read_bytes()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
            shown = RUNNER.invoke(app, self.args(
                "research", "show", "canonical.synthetic.unicode"
            ))
            status = RUNNER.invoke(app, self.args(
                "research", "status", "canonical.synthetic.unicode"
            ))
        self.assertEqual(0, shown.exit_code, shown.output)
        self.assertIn("δοκιμή", shown.output)
        self.assertIn("provider=wikidata", shown.output)
        self.assertEqual(0, status.exit_code, status.output)
        self.assertIn("style_tradition", status.output)
        self.assertIn("state=satisfied", status.output)
        self.assertNotIn("Spotify Client ID", shown.output + status.output)
        self.assertEqual(before, database.read_bytes())

    def test_status_reports_sanitized_provider_outcome_reason_offline(self) -> None:
        connection = connect_database(self.data_dir / "state" / "app.db")
        try:
            connection.execute(
                "INSERT INTO research_jobs VALUES (?, ?, ?, ?, ?, ?, 'completed', ?, ?)",
                ("job.synthetic.no-claims", "canonical.synthetic.unicode", "request.synthetic.no-claims",
                 "f" * 64, "research.v1", "a" * 64,
                 "2026-01-02T03:04:05Z", "2026-01-02T03:04:05Z"),
            )
            connection.execute(
                "INSERT INTO research_provider_attempts("
                "attempt_id, job_id, provider_id, adapter_version, query_id, query_fingerprint, outcome, "
                "cache_hit, attempt_number, started_at, completed_at, error_category"
                ") VALUES (?, ?, 'wikidata', 'wikidata.v1', 'query.synthetic', 'f', 'no_result', 0, 1, ?, ?, ?)",
                ("attempt.synthetic.no-claims", "job.synthetic.no-claims", "2026-01-02T03:04:05Z",
                 "2026-01-02T03:04:05Z", "candidate_found_no_supported_claims"),
            )
            connection.commit()
        finally:
            connection.close()
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
            result = RUNNER.invoke(app, self.args("research", "status", "canonical.synthetic.unicode"))
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("outcome=no_result", result.output)
        self.assertIn("reason=candidate_found_no_supported_claims", result.output)
        self.assertNotIn("raw", result.output.casefold())

    def test_config_validation_includes_strict_research_policy_offline(self) -> None:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
            result = RUNNER.invoke(app, ["config", "validate", "--config", str(CONFIG)])
        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("research_policy=research.v1", result.output)
        self.assertIn("research_providers=2", result.output)


if __name__ == "__main__":
    unittest.main()
