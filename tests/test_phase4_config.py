from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401
from typer.testing import CliRunner
import yaml

from spotify_playlist_taxonomy.cli import app
from spotify_playlist_taxonomy.config import (
    Phase4ValidationError,
    load_phase4_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "application.yaml"
RUNNER = CliRunner()


class Phase4ConfigurationTests(unittest.TestCase):
    def copied_config(self, directory: str) -> Path:
        target = Path(directory) / "config"
        shutil.copytree(ROOT / "config", target)
        return target / "application.yaml"

    @staticmethod
    def mutate_yaml(path: Path, mutation) -> None:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutation(payload)
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    @staticmethod
    def resolve_variant(
        policy,
        *,
        lyrical_profile: str | None = None,
        vocal_presence: str = "prominent_lead",
        regional_association: str | None = None,
        project_association: str | None = None,
        collaboration: bool = False,
    ) -> str:
        """Exercise the declarative policy in tests without adding runtime routing."""
        if lyrical_profile is not None:
            outcome = policy.lexical_routes[lyrical_profile].value
            if outcome != "unresolved" or not collaboration:
                return outcome
        if vocal_presence in policy.instrumental_nonlexical.vocal_presence_values:
            if regional_association is not None:
                regional_outcome = policy.instrumental_nonlexical.regional_routes[
                    regional_association
                ].value
                if regional_outcome != "continue":
                    return regional_outcome
            if project_association is not None:
                return policy.instrumental_nonlexical.project_scene_routes[
                    project_association
                ].value
            return policy.instrumental_nonlexical.unresolved_result.value
        if collaboration and project_association is not None:
            return policy.instrumental_nonlexical.project_scene_routes[
                project_association
            ].value
        return policy.unknown_result.value

    @staticmethod
    def concrete_genre_targets(bundle, assignments: set[str], variant: str) -> set[str]:
        return {
            target.output_target_id
            for target in bundle.output_targets.targets
            if target.logical_playlist_id in assignments
            and target.variant_id is not None
            and target.variant_id.value == variant
        }

    @staticmethod
    def indicator_matches(indicator, values: dict[str, object]) -> bool:
        """Evaluate the small declarative subset used by the Genre review tests."""
        for condition in indicator.conditions:
            if condition.characteristic_id not in values:
                return False
            actual = values[condition.characteristic_id]
            expected = set(condition.values)
            if condition.operator.value == "contains_any":
                if not set(actual) & expected:
                    return False
            elif condition.operator.value == "contains_none":
                if set(actual) & expected:
                    return False
            elif condition.operator.value == "one_of":
                if actual not in expected:
                    return False
            elif condition.operator.value == "is":
                if actual is not condition.values[0]:
                    return False
            else:
                raise AssertionError(
                    f"unsupported synthetic operator {condition.operator.value}"
                )
        return True

    @classmethod
    def qualifies_from_strong_indicators(cls, specification, values) -> bool:
        return any(
            cls.indicator_matches(indicator, values)
            for indicator in specification.strong_indicators
        )

    def test_repository_bundle_is_complete_reviewed_and_deterministic(self) -> None:
        first = load_phase4_bundle(CONFIG)
        second = load_phase4_bundle(CONFIG)

        self.assertEqual(36, len(first.taxonomy.playlists))
        self.assertEqual(36, len(first.specifications))
        self.assertEqual(58, len(first.vocabulary.characteristics))
        self.assertEqual(13, len(first.vocabulary.families))
        self.assertEqual(51, len(first.output_targets.targets))
        self.assertEqual(
            "genre-variant-routing.v1",
            first.genre_variant_routing.routing_policy_version,
        )
        self.assertEqual("reviewed", first.manifest.status.value)
        self.assertEqual("reviewed", first.vocabulary.status.value)
        self.assertTrue(
            all(item.status.value == "reviewed" for item in first.specifications)
        )
        self.assertTrue(
            all(
                item.specification_status.value == "reviewed"
                for item in first.taxonomy.playlists
            )
        )
        self.assertEqual(first.digest, second.digest)
        self.assertEqual(
            {item.logical_id for item in first.taxonomy.playlists},
            {item.logical_playlist_id for item in first.specifications},
        )
        self.assertEqual(
            {"genre": 15, "vibe": 10, "activity": 11},
            {
                dimension: sum(
                    item.logical_playlist_id.startswith(f"{dimension}.")
                    for item in first.specifications
                )
                for dimension in ("genre", "vibe", "activity")
            },
        )

    def test_output_targets_preserve_36_semantics_and_expand_only_genres(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        targets = bundle.output_targets.targets
        by_dimension = {
            dimension: [
                item for item in targets
                if item.logical_playlist_id.startswith(f"{dimension}.")
            ]
            for dimension in ("genre", "vibe", "activity")
        }
        self.assertEqual(30, len(by_dimension["genre"]))
        self.assertEqual(10, len(by_dimension["vibe"]))
        self.assertEqual(11, len(by_dimension["activity"]))
        for logical_id in (
            item.logical_id for item in bundle.taxonomy.playlists
            if item.dimension.value == "genre"
        ):
            self.assertEqual(
                {"greek", "foreign"},
                {
                    item.variant_id.value
                    for item in targets
                    if item.logical_playlist_id == logical_id
                    and item.variant_id is not None
                },
            )
        self.assertTrue(
            all(item.variant_id is None for item in by_dimension["vibe"])
        )
        self.assertTrue(
            all(item.variant_id is None for item in by_dimension["activity"])
        )

    def test_hybrid_genre_variant_policy_routes_synthetic_cases(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        policy = bundle.genre_variant_routing
        cases = (
            ("greek_artist_english_lyrics", {"lyrical_profile": "non_greek", "project_association": "greek"}, "foreign"),
            ("foreign_artist_greek_lyrics", {"lyrical_profile": "greek", "project_association": "non_greek"}, "greek"),
            ("greek_dominant_mixed", {"lyrical_profile": "mixed_greek_dominant"}, "greek"),
            ("foreign_dominant_mixed", {"lyrical_profile": "mixed_non_greek_dominant"}, "foreign"),
            ("balanced_mixed", {"lyrical_profile": "mixed_balanced"}, "unresolved"),
            ("greek_traditional_instrumental", {"vocal_presence": "instrumental", "regional_association": "greek", "project_association": "non_greek"}, "greek"),
            ("greek_led_instrumental", {"vocal_presence": "instrumental", "project_association": "greek"}, "greek"),
            ("greek_led_non_greek_regional_instrumental", {"vocal_presence": "instrumental", "regional_association": "non_greek", "project_association": "greek"}, "greek"),
            ("foreign_led_instrumental", {"vocal_presence": "instrumental", "project_association": "non_greek"}, "foreign"),
            ("ambiguous_instrumental", {"vocal_presence": "instrumental", "regional_association": "mixed_or_ambiguous", "project_association": "greek"}, "unresolved"),
            ("co_equal_collaboration", {"lyrical_profile": "mixed_balanced", "project_association": "mixed_or_ambiguous", "collaboration": True}, "unresolved"),
        )
        for label, inputs, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(expected, self.resolve_variant(policy, **inputs))

        version_routes = {
            "canonical.synthetic.greek_version": self.resolve_variant(
                policy, lyrical_profile="greek"
            ),
            "canonical.synthetic.foreign_version": self.resolve_variant(
                policy, lyrical_profile="non_greek"
            ),
        }
        self.assertEqual("greek", version_routes["canonical.synthetic.greek_version"])
        self.assertEqual(
            "foreign", version_routes["canonical.synthetic.foreign_version"]
        )

    def test_variant_routing_combines_with_independent_genre_memberships(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        greek_assignments = {"genre.pop", "genre.electronic"}
        foreign_assignments = {"genre.hip_hop_rap", "genre.rnb_soul"}
        self.assertEqual(
            {
                "output.genre.pop.greek",
                "output.genre.electronic.greek",
            },
            self.concrete_genre_targets(bundle, greek_assignments, "greek"),
        )
        self.assertEqual(
            {
                "output.genre.hip_hop_rap.foreign",
                "output.genre.rnb_soul.foreign",
            },
            self.concrete_genre_targets(bundle, foreign_assignments, "foreign"),
        )
        self.assertEqual(
            greek_assignments,
            {
                target.logical_playlist_id
                for target in bundle.output_targets.targets
                if target.output_target_id
                in self.concrete_genre_targets(bundle, greek_assignments, "greek")
            },
        )

        for specification in bundle.specifications:
            if not specification.logical_playlist_id.startswith("genre."):
                continue
            characteristic_ids = {
                condition.characteristic_id
                for indicator in (
                    specification.strong_indicators
                    + specification.supporting_indicators
                    + specification.negative_indicators
                    + specification.hard_exclusions
                )
                for condition in indicator.conditions
            }
            self.assertNotIn(
                "vocals_lyrics.lyrical_language_profile", characteristic_ids
            )
            self.assertFalse(
                any(item.startswith("cultural_context.") for item in characteristic_ids)
            )

    def test_vocabulary_declares_unknown_and_contradiction_semantics(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        for definition in bundle.vocabulary.characteristics:
            self.assertEqual("absent_means_unknown", definition.missing_semantics)
            self.assertEqual("preserve_assertions", definition.contradiction_policy)
            self.assertTrue(definition.provenance_expectations.evidence_required)

    def test_approved_boundaries_are_explicit(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specifications = {
            item.logical_playlist_id: item for item in bundle.specifications
        }
        chill_instrumental = specifications["vibe.chill_instrumental"]
        self.assertIn(
            "vibe.chill__vibe.chill_instrumental",
            chill_instrumental.overlap_refs,
        )
        self.assertTrue(chill_instrumental.hard_exclusions)
        self.assertIn(
            "genre.latin__genre.world_traditional",
            specifications["genre.latin"].overlap_refs,
        )
        soundtrack_conditions = [
            condition
            for indicator in specifications["genre.soundtracks_scores"].strong_indicators
            for condition in indicator.conditions
        ]
        self.assertTrue(
            any(
                condition.characteristic_id == "work_context.function"
                and "soundtrack_compilation_appearance" not in condition.values
                for condition in soundtrack_conditions
            )
        )

    def test_folk_acoustic_requires_lineage_or_contextual_acoustic_idiom(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specification = next(
            item for item in bundle.specifications
            if item.logical_playlist_id == "genre.folk_acoustic"
        )
        acoustic_balance_only = {
            "arrangement.acoustic_electronic_balance": "fully_acoustic",
        }
        self.assertFalse(
            self.qualifies_from_strong_indicators(
                specification, acoustic_balance_only
            )
        )
        self.assertTrue(
            self.qualifies_from_strong_indicators(
                specification, {"style.traditions": ["folk"]}
            )
        )
        self.assertTrue(
            self.qualifies_from_strong_indicators(
                specification,
                {
                    "arrangement.acoustic_electronic_balance": "mostly_acoustic",
                    "arrangement.substantive_acoustic_idiom": True,
                },
            )
        )
        for adjacent_style in ("western_classical", "jazz", "pop"):
            with self.subTest(adjacent_style=adjacent_style):
                self.assertFalse(
                    self.qualifies_from_strong_indicators(
                        specification,
                        {
                            "style.traditions": [adjacent_style],
                            **acoustic_balance_only,
                        },
                    )
                )

        definition = next(
            item for item in bundle.vocabulary.characteristics
            if item.characteristic_id == "arrangement.substantive_acoustic_idiom"
        )
        self.assertEqual("arrangement", definition.family_id)
        self.assertEqual("boolean", definition.value_schema.type.value)
        self.assertEqual("may_vary", definition.version_sensitivity.value)
        self.assertEqual("absent_means_unknown", definition.missing_semantics)

    def test_reggae_ska_support_is_balanced_but_never_decisive(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specification = next(
            item for item in bundle.specifications
            if item.logical_playlist_id == "genre.reggae_ska"
        )
        self.assertTrue(
            self.qualifies_from_strong_indicators(
                specification, {"style.traditions": ["reggae"]}
            )
        )
        self.assertTrue(
            self.qualifies_from_strong_indicators(
                specification, {"style.traditions": ["ska"]}
            )
        )
        for supporting_only in (
            {"arrangement.instrumentation": ["brass_led"]},
            {"tempo_rhythm.perceived_speed": "high"},
        ):
            self.assertFalse(
                self.qualifies_from_strong_indicators(
                    specification, supporting_only
                )
            )
        supporting_conditions = [
            condition
            for indicator in specification.supporting_indicators
            for condition in indicator.conditions
        ]
        self.assertTrue(
            any(
                condition.characteristic_id == "arrangement.instrumentation"
                and "brass_led" in condition.values
                for condition in supporting_conditions
            )
        )
        self.assertTrue(
            any(
                condition.characteristic_id == "tempo_rhythm.perceived_speed"
                and condition.operator.value == "at_least"
                and condition.values == ["high"]
                for condition in supporting_conditions
            )
        )

    def test_soundtracks_use_work_context_and_guard_compilation_negatives(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specifications = {
            item.logical_playlist_id: item for item in bundle.specifications
        }
        soundtracks = specifications["genre.soundtracks_scores"]
        classical = specifications["genre.classical"]
        self.assertEqual("work_context", soundtracks.dimension_basis.value)
        negative = soundtracks.negative_indicators[0]

        for work_specific_function in ("original_score", "work_specific_song"):
            values = {
                "work_context.function": [
                    work_specific_function,
                    "soundtrack_compilation_appearance",
                ]
            }
            self.assertTrue(
                self.qualifies_from_strong_indicators(soundtracks, values)
            )
            self.assertFalse(self.indicator_matches(negative, values))

        compilation_only = {
            "work_context.function": ["soundtrack_compilation_appearance"]
        }
        self.assertFalse(
            self.qualifies_from_strong_indicators(soundtracks, compilation_only)
        )
        self.assertTrue(self.indicator_matches(negative, compilation_only))

        pre_existing_placement = {
            "work_context.function": [
                "standalone_song",
                "soundtrack_compilation_appearance",
            ]
        }
        self.assertFalse(
            self.qualifies_from_strong_indicators(
                soundtracks, pre_existing_placement
            )
        )
        self.assertTrue(self.indicator_matches(negative, pre_existing_placement))

        classical_score = {
            "work_context.function": ["original_score"],
            "style.traditions": ["western_classical"],
        }
        self.assertTrue(
            self.qualifies_from_strong_indicators(soundtracks, classical_score)
        )
        self.assertTrue(
            self.qualifies_from_strong_indicators(classical, classical_score)
        )

    def test_human_reviewed_vibe_calibrations_are_explicit(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specifications = {
            item.logical_playlist_id: item for item in bundle.specifications
        }

        def conditions(playlist_id: str, group: str):
            return [
                condition
                for indicator in getattr(specifications[playlist_id], group)
                for condition in indicator.conditions
            ]

        chill = conditions("vibe.chill", "strong_indicators")
        chill_instrumental = conditions(
            "vibe.chill_instrumental", "strong_indicators"
        )
        for definition in (chill, chill_instrumental):
            self.assertTrue(
                any(
                    item.characteristic_id == "affect_mood.relaxation"
                    and item.values == ["high"]
                    for item in definition
                )
            )
        self.assertTrue(
            any(
                item.characteristic_id == "vocals_lyrics.vocal_presence"
                for item in chill_instrumental
            )
        )

        uplifting_routes = specifications["vibe.uplifting_feel_good"].strong_indicators
        self.assertTrue(
            any(
                any(item.values == ["mixed"] for item in route.conditions)
                and any(item.values == ["very_high"] for item in route.conditions)
                for route in uplifting_routes
            )
        )

        euphoric_routes = specifications["vibe.euphoric_energetic"].strong_indicators
        self.assertEqual(2, len(euphoric_routes))
        for route in euphoric_routes:
            self.assertTrue(any(item.values == ["very_high"] for item in route.conditions))
            quality = next(
                item for item in route.conditions
                if item.characteristic_id == "affect_mood.qualities"
            )
            self.assertNotIn("joyful", quality.values)

        hype_routes = specifications["vibe.hype"].strong_indicators
        self.assertEqual(2, len(hype_routes))
        route_characteristics = [
            {item.characteristic_id for item in route.conditions}
            for route in hype_routes
        ]
        self.assertTrue(all("energy_dynamics.intensity" in item for item in route_characteristics))
        self.assertEqual(
            {"functional.personal_activation", "functional.social_activation"},
            {
                item
                for route in route_characteristics
                for item in route
                if item.endswith("activation")
            },
        )

        dreamy_routes = specifications["vibe.dreamy_atmospheric"].strong_indicators
        self.assertEqual(3, len(dreamy_routes))
        for route in dreamy_routes:
            characteristic_ids = {item.characteristic_id for item in route.conditions}
            qualities = next(
                item for item in route.conditions
                if item.characteristic_id == "atmosphere_space.qualities"
            )
            if set(qualities.values) & {"spacious", "immersive"}:
                self.assertGreater(len(characteristic_ids), 2)

        romantic = specifications["vibe.romantic"]
        strong_theme_values = {
            value
            for item in conditions("vibe.romantic", "strong_indicators")
            if item.characteristic_id == "vocals_lyrics.themes"
            for value in item.values
        }
        self.assertNotIn("romance", strong_theme_values)
        self.assertNotIn("heartbreak", strong_theme_values)
        self.assertFalse(romantic.hard_exclusions)

        for playlist_id in (
            "vibe.chill", "vibe.chill_instrumental", "vibe.uplifting_feel_good",
            "vibe.euphoric_energetic", "vibe.hype", "vibe.motivational",
            "vibe.dark_sad_melancholic", "vibe.emotional_nostalgic",
            "vibe.dreamy_atmospheric", "vibe.romantic",
        ):
            self.assertEqual("reviewed", specifications[playlist_id].status.value)

    def test_human_reviewed_activity_calibrations_are_explicit(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        specifications = {
            item.logical_playlist_id: item for item in bundle.specifications
        }

        def route_map(route):
            return {
                condition.characteristic_id: condition
                for condition in route.conditions
            }

        driving_routes = specifications["activity.driving"].strong_indicators
        self.assertEqual(2, len(driving_routes))
        self.assertEqual(
            ["medium"],
            route_map(driving_routes[0])[
                "structure_attention.attention_demand"
            ].values,
        )
        self.assertEqual(
            ["high"],
            route_map(driving_routes[1])[
                "structure_attention.attention_demand"
            ].values,
        )
        driving_negative = {
            value
            for indicator in specifications["activity.driving"].negative_indicators
            for condition in indicator.conditions
            if condition.characteristic_id == "structure_attention.attention_demand"
            for value in condition.values
        }
        self.assertEqual({"high", "very_high"}, driving_negative)
        self.assertFalse(specifications["activity.driving"].hard_exclusions)

        night_routes = specifications["activity.night_driving"].strong_indicators
        self.assertEqual(3, len(night_routes))
        self.assertTrue(
            all(
                "functional.sustained_momentum" in route_map(route)
                for route in night_routes
            )
        )
        self.assertIn(
            "nocturnal",
            route_map(night_routes[0])["atmosphere_space.qualities"].values,
        )
        for route in night_routes[1:]:
            mapped = route_map(route)
            self.assertEqual(
                ["high"],
                mapped["atmosphere_space.atmospheric_prominence"].values,
            )
            self.assertEqual(
                ["medium"],
                mapped["structure_attention.attention_demand"].values,
            )
            self.assertTrue(
                {
                    "atmosphere_space.spatial_openness",
                    "timbre_production.bass_weight",
                }
                & mapped.keys()
            )
        self.assertIn(
            "activity.night_driving__vibe.chill__vibe.dark_sad_melancholic__vibe.dreamy_atmospheric",
            specifications["activity.night_driving"].overlap_refs,
        )

        gaming_routes = specifications["activity.gaming"].strong_indicators
        self.assertEqual(3, len(gaming_routes))
        gaming_route_ids = [set(route_map(route)) for route in gaming_routes]
        self.assertTrue(
            all("functional.interactive_engagement" in ids for ids in gaming_route_ids)
        )
        self.assertIn("atmosphere_space.qualities", gaming_route_ids[0])
        self.assertTrue(
            {
                "functional.movement_support",
                "functional.personal_activation",
            }.issubset(gaming_route_ids[1])
        )
        self.assertTrue(
            {
                "functional.focus_support",
                "structure_attention.distraction_risk",
            }.issubset(gaming_route_ids[2])
        )
        self.assertIn(
            "activity.gaming__activity.background_music__vibe.euphoric_energetic",
            specifications["activity.gaming"].overlap_refs,
        )

        sleeping = specifications["activity.sleeping"]
        sleeping_negative_ids = {
            condition.characteristic_id
            for indicator in sleeping.negative_indicators
            for condition in indicator.conditions
        }
        self.assertTrue(
            {
                "affect_mood.emotional_intensity",
                "affect_mood.tension",
            }.issubset(sleeping_negative_ids)
        )
        self.assertEqual(1, len(sleeping.hard_exclusions))
        self.assertEqual(
            {"functional.sleep_disruption_risk"},
            {
                condition.characteristic_id
                for condition in sleeping.hard_exclusions[0].conditions
            },
        )

        party_routes = specifications["activity.party"].strong_indicators
        self.assertEqual(2, len(party_routes))
        communal_route = route_map(party_routes[1])
        self.assertEqual(
            ["very_high"], communal_route["functional.social_activation"].values
        )
        self.assertIn("functional.communal_participation", communal_route)
        self.assertNotIn("tempo_rhythm.dance_impulse", communal_route)

        preserved_strong_axes = {
            "activity.gym": {
                "functional.exercise_activation", "energy_dynamics.intensity"
            },
            "activity.running": {
                "functional.pacing_compatibility",
                "tempo_rhythm.rhythmic_stability",
                "functional.sustained_momentum",
            },
            "activity.studying": {
                "functional.focus_support", "structure_attention.distraction_risk"
            },
            "activity.coding_focus": {
                "functional.focus_support", "functional.sustained_momentum"
            },
            "activity.background_music": {
                "functional.background_compatibility",
                "structure_attention.attention_demand",
            },
            "activity.road_trip": {
                "functional.travel_endurance", "functional.sustained_momentum"
            },
        }
        for playlist_id, expected in preserved_strong_axes.items():
            self.assertEqual(
                expected,
                set(route_map(specifications[playlist_id].strong_indicators[0])),
            )

        for playlist_id in (
            "activity.driving", "activity.night_driving", "activity.gym",
            "activity.running", "activity.studying", "activity.coding_focus",
            "activity.gaming", "activity.sleeping", "activity.party",
            "activity.background_music", "activity.road_trip",
        ):
            self.assertEqual("reviewed", specifications[playlist_id].status.value)

    def test_bundle_has_no_assignment_or_provider_signal_fields(self) -> None:
        bundle = load_phase4_bundle(CONFIG)
        serialized = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (
                bundle.vocabulary_path,
                bundle.manifest_path,
                bundle.overlap_path,
                bundle.output_targets_path,
                bundle.genre_variant_routing_path,
                *bundle.specification_paths,
            )
        ).casefold()
        for forbidden in (
            "playlist_assignment",
            "spotify_audio_feature",
            "provider_track_id",
            "canonical_track_id",
        ):
            self.assertNotIn(forbidden, serialized)
        routing_sources = (
            bundle.genre_variant_routing_path.read_text(encoding="utf-8")
            + bundle.output_targets_path.read_text(encoding="utf-8")
        ).casefold()
        for forbidden in ("spotify", "provider_track", "platform_track"):
            self.assertNotIn(forbidden, routing_sources)

    def test_source_paths_must_remain_inside_config_root(self) -> None:
        for field in (
            "taxonomy_path",
            "characteristic_vocabulary_path",
            "specification_manifest_path",
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                config = self.copied_config(directory)
                self.mutate_yaml(
                    config,
                    lambda payload: payload.update({field: "../outside.yaml"}),
                )
                with self.assertRaisesRegex(Phase4ValidationError, "escapes"):
                    load_phase4_bundle(config)

        for field in ("output_targets_path", "genre_variant_routing_path"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                config = self.copied_config(directory)
                manifest = config.parent / "specifications" / "manifest.yaml"
                self.mutate_yaml(
                    manifest,
                    lambda payload: payload.update({field: "../outside.yaml"}),
                )
                with self.assertRaisesRegex(Phase4ValidationError, "escapes"):
                    load_phase4_bundle(config)

    def test_manifest_and_overlap_sets_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            manifest = config.parent / "specifications" / "manifest.yaml"
            self.mutate_yaml(manifest, lambda payload: payload["specifications"].pop())
            with self.assertRaisesRegex(Phase4ValidationError, "exactly every"):
                load_phase4_bundle(config)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            overlaps = config.parent / "specifications" / "overlaps.yaml"
            self.mutate_yaml(overlaps, lambda payload: payload["overlaps"].pop())
            with self.assertRaisesRegex(Phase4ValidationError, "required overlap"):
                load_phase4_bundle(config)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            overlaps = config.parent / "specifications" / "overlaps.yaml"

            def use_concrete_target(payload) -> None:
                payload["overlaps"][0]["playlist_ids"][0] = "output.vibe.chill"

            self.mutate_yaml(overlaps, use_concrete_target)
            with self.assertRaisesRegex(Phase4ValidationError, "semantic playlist IDs"):
                load_phase4_bundle(config)

    def test_output_target_registry_and_routing_policy_fail_closed(self) -> None:
        mutations = (
            (
                "output-targets.yaml",
                lambda payload: payload["targets"].pop(),
                "exactly 51",
            ),
            (
                "output-targets.yaml",
                lambda payload: payload["targets"][30].update(
                    {"variant_id": "greek", "output_target_id": "output.vibe.chill.greek"}
                ),
                "unsplit",
            ),
            (
                "genre-variant-routing.yaml",
                lambda payload: payload["lexical_routes"].update(
                    {"mixed_balanced": "foreign"}
                ),
                "lexical routing",
            ),
            (
                "genre-variant-routing.yaml",
                lambda payload: payload.update({"unknown_result": "foreign"}),
                "Input should be",
            ),
        )
        for filename, mutation, message in mutations:
            with self.subTest(filename=filename, message=message), tempfile.TemporaryDirectory() as directory:
                config = self.copied_config(directory)
                self.mutate_yaml(config.parent / filename, mutation)
                with self.assertRaisesRegex(ValueError, message):
                    load_phase4_bundle(config)

    def test_output_and_routing_documents_affect_bundle_digest(self) -> None:
        baseline = load_phase4_bundle(CONFIG).digest
        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            self.mutate_yaml(
                config.parent / "output-targets.yaml",
                lambda payload: payload["targets"][0].update(
                    {"display_name": "Synthetic Greek Pop Label"}
                ),
            )
            self.assertNotEqual(baseline, load_phase4_bundle(config).digest)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            self.mutate_yaml(
                config.parent / "genre-variant-routing.yaml",
                lambda payload: payload.update(
                    {"routing_policy_version": "genre-variant-routing.v2"}
                ),
            )
            self.mutate_yaml(
                config.parent / "specifications" / "manifest.yaml",
                lambda payload: payload.update(
                    {"genre_variant_routing_policy_version": "genre-variant-routing.v2"}
                ),
            )
            self.assertNotEqual(baseline, load_phase4_bundle(config).digest)

    def test_unknown_characteristic_operator_and_value_fail_closed(self) -> None:
        cases = (
            (
                lambda condition: condition.update(
                    {"characteristic_id": "synthetic.unknown"}
                ),
                "unknown characteristic",
            ),
            (lambda condition: condition.update({"operator": "is"}), "invalid"),
            (lambda condition: condition.update({"values": ["synthetic_unknown"]}), "unknown value"),
        )
        for mutation, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as directory:
                config = self.copied_config(directory)
                specification = config.parent / "specifications" / "genre.pop.yaml"

                def mutate(payload) -> None:
                    mutation(payload["strong_indicators"][0]["conditions"][0])

                self.mutate_yaml(specification, mutate)
                with self.assertRaisesRegex(Phase4ValidationError, message):
                    load_phase4_bundle(config)

    def test_wrong_dimension_and_duplicate_condition_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            specification = config.parent / "specifications" / "genre.pop.yaml"
            self.mutate_yaml(
                specification,
                lambda payload: payload.update({"dimension_basis": "functional_context"}),
            )
            with self.assertRaisesRegex(Phase4ValidationError, "wrong dimension"):
                load_phase4_bundle(config)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            specification = config.parent / "specifications" / "genre.pop.yaml"
            self.mutate_yaml(
                specification,
                lambda payload: payload.update({"dimension_basis": "work_context"}),
            )
            with self.assertRaisesRegex(Phase4ValidationError, "wrong dimension"):
                load_phase4_bundle(config)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            specification = (
                config.parent / "specifications" / "genre.soundtracks_scores.yaml"
            )
            self.mutate_yaml(
                specification,
                lambda payload: payload.update({"dimension_basis": "musical_style"}),
            )
            with self.assertRaisesRegex(Phase4ValidationError, "wrong dimension"):
                load_phase4_bundle(config)

        with tempfile.TemporaryDirectory() as directory:
            config = self.copied_config(directory)
            specification = config.parent / "specifications" / "genre.pop.yaml"

            def duplicate(payload) -> None:
                conditions = payload["strong_indicators"][0]["conditions"]
                conditions.append(dict(conditions[0]))

            self.mutate_yaml(specification, duplicate)
            with self.assertRaisesRegex(Phase4ValidationError, "repeats a condition"):
                load_phase4_bundle(config)

    def test_config_validate_is_local_and_needs_no_spotify_service(self) -> None:
        with patch(
            "spotify_playlist_taxonomy.cli._spotify_http_client"
        ) as spotify_client, patch(
            "spotify_playlist_taxonomy.cli.SpotifyReadOnlyAdapter"
        ) as spotify_reader:
            result = RUNNER.invoke(app, ["config", "validate", "--config", str(CONFIG)])

        self.assertEqual(0, result.exit_code, result.output)
        self.assertIn("playlists=36", result.output)
        self.assertIn("characteristics=58", result.output)
        self.assertIn("specifications=36", result.output)
        self.assertIn("output_targets=51", result.output)
        self.assertIn("routing=genre-variant-routing.v1", result.output)
        spotify_client.assert_not_called()
        spotify_reader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
