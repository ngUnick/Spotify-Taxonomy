from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import _bootstrap  # noqa: F401
from typer.testing import CliRunner

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.bulk_review import BulkReviewService
from spotify_playlist_taxonomy.workflows.catalogue_classification_batch import (
    CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS,
    CatalogueClassificationBatchService,
    CatalogueClassificationBatchValidationError,
    parse_catalogue_classification_batch,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"


class CatalogueCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.temp.name) / "app.db")
        self.assertEqual(20, apply_migrations(self.connection))
        self.bundle = load_phase4_bundle(CONFIG)
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.first, self.second = "canonical.synthetic.completion.first", "canonical.synthetic.completion.second"
        store = SQLiteStateStore(self.connection)
        for canonical_id in (self.first, self.second):
            store.save_canonical_track(CanonicalTrack(canonical_id, "Synthetic", ("Invented",)))
        self.combined = CatalogueClassificationBatchService(self.connection, self.bundle)

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def _document(self, tracks: list[dict[str, object]]) -> dict[str, object]:
        return {"schema": "catalogue-classification-batch.v1", "tracks": tracks}

    def test_combined_import_is_atomic_and_preserves_existing_event_semantics(self) -> None:
        outcome = self.combined.apply(parse_catalogue_classification_batch(self._document([
            {"canonical_id": self.first, "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}], "genre_route": "foreign"},
            {"canonical_id": self.second, "decisions": [{"semantic_id": "activity.driving", "decision": "exclude", "note": "synthetic"}]},
        ])))
        self.assertEqual((2, 2, 1, 1, 1, 0), (
            outcome.track_count, outcome.review_decision_count, outcome.routing_decision_count,
            outcome.include_count, outcome.exclude_count, outcome.abstain_count,
        ))
        self.assertEqual("include", self.connection.execute("SELECT decision FROM phase7_review_events WHERE canonical_track_id=?", (self.first,)).fetchone()[0])
        self.assertEqual("foreign", self.connection.execute("SELECT route FROM genre_routing_events WHERE canonical_track_id=?", (self.first,)).fetchone()[0])
        self.combined.apply(parse_catalogue_classification_batch(self._document([
            {"canonical_id": self.first, "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}], "genre_route": "foreign"},
        ])))
        self.assertEqual(2, self.connection.execute("SELECT COUNT(*) FROM phase7_review_events").fetchone()[0])
        self.assertEqual(1, self.connection.execute("SELECT COUNT(*) FROM genre_routing_events").fetchone()[0])

    def test_combined_validation_and_database_failure_leave_no_partial_writes(self) -> None:
        invalid = self._document([
            {"canonical_id": self.first, "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}, {"semantic_id": "vibe.chill", "decision": "exclude"}]},
        ])
        with self.assertRaises(CatalogueClassificationBatchValidationError):
            parse_catalogue_classification_batch(invalid)
        self.connection.execute("CREATE TRIGGER synthetic_combined_failure BEFORE INSERT ON genre_routing_events BEGIN SELECT RAISE(ABORT, 'synthetic'); END")
        self.connection.commit()
        with self.assertRaisesRegex(ValueError, "could not be applied"):
            self.combined.apply(parse_catalogue_classification_batch(self._document([
                {"canonical_id": self.first, "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}], "genre_route": "foreign"},
            ])))
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM phase7_review_events").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT COUNT(*) FROM genre_routing_events").fetchone()[0])

    def test_combined_parser_accepts_250_tracks(self) -> None:
        document = self._document([
            {"canonical_id": f"canonical.synthetic.bulk.{index}", "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}]}
            for index in range(CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS)
        ])
        self.assertEqual(CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS, len(parse_catalogue_classification_batch(document)))

    def test_bulk_preview_is_fail_closed_and_apply_requires_regenerated_plan(self) -> None:
        service = BulkReviewService(self.connection, self.bundle)
        preview = service.preview(25)
        states = {item.canonical_track_id: item.state for item in preview.items}
        self.assertEqual("manual_review_required", states[self.first])
        self._insert_complete_phase6(self.first)
        eligible = service.preview(25)
        item = next(item for item in eligible.items if item.canonical_track_id == self.first)
        self.assertEqual("eligible_for_bulk_approval", item.state)
        self.assertEqual(36, len(item.decisions))
        with self.assertRaises(ValueError):
            service.apply("wrong", limit=25)
        service.apply(eligible.plan_id, limit=25)
        self.assertEqual(36, self.connection.execute("SELECT COUNT(*) FROM phase7_review_events WHERE canonical_track_id=?", (self.first,)).fetchone()[0])
        self.assertEqual("manual_review_required", next(item for item in service.preview(25).items if item.canonical_track_id == self.first).state)

    def test_worklist_allows_250_and_combined_cli_is_local(self) -> None:
        data_dir = Path(self.temp.name) / "runtime"; (data_dir / "state").mkdir(parents=True)
        target = connect_database(data_dir / "state" / "app.db"); self.connection.backup(target); target.close()
        batch_file = Path(self.temp.name) / "combined.json"
        batch_file.write_text(json.dumps(self._document([
            {"canonical_id": self.first, "decisions": [{"semantic_id": "vibe.chill", "decision": "include"}], "genre_route": "foreign"},
        ])), encoding="utf-8")
        runner = CliRunner()
        imported = runner.invoke(app, ["catalogue", "classify", "batch", "import", str(batch_file), "--config", str(CONFIG), "--data-dir", str(data_dir)])
        worklist = runner.invoke(app, ["catalogue", "classify", "worklist", "--limit", "250", "--config", str(CONFIG), "--data-dir", str(data_dir)])
        self.assertEqual(0, imported.exit_code, imported.output)
        self.assertEqual(0, worklist.exit_code, worklist.output)
        self.assertIn("canonical_tracks: 1", imported.output)
        self.assertIn("limit: 250", worklist.output)

    def _insert_complete_phase6(self, canonical_id: str) -> None:
        for index, spec in enumerate(self.bundle.specifications):
            decision, state = ("include", "supported") if spec.logical_playlist_id == "vibe.chill" else ("exclude", "hard_excluded")
            self.connection.execute(
                "INSERT INTO phase6_proposals VALUES (?, ?, ?, ?, ?, .9, ?, ?, ?, 'synthetic.v1', ?, ?, 'needs_review', '{}', '2026-01-01T00:00:00Z')",
                (f"proposal.synthetic.bulk.{index}", canonical_id, spec.logical_playlist_id, decision, state,
                 spec.specification_version, self.bundle.taxonomy.taxonomy_version,
                 self.bundle.vocabulary.vocabulary_version, self.bundle.digest, f"input.synthetic.bulk.{index}"),
            )
        self.connection.commit()


if __name__ == "__main__":
    unittest.main()
