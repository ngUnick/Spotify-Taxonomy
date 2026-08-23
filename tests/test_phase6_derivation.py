from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.config import load_phase4_bundle
from spotify_playlist_taxonomy.domain.models import CanonicalTrack, EvidenceKind, ResearchEvidence, ResearchSource
from spotify_playlist_taxonomy.persistence import SQLiteStateStore, apply_migrations, connect_database, sync_taxonomy
from spotify_playlist_taxonomy.workflows.phase6 import DERIVATION_VERSION, Phase6Service

ROOT = Path(__file__).resolve().parents[1]


def evidence(evidence_id: str, value: object, *, canonical_id: str = "canonical.synthetic.phase6", evidence_type: str = "style_tradition", scope: str = "recording", match: str = "exact", state: str = "active") -> ResearchEvidence:
    return ResearchEvidence(evidence_id, canonical_id, ResearchSource.WIKIDATA, EvidenceKind.SOURCED_FACT,
        json.dumps({"claim": evidence_type, "value": value}), "https://example.invalid/evidence", .9,
        provider_id="wikidata", provider_version="wikidata.v1", request_id="request.synthetic", query_id="query.synthetic",
        subject_fingerprint="a" * 64, subject_scope=scope, subject_match=match, evidence_type=evidence_type,
        claim_key=evidence_type, quality_band="high", normalized_schema_version="research-evidence.v1",
        normalizer_version="evidence-normalizer.v1", retrieved_at="2026-01-01T00:00:00Z", fresh_until="2099-01-01T00:00:00Z",
        evidence_state=state, content_digest="b" * 64)


class Phase6DerivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.connection = connect_database(Path(self.temp.name) / "app.db")
        apply_migrations(self.connection)
        bundle = load_phase4_bundle(ROOT / "config" / "application.yaml")
        sync_taxonomy(self.connection, bundle.taxonomy)
        SQLiteStateStore(self.connection).save_canonical_track(CanonicalTrack("canonical.synthetic.phase6", "Synthetic Aurora", ("Invented Act",)))
        self.store = SQLiteStateStore(self.connection); self.service = Phase6Service(self.connection, bundle)

    def tearDown(self) -> None:
        self.connection.close(); self.temp.cleanup()

    def test_exact_supported_style_derives_and_is_idempotent(self) -> None:
        self.store.save_evidence(evidence("evidence.synthetic.rock", "rock"))
        first = self.service.persist_derivations(self.service.derive("canonical.synthetic.phase6"))
        second = self.service.persist_derivations(self.service.derive("canonical.synthetic.phase6"))
        self.assertEqual(1, len(first)); self.assertEqual("style.traditions", first[0].name)
        self.assertEqual('["rock"]', first[0].value); self.assertEqual(DERIVATION_VERSION, first[0].derivation_version)
        self.assertEqual(1, len(second)); self.assertEqual(1, self.connection.execute("SELECT count(*) FROM phase6_characteristic_derivations").fetchone()[0])

    def test_missing_stale_probable_and_contextual_style_do_not_derive(self) -> None:
        self.store.save_evidence(evidence("evidence.synthetic.stale", "rock", state="stale"))
        self.store.save_evidence(evidence("evidence.synthetic.probable", "rock", match="probable"))
        self.store.save_evidence(evidence("evidence.synthetic.context", "rock", scope="artist"))
        self.assertEqual((), self.service.derive("canonical.synthetic.phase6"))

    def test_derivability_projection_uses_the_same_value_validation_as_derive(self) -> None:
        invalid = "canonical.synthetic.phase6.invalid-version"
        self.store.save_canonical_track(CanonicalTrack(invalid, "Synthetic Invalid", ("Invented Act",)))
        self.store.save_evidence(evidence("evidence.synthetic.invalid-version", "unmapped_version", canonical_id=invalid, evidence_type="version_information"))
        self.store.save_evidence(evidence("evidence.synthetic.valid", "pop"))
        before = self.connection.total_changes
        derivable = self.service.derivable_canonical_ids()
        self.assertIn("canonical.synthetic.phase6", derivable)
        self.assertNotIn(invalid, derivable)
        self.assertEqual(bool(self.service.derive("canonical.synthetic.phase6")), "canonical.synthetic.phase6" in derivable)
        self.assertEqual(bool(self.service.derive(invalid)), invalid in derivable)
        self.assertEqual(before, self.connection.total_changes)

    def test_conflicting_single_value_is_preserved_not_collapsed(self) -> None:
        self.store.save_evidence(evidence("evidence.synthetic.language-a", "greek", evidence_type="lyrical_language", scope="work"))
        self.store.save_evidence(evidence("evidence.synthetic.language-b", "non_greek", evidence_type="lyrical_language", scope="work"))
        items = self.service.persist_derivations(self.service.derive("canonical.synthetic.phase6"))
        self.assertEqual("conflicting", items[0].state)

    def test_generic_evaluator_includes_pop_and_leaves_other_specs_abstained(self) -> None:
        self.store.save_evidence(evidence("evidence.synthetic.pop", "pop"))
        self.service.persist_derivations(self.service.derive("canonical.synthetic.phase6"))
        proposals = {p.logical_playlist_id: p for p in self.service.classify("canonical.synthetic.phase6")}
        self.assertEqual("include", proposals["genre.pop"].decision)
        self.assertEqual("supported", proposals["genre.pop"].state)
        self.assertEqual("abstain", proposals["genre.latin"].decision)
        self.assertEqual(36, len(proposals))

    def test_work_context_is_not_style_and_can_include_soundtracks(self) -> None:
        self.store.save_evidence(evidence("evidence.synthetic.score", "original_score", evidence_type="work_context", scope="work"))
        self.service.persist_derivations(self.service.derive("canonical.synthetic.phase6"))
        proposals = {p.logical_playlist_id: p for p in self.service.classify("canonical.synthetic.phase6")}
        self.assertEqual("include", proposals["genre.soundtracks_scores"].decision)
        self.assertEqual("abstain", proposals["genre.world_traditional"].decision)


if __name__ == "__main__":
    unittest.main()
