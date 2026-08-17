from __future__ import annotations

import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, connect_database

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class Phase6CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.data_dir = Path(self.temp.name) / "runtime"; self.runner = CliRunner()
        result = self.runner.invoke(app, ["db", "init", "--config", str(CONFIG), "--data-dir", str(self.data_dir)])
        self.assertEqual(0, result.exit_code, result.output)
        connection = connect_database(self.data_dir / "state" / "app.db")
        store = SQLiteStateStore(connection); store.save_canonical_track(CanonicalTrack("canonical.synthetic.cli", "Synthetic Ω", ("Invented Ω",)))
        store.save_evidence(ResearchEvidence("evidence.synthetic.cli", "canonical.synthetic.cli", ResearchSource.WIKIDATA, EvidenceKind.SOURCED_FACT,
            json.dumps({"claim":"style_tradition", "value":"pop"}), "https://example.invalid/evidence", .9,
            provider_id="wikidata", provider_version="wikidata.v1", request_id="request.synthetic", query_id="query.synthetic",
            subject_fingerprint="a" * 64, subject_scope="recording", subject_match="exact", evidence_type="style_tradition", claim_key="style_tradition", quality_band="high",
            normalized_schema_version="research-evidence.v1", normalizer_version="evidence-normalizer.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z", content_digest="b" * 64))
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def args(self, *args: str) -> list[str]:
        return [*args, "--config", str(CONFIG), "--data-dir", str(self.data_dir)]

    def test_offline_bounded_commands_render_unicode_and_make_proposals_only(self) -> None:
        with patch.object(socket.socket, "connect", side_effect=AssertionError("network attempted")):
            derived = self.runner.invoke(app, self.args("derive", "run", "canonical.synthetic.cli"))
            shown = self.runner.invoke(app, self.args("derive", "show", "canonical.synthetic.cli"))
            classified = self.runner.invoke(app, self.args("classify", "run", "canonical.synthetic.cli"))
            proposals = self.runner.invoke(app, self.args("classify", "show", "canonical.synthetic.cli"))
        self.assertEqual(0, derived.exit_code, derived.output); self.assertEqual(0, shown.exit_code, shown.output)
        self.assertIn("style.traditions", shown.output); self.assertEqual(0, classified.exit_code, classified.output)
        self.assertEqual(0, proposals.exit_code, proposals.output); self.assertIn("genre.pop\tdecision=include", proposals.output)
        self.assertNotIn("approved", proposals.output.casefold())


if __name__ == "__main__":
    unittest.main()
