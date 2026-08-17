from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import _bootstrap  # noqa:F401

from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase6 import Phase6Service
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase8 import Phase8ImportError, Phase8Service
from spotify_playlist_taxonomy.workflows.phase8_inbound import RESULT_SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]


class Phase8ImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.connection = connect_database(self.root / "app.db")
        apply_migrations(self.connection)
        self.bundle = load_phase4_bundle(ROOT / "config" / "application.yaml")
        sync_taxonomy(self.connection, self.bundle.taxonomy)
        self.canonical_id = "canonical.synthetic.phase8"
        store = SQLiteStateStore(self.connection)
        store.save_canonical_track(CanonicalTrack(self.canonical_id, "Synthetic Signal", ("Invented Act",)))
        store.save_evidence(ResearchEvidence(
            "evidence.synthetic.phase8", self.canonical_id, ResearchSource.WIKIDATA,
            EvidenceKind.SOURCED_FACT, json.dumps({"claim": "style_tradition", "value": "pop"}),
            "https://example.invalid/evidence", .9, provider_id="wikidata", provider_version="v1",
            request_id="request.synthetic", query_id="query.synthetic", subject_fingerprint="a" * 64,
            subject_scope="recording", subject_match="exact", evidence_type="style_tradition",
            claim_key="style_tradition", quality_band="high", normalized_schema_version="v1",
            normalizer_version="v1", retrieved_at="2026-01-01T00:00:00Z",
            fresh_until="2099-01-01T00:00:00Z", content_digest="b" * 64,
        ))
        phase6 = Phase6Service(self.connection, self.bundle)
        phase6.persist_derivations(phase6.derive(self.canonical_id))
        phase6.classify(self.canonical_id)
        self.service = Phase8Service(self.connection, self.bundle, self.root / "outbound")
        self.batch_id, self.outbound_path = self.service.create((self.canonical_id,))

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary.cleanup()

    def _result_path(self, *, decision: str = "include") -> Path:
        batch = self.connection.execute("SELECT * FROM codex_batches WHERE batch_id=?", (self.batch_id,)).fetchone()
        outbound = json.loads(self.outbound_path.read_text(encoding="utf-8"))
        item = outbound["items"][0]
        result = {
            "result_schema_version": RESULT_SCHEMA_VERSION,
            "batch_id": self.batch_id,
            "outbound_batch_digest": batch["outbound_digest"],
            "taxonomy_version": batch["taxonomy_version"],
            "specification_bundle_digest": batch["bundle_digest"],
            "items": [{
                "item_id": item["item_id"],
                "classifications": [{
                    "semantic_specification_id": "genre.pop",
                    "decision": decision,
                    "justification_summary": "Synthetic normalized evidence supports this semantic result.",
                    "evidence_ids": ["evidence.synthetic.phase8"],
                    "characteristic_ids": [item["characteristics"][0]["characteristic_id"]],
                }],
            }],
        }
        path = self.root / "result.json"
        path.write_text(json.dumps(result), encoding="utf-8")
        return path

    def test_valid_import_preserves_deterministic_proposal_and_provenance(self) -> None:
        outcome = self.service.import_result(self._result_path())
        self.assertEqual("imported", outcome.state)
        self.assertFalse(outcome.idempotent)
        self.assertEqual(36, self.connection.execute("SELECT count(*) FROM phase6_proposals").fetchone()[0])
        proposal = self.connection.execute("SELECT * FROM phase8_codex_proposals").fetchone()
        self.assertEqual(self.batch_id, proposal["batch_id"])
        self.assertEqual("include", proposal["decision"])
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM phase8_codex_proposal_evidence").fetchone()[0])
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM phase8_codex_proposal_characteristics").fetchone()[0])
        self.assertEqual("imported", self.connection.execute("SELECT state FROM codex_batches").fetchone()[0])

    def test_identical_replay_is_idempotent_and_conflicting_replay_fails_closed(self) -> None:
        path = self._result_path()
        self.service.import_result(path)
        replay = self.service.import_result(path)
        self.assertTrue(replay.idempotent)
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM phase8_codex_proposals").fetchone()[0])
        with self.assertRaises(Phase8ImportError):
            self.service.import_result(self._result_path(decision="exclude"))
        self.assertEqual(1, self.connection.execute("SELECT count(*) FROM phase8_codex_proposals").fetchone()[0])

    def test_invalid_result_and_persistence_failure_leave_no_partial_state(self) -> None:
        path = self._result_path()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["items"][0]["classifications"][0]["evidence_ids"] = ["evidence.unknown"]
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(Phase8ImportError):
            self.service.import_result(path)
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM codex_batch_imports").fetchone()[0])
        self.assertEqual("awaiting_result", self.connection.execute("SELECT state FROM codex_batches").fetchone()[0])

        self.connection.execute("CREATE TRIGGER fail_phase8 BEFORE INSERT ON phase8_codex_proposals BEGIN SELECT RAISE(FAIL, 'synthetic'); END")
        with self.assertRaises(Phase8ImportError):
            self.service.import_result(self._result_path())
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM codex_batch_imports").fetchone()[0])
        self.assertEqual(0, self.connection.execute("SELECT count(*) FROM phase8_codex_proposals").fetchone()[0])
        self.assertEqual("awaiting_result", self.connection.execute("SELECT state FROM codex_batches").fetchone()[0])

    def test_incompatible_current_bundle_marks_unimported_batch_stale(self) -> None:
        stale_service = Phase8Service(self.connection, replace(self.bundle, digest="bundle.changed"), self.root / "outbound")
        with self.assertRaises(Phase8ImportError):
            stale_service.import_result(self._result_path())
        self.assertEqual("stale", self.connection.execute("SELECT state FROM codex_batches").fetchone()[0])

    def test_phase7_preserves_agreement_and_surfaces_disagreement_after_clear(self) -> None:
        self.service.import_result(self._result_path(decision="include"))
        review = Phase7Service(self.connection, self.bundle)
        agreed = review.effective(self.canonical_id, "genre.pop")
        self.assertEqual("include", agreed.generated)
        self.assertEqual(("codex", "deterministic"), agreed.generated_sources)

        self.connection.execute("DELETE FROM phase8_codex_proposal_evidence")
        self.connection.execute("DELETE FROM phase8_codex_proposal_characteristics")
        self.connection.execute("DELETE FROM phase8_codex_proposals")
        self.connection.execute("DELETE FROM codex_batch_imports")
        self.connection.execute("UPDATE codex_batches SET state='awaiting_result'")
        self.connection.commit()
        self.service.import_result(self._result_path(decision="exclude"))
        disagreement = review.effective(self.canonical_id, "genre.pop")
        self.assertTrue(disagreement.generated_disagreement)
        review.decide(self.canonical_id, "genre.pop", __import__("spotify_playlist_taxonomy.domain.models", fromlist=["AssignmentDecision"]).AssignmentDecision.INCLUDE)
        self.assertEqual("include", review.effective(self.canonical_id, "genre.pop").effective)
        cleared = review.clear(self.canonical_id, "genre.pop")
        self.assertIsNone(cleared.effective)
        self.assertTrue(cleared.generated_disagreement)


if __name__ == "__main__":
    unittest.main()
