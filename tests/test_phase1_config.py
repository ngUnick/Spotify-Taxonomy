from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401
import yaml
from pydantic import ValidationError

from spotify_playlist_taxonomy.config import (
    ApplicationConfig,
    PlaylistSpecificationDocument,
    TaxonomyDocument,
    load_application_config,
    load_taxonomy_document,
)

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_IDS = {
    "genre.pop", "genre.rock", "genre.metal", "genre.hip_hop_rap", "genre.electronic",
    "genre.rnb_soul", "genre.jazz", "genre.blues", "genre.classical",
    "genre.folk_acoustic", "genre.country", "genre.reggae_ska", "genre.latin",
    "genre.world_traditional", "genre.soundtracks_scores", "vibe.chill",
    "vibe.chill_instrumental", "vibe.uplifting_feel_good", "vibe.euphoric_energetic",
    "vibe.hype", "vibe.motivational", "vibe.dark_sad_melancholic",
    "vibe.emotional_nostalgic", "vibe.dreamy_atmospheric", "vibe.romantic",
    "activity.driving", "activity.night_driving", "activity.gym", "activity.running",
    "activity.studying", "activity.coding_focus", "activity.gaming",
    "activity.background_music", "activity.sleeping", "activity.party", "activity.road_trip",
}


def application_payload(**extra: object) -> dict[str, object]:
    return {
        "schema_version": "3",
        "taxonomy_path": "taxonomy.yaml",
        "characteristic_vocabulary_path": "characteristics.yaml",
        "specification_manifest_path": "specifications/manifest.yaml",
        **extra,
    }


class ConfigurationTests(unittest.TestCase):
    def test_repository_taxonomy_has_exact_stable_ids(self) -> None:
        document = load_taxonomy_document(ROOT / "config" / "taxonomy.yaml")
        self.assertEqual("1", document.schema_version)
        self.assertEqual("taxonomy.v1", document.taxonomy_version)
        self.assertEqual(EXPECTED_IDS, {item.logical_id for item in document.playlists})
        self.assertEqual(36, len(document.playlists))

    def test_runtime_root_is_not_an_application_config_field(self) -> None:
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate(application_payload(runtime_root="elsewhere"))

    def test_credential_like_unknown_field_fails_strict_schema(self) -> None:
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate(
                application_payload(client_secret="synthetic-not-a-secret")
            )

    def test_duplicate_ids_and_prefix_mismatch_are_rejected(self) -> None:
        entry = {
            "logical_id": "genre.pop", "dimension": "genre", "display_name": "Pop",
            "enabled": True, "specification_status": "draft",
        }
        with self.assertRaises(ValidationError):
            TaxonomyDocument.model_validate({
                "schema_version": "1", "taxonomy_version": "synthetic.v1",
                "playlists": [entry, entry],
            })
        with self.assertRaises(ValidationError):
            TaxonomyDocument.model_validate({
                "schema_version": "1", "taxonomy_version": "synthetic.v1",
                "playlists": [{**entry, "dimension": "vibe"}],
            })

    def test_phase4_specification_requires_structured_review_guidance(self) -> None:
        indicator = {
            "indicator_id": "strong.001",
            "summary": "Synthetic indicator",
            "match": "all",
            "conditions": [{
                "characteristic_id": "synthetic.level",
                "operator": "equals",
                "values": ["medium"],
            }],
            "rationale": "Synthetic rationale.",
        }
        document = PlaylistSpecificationDocument.model_validate({
            "schema_version": "2",
            "specification_version": "genre.synthetic.v1",
            "logical_playlist_id": "genre.synthetic",
            "status": "draft",
            "characteristic_vocabulary_version": "characteristics.v1",
            "purpose": "Synthetic broad parent genre.",
            "dimension_basis": "musical_style",
            "strong_indicators": [indicator],
            "supporting_indicators": [{**indicator, "indicator_id": "supporting.001"}],
            "negative_indicators": [{**indicator, "indicator_id": "negative.001"}],
            "ambiguity_guidance": {
                "insufficient_evidence": "abstain",
                "borderline_notes": ["Keep uncertain cases unassigned."],
            },
            "version_guidance": ["Assess the exact version."],
            "instrumental_vocal_guidance": ["Assess vocals independently."],
        })
        self.assertEqual("abstain", document.ambiguity_guidance.insufficient_evidence)
        self.assertEqual("synthetic.level", document.strong_indicators[0].conditions[0].characteristic_id)

    def test_yaml_unknown_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.yaml"
            path.write_text(
                yaml.safe_dump(application_payload(surprise=True)), encoding="utf-8"
            )
            with self.assertRaises(ValidationError):
                load_application_config(path)


if __name__ == "__main__":
    unittest.main()
