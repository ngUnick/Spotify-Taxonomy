import inspect
import json
from pathlib import Path
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.domain.models import (
    AssignmentDecision,
    ClassificationProposal,
    ProposalOrigin,
    ReviewStatus,
)
from spotify_playlist_taxonomy.domain.policies import validate_proposal_references
from spotify_playlist_taxonomy.ports.classification import BatchClassifier


class ClassificationBoundaryTests(unittest.TestCase):
    def test_classifier_port_has_no_spotify_mutation_capability(self) -> None:
        method_names = {
            name
            for name, member in inspect.getmembers(BatchClassifier, inspect.isfunction)
            if not name.startswith("_")
        }
        self.assertEqual({"classify_batch"}, method_names)
        self.assertTrue(method_names.isdisjoint({"add_items", "create_managed_playlist"}))

    def test_proposal_uses_compact_summary_and_evidence_ids(self) -> None:
        item = ClassificationProposal(
            proposal_id="proposal.synthetic.001",
            canonical_track_id="track.synthetic.001",
            logical_playlist_id="vibe.hype",
            decision=AssignmentDecision.INCLUDE,
            confidence=0.82,
            origin=ProposalOrigin.CODEX_CLASSIFIER,
            evidence_ids=("evidence.synthetic.001",),
            characteristic_ids=("characteristic.synthetic.001",),
            justification_summary="High energy and driving rhythm; see evidence.synthetic.001.",
            taxonomy_version="taxonomy.synthetic.v1",
            specification_version="spec.synthetic.v1",
            review_status=ReviewStatus.NEEDS_REVIEW,
        )
        self.assertLessEqual(len(item.justification_summary), 280)
        self.assertEqual(("evidence.synthetic.001",), item.evidence_ids)
        self.assertFalse(hasattr(item, "reasoning"))

    def test_excessive_summary_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "<= 280"):
            ClassificationProposal(
                proposal_id="proposal.synthetic.long",
                canonical_track_id="track.synthetic.001",
                logical_playlist_id="vibe.hype",
                decision=AssignmentDecision.INCLUDE,
                confidence=0.82,
                origin=ProposalOrigin.CODEX_CLASSIFIER,
                evidence_ids=("evidence.synthetic.001",),
                characteristic_ids=(),
                justification_summary="x" * 281,
                taxonomy_version="taxonomy.synthetic.v1",
                specification_version="spec.synthetic.v1",
                review_status=ReviewStatus.PROPOSED,
            )

    def test_unknown_references_fail_validation(self) -> None:
        item = ClassificationProposal(
            proposal_id="proposal.synthetic.unknown",
            canonical_track_id="track.synthetic.001",
            logical_playlist_id="genre.rock",
            decision=AssignmentDecision.INCLUDE,
            confidence=0.9,
            origin=ProposalOrigin.GENRE_MAPPING_RULE,
            evidence_ids=("evidence.synthetic.missing",),
            characteristic_ids=(),
            justification_summary="A synthetic genre tag maps to the broad parent.",
            taxonomy_version="taxonomy.synthetic.v1",
            specification_version="spec.synthetic.v1",
            review_status=ReviewStatus.PROPOSED,
        )
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            validate_proposal_references(
                item,
                known_evidence_ids=set(),
                known_characteristic_ids=set(),
                known_playlist_ids={"genre.rock"},
            )

    def test_fixture_contains_only_invented_data_and_distinct_versions(self) -> None:
        fixture_path = Path(__file__).parent / "fixtures" / "synthetic_classification_batch.json"
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
        self.assertTrue(payload["schema_version"].startswith("phase0.synthetic"))
        self.assertEqual(2, len(payload["tracks"]))
        self.assertNotEqual(
            payload["tracks"][0]["canonical_track_id"],
            payload["tracks"][1]["canonical_track_id"],
        )


if __name__ == "__main__":
    unittest.main()
