from dataclasses import fields
import unittest

import _bootstrap  # noqa: F401

from spotify_playlist_taxonomy.domain.models import (
    CharacteristicOrigin,
    DerivedCharacteristic,
    IdentityProvider,
    ProposalOrigin,
    ResearchSource,
)


class ProvenanceInvariantTests(unittest.TestCase):
    def test_provenance_domains_are_separate(self) -> None:
        self.assertNotIn("codex", {item.value for item in ResearchSource})
        self.assertNotIn("derived_rule", {item.value for item in ResearchSource})
        self.assertIn("spotify", {item.value for item in IdentityProvider})
        self.assertNotIn("spotify", {item.value for item in ResearchSource})
        self.assertIn("codex_interpretation", {item.value for item in CharacteristicOrigin})
        self.assertIn("codex_classifier", {item.value for item in ProposalOrigin})

    def test_characteristics_reference_evidence_not_proposals(self) -> None:
        field_names = {field.name for field in fields(DerivedCharacteristic)}
        self.assertIn("evidence_ids", field_names)
        self.assertNotIn("proposal_ids", field_names)

    def test_generated_characteristic_requires_evidence(self) -> None:
        with self.assertRaisesRegex(ValueError, "must reference evidence"):
            DerivedCharacteristic(
                characteristic_id="characteristic.synthetic.none",
                canonical_track_id="track.synthetic.001",
                name="energy",
                value="high",
                origin=CharacteristicOrigin.CODEX_INTERPRETATION,
                evidence_ids=(),
                confidence=0.8,
                derivation_version="derive.synthetic.v1",
                justification_summary="No supporting evidence exists.",
            )


if __name__ == "__main__":
    unittest.main()
