from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, CanonicalTrack
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase7_batch import (
    BATCH_MAX_DECISIONS,
    BATCH_SCHEMA_VERSION,
    ReviewBatchValidationError,
    parse_review_batch,
)
from spotify_playlist_taxonomy.workflows.phase9 import Phase9Service


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class Phase7BatchReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        self.bundle = load_phase4_bundle(CONFIG)
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        store = SQLiteStateStore(self.connection)
        self.first = "canonical.synthetic.batch.first"
        self.second = "canonical.synthetic.batch.second"
        for canonical_id in (self.first, self.second):
            store.save_canonical_track(CanonicalTrack(canonical_id, "Synthetic", ("Invented",)))
        self.review = Phase7Service(self.connection, self.bundle)
        self.phase9 = Phase9Service(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def _document(self, *decisions: dict[str, object]) -> dict[str, object]:
        return {"schema": BATCH_SCHEMA_VERSION, "decisions": list(decisions)}

    def _count_events(self) -> int:
        return self.connection.execute("SELECT COUNT(*) FROM phase7_review_events").fetchone()[0]

    def test_batch_applies_multiple_decisions_with_individual_semantics_and_desired_state(self) -> None:
        decisions = parse_review_batch(self._document(
            {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include", "note": "synthetic include"},
            {"canonical_id": self.first, "semantic_id": "activity.driving", "decision": "exclude"},
            {"canonical_id": self.second, "semantic_id": "vibe.chill", "decision": "abstain"},
        ))
        outcome = self.review.apply_batch(decisions)
        self.assertEqual((3, 2, 1, 1, 1), (
            outcome.decision_count, outcome.canonical_track_count, outcome.include_count,
            outcome.exclude_count, outcome.abstain_count,
        ))
        self.assertEqual("include", self.review.effective(self.first, "vibe.chill").effective)
        self.assertEqual("exclude", self.review.effective(self.first, "activity.driving").effective)
        self.assertEqual("abstain", self.review.effective(self.second, "vibe.chill").effective)
        self.assertEqual(
            {"output.vibe.chill"},
            {item.output_target_id for item in self.phase9.desired(self.first).memberships},
        )
        self.assertEqual(1, len(self.review.history(self.first, "vibe.chill")))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM phase8_codex_proposals").fetchone()[0])

    def test_batch_matches_individual_decide_and_explicitly_outranks_generated_abstain(self) -> None:
        spec = next(item for item in self.bundle.specifications if item.logical_playlist_id == "vibe.chill")
        self.connection.execute(
            "INSERT INTO phase6_proposals VALUES (?, ?, 'vibe.chill', 'abstain', 'insufficient', .0, ?, ?, ?, 'synthetic.v1', ?, ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')",
            ("proposal.synthetic.batch", self.first, spec.specification_version, self.bundle.taxonomy.taxonomy_version, self.bundle.vocabulary.vocabulary_version, self.bundle.digest, "input.synthetic.batch"),
        )
        self.connection.commit()
        self.review.apply_batch(parse_review_batch(self._document(
            {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include"},
        )))
        self.review.decide(self.second, "vibe.chill", AssignmentDecision.INCLUDE)
        first = self.review.effective(self.first, "vibe.chill")
        second = self.review.effective(self.second, "vibe.chill")
        self.assertEqual(("include", "include", False, False), (first.explicit, first.effective, first.needs_review, first.stale))
        self.assertEqual(
            (second.explicit, second.effective, second.needs_review, second.stale),
            (first.explicit, first.effective, first.needs_review, first.stale),
        )

    def test_invalid_documents_fail_before_writes(self) -> None:
        invalid_documents = (
            self._document({"canonical_id": "canonical.synthetic.missing", "semantic_id": "vibe.chill", "decision": "include"}),
            self._document({"canonical_id": self.first, "semantic_id": "unknown.semantic", "decision": "include"}),
            self._document({"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "approve"}),
            self._document(
                {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include"},
                {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "exclude"},
            ),
            {"schema": "phase7-review-batch.v0", "decisions": []},
            {"schema": BATCH_SCHEMA_VERSION, "decisions": [{"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include", "unexpected": "x"}]},
        )
        for document in invalid_documents:
            with self.subTest(document=document):
                before = self._count_events()
                try:
                    decisions = parse_review_batch(document)
                    self.review.apply_batch(decisions)
                except (ReviewBatchValidationError, ValueError):
                    pass
                else:
                    self.fail("invalid batch was accepted")
                self.assertEqual(before, self._count_events())
        accepted = self._document(*[
            {"canonical_id": self.first, "semantic_id": f"synthetic.semantic.{index}", "decision": "include"}
            for index in range(BATCH_MAX_DECISIONS)
        ])
        self.assertEqual(BATCH_MAX_DECISIONS, len(parse_review_batch(accepted)))
        too_many = self._document(*[
            {"canonical_id": self.first, "semantic_id": f"vibe.chill.{index}", "decision": "include"}
            for index in range(BATCH_MAX_DECISIONS + 1)
        ])
        with self.assertRaises(ReviewBatchValidationError):
            parse_review_batch(too_many)

    def test_database_failure_rolls_back_entire_batch(self) -> None:
        self.connection.execute(
            "CREATE TRIGGER synthetic_batch_failure BEFORE INSERT ON phase7_review_events "
            "WHEN NEW.canonical_track_id='canonical.synthetic.batch.second' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic batch failure'); END"
        )
        self.connection.commit()
        decisions = parse_review_batch(self._document(
            {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include"},
            {"canonical_id": self.second, "semantic_id": "activity.driving", "decision": "exclude"},
        ))
        with self.assertRaisesRegex(ValueError, "could not be applied"):
            self.review.apply_batch(decisions)
        self.assertEqual(0, self._count_events())

    def test_cli_import_is_local_and_reports_compact_summary(self) -> None:
        data_dir = Path(self.temp.name) / "runtime"
        (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db")
        self.connection.backup(target)
        target.close()
        batch_file = Path(self.temp.name) / "review-batch.json"
        batch_file.write_text(json.dumps(self._document(
            {"canonical_id": self.first, "semantic_id": "vibe.chill", "decision": "include"},
            {"canonical_id": self.second, "semantic_id": "activity.driving", "decision": "abstain"},
        )), encoding="utf-8")
        result = CliRunner().invoke(app, [
            "review", "batch", "import", str(batch_file), "--config", str(CONFIG), "--data-dir", str(data_dir),
        ])
        self.assertEqual(0, result.exit_code, result.output)
        for line in ("batch_decisions: 2", "canonical_tracks: 2", "include: 1", "exclude: 0", "abstain: 1", "status: applied"):
            self.assertIn(line, result.output)
        verified = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(2, verified.execute("SELECT COUNT(*) FROM phase7_review_events").fetchone()[0])
        self.assertEqual(0, verified.execute("SELECT COUNT(*) FROM phase8_codex_proposals").fetchone()[0])
        verified.close()

    def test_cli_rejects_malformed_json_without_writes(self) -> None:
        data_dir = Path(self.temp.name) / "malformed-runtime"
        (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db")
        self.connection.backup(target)
        target.close()
        batch_file = Path(self.temp.name) / "malformed-review-batch.json"
        batch_file.write_text("{not json", encoding="utf-8")
        result = CliRunner().invoke(app, [
            "review", "batch", "import", str(batch_file), "--config", str(CONFIG), "--data-dir", str(data_dir),
        ])
        self.assertNotEqual(0, result.exit_code)
        self.assertIn("review batch file is unreadable or invalid JSON", result.output)
        verified = connect_database(data_dir / "state" / "app.db")
        self.assertEqual(0, verified.execute("SELECT COUNT(*) FROM phase7_review_events").fetchone()[0])
        verified.close()


if __name__ == "__main__":
    unittest.main()
