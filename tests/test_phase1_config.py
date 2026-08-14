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


class ConfigurationTests(unittest.TestCase):
    def test_repository_taxonomy_has_exact_stable_ids(self) -> None:
        document = load_taxonomy_document(ROOT / "config" / "taxonomy.yaml")
        self.assertEqual("1", document.schema_version)
        self.assertEqual("taxonomy.v1", document.taxonomy_version)
        self.assertEqual(EXPECTED_IDS, {item.logical_id for item in document.playlists})
        self.assertEqual(36, len(document.playlists))

    def test_runtime_root_is_not_an_application_config_field(self) -> None:
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate({
                "schema_version": "1", "taxonomy_path": "taxonomy.yaml",
                "runtime_root": "elsewhere",
            })

    def test_credential_like_unknown_field_fails_strict_schema(self) -> None:
        with self.assertRaises(ValidationError):
            ApplicationConfig.model_validate({
                "schema_version": "1", "taxonomy_path": "taxonomy.yaml",
                "client_secret": "synthetic-not-a-secret",
            })

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

    def test_specification_complexity_is_proportional(self) -> None:
        genre = PlaylistSpecificationDocument.model_validate({
            "schema_version": "1", "specification_version": "genre.synthetic.v1",
            "logical_playlist_id": "genre.rock", "status": "draft",
            "purpose": "Synthetic broad parent genre.",
        })
        activity = PlaylistSpecificationDocument.model_validate({
            "schema_version": "1", "specification_version": "activity.synthetic.v1",
            "logical_playlist_id": "activity.sleeping", "status": "draft",
            "purpose": "Synthetic low-intensity context.",
            "inclusion_criteria": ["Low intensity", "Unobtrusive vocals"],
            "exclusion_criteria": ["Abrupt loud changes"],
            "notes": ["Invented fixture only"],
        })
        self.assertEqual([], genre.inclusion_criteria)
        self.assertEqual(2, len(activity.inclusion_criteria))

    def test_yaml_unknown_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "application.yaml"
            path.write_text(yaml.safe_dump({
                "schema_version": "1", "taxonomy_path": "taxonomy.yaml", "surprise": True,
            }), encoding="utf-8")
            with self.assertRaises(ValidationError):
                load_application_config(path)


if __name__ == "__main__":
    unittest.main()
