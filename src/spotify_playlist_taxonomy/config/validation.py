"""Cross-document validation for the source-controlled Phase 4 taxonomy bundle."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .models import (
    ApplicationConfig,
    CharacteristicCondition,
    CharacteristicDefinition,
    CharacteristicValueType,
    CharacteristicVocabularyDocument,
    ConditionOperator,
    DimensionBasis,
    GenreVariantRoutingPolicyDocument,
    OverlapGuidanceDocument,
    OutputTargetRegistryDocument,
    OutputVariant,
    PlaylistDimension,
    PlaylistSpecificationDocument,
    RoutingOutcome,
    SpecificationManifestDocument,
    TaxonomyDocument,
    document_digest,
    load_application_config,
    load_characteristic_vocabulary_document,
    load_genre_variant_routing_policy_document,
    load_overlap_guidance_document,
    load_output_target_registry_document,
    load_specification_document,
    load_specification_manifest_document,
    load_taxonomy_document,
)


class Phase4ValidationError(ValueError):
    pass


REQUIRED_OVERLAP_IDS = {
    "vibe.chill__vibe.chill_instrumental",
    "vibe.chill__vibe.dreamy_atmospheric",
    "vibe.chill__vibe.emotional_nostalgic",
    "vibe.chill__vibe.dark_sad_melancholic",
    "vibe.uplifting_feel_good__vibe.euphoric_energetic__vibe.hype__vibe.motivational",
    "vibe.dark_sad_melancholic__vibe.emotional_nostalgic",
    "vibe.dark_sad_melancholic__vibe.emotional_nostalgic__vibe.romantic",
    "activity.driving__activity.night_driving__activity.road_trip",
    "activity.night_driving__activity.gaming__vibe.dreamy_atmospheric",
    "activity.night_driving__vibe.chill__vibe.dark_sad_melancholic__vibe.dreamy_atmospheric",
    "activity.gym__activity.running",
    "activity.studying__activity.coding_focus__activity.background_music",
    "activity.gaming__activity.coding_focus__vibe.hype__vibe.dreamy_atmospheric",
    "activity.gaming__activity.background_music__vibe.euphoric_energetic",
    "activity.party__vibe.hype__vibe.euphoric_energetic",
    "activity.party__activity.road_trip__vibe.uplifting_feel_good",
    "activity.sleeping__activity.background_music__vibe.chill__vibe.dreamy_atmospheric",
    "genre.pop__genre.rock__genre.electronic",
    "genre.folk_acoustic__genre.country",
    "genre.rnb_soul__genre.pop__genre.hip_hop_rap",
    "genre.latin__genre.world_traditional",
    "genre.soundtracks_scores__genre.style_overlap",
}


@dataclass(frozen=True, slots=True)
class Phase4Bundle:
    config: ApplicationConfig
    taxonomy: TaxonomyDocument
    vocabulary: CharacteristicVocabularyDocument
    manifest: SpecificationManifestDocument
    overlaps: OverlapGuidanceDocument
    output_targets: OutputTargetRegistryDocument
    genre_variant_routing: GenreVariantRoutingPolicyDocument
    specifications: tuple[PlaylistSpecificationDocument, ...]
    taxonomy_path: Path
    vocabulary_path: Path
    manifest_path: Path
    overlap_path: Path
    output_targets_path: Path
    genre_variant_routing_path: Path
    specification_paths: tuple[Path, ...]
    digest: str


def _resolve_source_path(root: Path, path: Path, *, label: str) -> Path:
    if path.is_absolute():
        raise Phase4ValidationError(f"{label} must be a relative source-controlled path")
    resolved = (root / path).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise Phase4ValidationError(f"{label} escapes the configuration root")
    if not resolved.is_file():
        raise Phase4ValidationError(f"{label} does not exist: {resolved}")
    return resolved


def _normalized_token(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _validate_aliases(definition: CharacteristicDefinition) -> None:
    allowed = {item.value for item in definition.value_schema.allowed_values}
    seen: set[str] = set()
    for alias, target in definition.normalization.aliases.items():
        normalized = _normalized_token(alias)
        if not normalized or normalized in seen:
            raise Phase4ValidationError(
                f"{definition.characteristic_id} has blank or duplicate normalized aliases"
            )
        seen.add(normalized)
        if target not in allowed:
            raise Phase4ValidationError(
                f"{definition.characteristic_id} alias targets unknown value {target}"
            )


def _validate_condition(
    condition: CharacteristicCondition,
    definitions: dict[str, CharacteristicDefinition],
) -> None:
    definition = definitions.get(condition.characteristic_id)
    if definition is None:
        raise Phase4ValidationError(
            f"condition references unknown characteristic {condition.characteristic_id}"
        )
    value_type = definition.value_schema.type
    allowed_operators = {
        CharacteristicValueType.BOOLEAN: {ConditionOperator.IS},
        CharacteristicValueType.ENUM: {
            ConditionOperator.EQUALS,
            ConditionOperator.ONE_OF,
            ConditionOperator.NOT_ONE_OF,
        },
        CharacteristicValueType.MULTI_ENUM: {
            ConditionOperator.CONTAINS_ANY,
            ConditionOperator.CONTAINS_ALL,
            ConditionOperator.CONTAINS_NONE,
        },
        CharacteristicValueType.ORDINAL: {
            ConditionOperator.EQUALS,
            ConditionOperator.ONE_OF,
            ConditionOperator.NOT_ONE_OF,
            ConditionOperator.AT_LEAST,
            ConditionOperator.AT_MOST,
            ConditionOperator.BETWEEN,
        },
        CharacteristicValueType.INTEGER: {
            ConditionOperator.EQUALS,
            ConditionOperator.AT_LEAST,
            ConditionOperator.AT_MOST,
            ConditionOperator.BETWEEN,
        },
        CharacteristicValueType.DECIMAL: {
            ConditionOperator.EQUALS,
            ConditionOperator.AT_LEAST,
            ConditionOperator.AT_MOST,
            ConditionOperator.BETWEEN,
        },
    }[value_type]
    if condition.operator not in allowed_operators:
        raise Phase4ValidationError(
            f"operator {condition.operator.value} is invalid for "
            f"{condition.characteristic_id} ({value_type.value})"
        )
    count = len(condition.values)
    if condition.operator is ConditionOperator.BETWEEN and count != 2:
        raise Phase4ValidationError("between requires exactly two values")
    if condition.operator in {
        ConditionOperator.EQUALS,
        ConditionOperator.AT_LEAST,
        ConditionOperator.AT_MOST,
        ConditionOperator.IS,
    } and count != 1:
        raise Phase4ValidationError(f"{condition.operator.value} requires exactly one value")

    schema = definition.value_schema
    if value_type is CharacteristicValueType.BOOLEAN:
        if not isinstance(condition.values[0], bool):
            raise Phase4ValidationError("boolean conditions require a boolean value")
        return
    if value_type in {
        CharacteristicValueType.ENUM,
        CharacteristicValueType.MULTI_ENUM,
        CharacteristicValueType.ORDINAL,
    }:
        allowed = {item.value for item in schema.allowed_values}
        if any(not isinstance(value, str) or value not in allowed for value in condition.values):
            raise Phase4ValidationError(
                f"condition for {condition.characteristic_id} uses an unknown value"
            )
        return
    for value in condition.values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Phase4ValidationError("numeric conditions require numeric values")
        if value_type is CharacteristicValueType.INTEGER and not isinstance(value, int):
            raise Phase4ValidationError("integer conditions require integer values")
        assert schema.minimum is not None and schema.maximum is not None
        if not schema.minimum <= value <= schema.maximum:
            raise Phase4ValidationError(
                f"condition for {condition.characteristic_id} is outside its range"
            )
    if condition.operator is ConditionOperator.BETWEEN:
        if float(condition.values[0]) > float(condition.values[1]):
            raise Phase4ValidationError("between bounds must be increasing")


def _condition_key(condition: CharacteristicCondition) -> str:
    return json.dumps(condition.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _validate_specification(
    specification: PlaylistSpecificationDocument,
    *,
    taxonomy_entry: object,
    definitions: dict[str, CharacteristicDefinition],
) -> None:
    if specification.logical_playlist_id == "genre.soundtracks_scores":
        expected_basis = DimensionBasis.WORK_CONTEXT
    else:
        expected_basis = {
            PlaylistDimension.GENRE: DimensionBasis.MUSICAL_STYLE,
            PlaylistDimension.VIBE: DimensionBasis.SUBJECTIVE_CHARACTER,
            PlaylistDimension.ACTIVITY: DimensionBasis.FUNCTIONAL_CONTEXT,
        }[taxonomy_entry.dimension]
    if specification.dimension_basis is not expected_basis:
        raise Phase4ValidationError(
            f"{specification.logical_playlist_id} uses the wrong dimension basis"
        )
    version_pattern = rf"^spec\.{re.escape(specification.logical_playlist_id)}\.v[1-9][0-9]*$"
    if re.fullmatch(version_pattern, specification.specification_version) is None:
        raise Phase4ValidationError(
            f"{specification.logical_playlist_id} has an invalid specification_version"
        )
    groups = (
        specification.strong_indicators,
        specification.supporting_indicators,
        specification.negative_indicators,
        specification.hard_exclusions,
    )
    for group in groups:
        for indicator in group:
            keys = [_condition_key(condition) for condition in indicator.conditions]
            if len(keys) != len(set(keys)):
                raise Phase4ValidationError(
                    f"{specification.logical_playlist_id} repeats a condition within an indicator"
                )
            for condition in indicator.conditions:
                _validate_condition(condition, definitions)

    positive = {
        _condition_key(condition)
        for indicator in specification.strong_indicators + specification.supporting_indicators
        for condition in indicator.conditions
    }
    hard_negative = {
        _condition_key(condition)
        for indicator in specification.hard_exclusions
        for condition in indicator.conditions
    }
    if positive & hard_negative:
        raise Phase4ValidationError(
            f"{specification.logical_playlist_id} uses the same condition positively and as a hard exclusion"
        )

    strong_families = {
        definitions[condition.characteristic_id].family_id
        for indicator in specification.strong_indicators
        for condition in indicator.conditions
    }
    required = {
        PlaylistDimension.GENRE: {"style", "work_context"},
        PlaylistDimension.VIBE: {"affect_mood", "energy_dynamics", "atmosphere_space"},
        PlaylistDimension.ACTIVITY: {"functional", "tempo_rhythm", "structure_attention"},
    }[taxonomy_entry.dimension]
    if not strong_families & required:
        raise Phase4ValidationError(
            f"{specification.logical_playlist_id} lacks a dimension-appropriate strong indicator"
        )


def _bundle_digest(components: dict[str, str]) -> str:
    payload = json.dumps(components, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_output_targets(
    registry: OutputTargetRegistryDocument,
    taxonomy: TaxonomyDocument,
) -> None:
    if len(registry.targets) != 51:
        raise Phase4ValidationError("output target registry must contain exactly 51 targets")
    taxonomy_entries = {item.logical_id: item for item in taxonomy.playlists}
    referenced = {item.logical_playlist_id for item in registry.targets}
    unknown = referenced - set(taxonomy_entries)
    if unknown:
        raise Phase4ValidationError("output targets reference unknown semantic playlists")
    if referenced != set(taxonomy_entries):
        raise Phase4ValidationError("every semantic playlist must have an output target")

    for logical_id, taxonomy_entry in taxonomy_entries.items():
        targets = [
            item for item in registry.targets if item.logical_playlist_id == logical_id
        ]
        variants = {item.variant_id for item in targets}
        if taxonomy_entry.dimension is PlaylistDimension.GENRE:
            if len(targets) != 2 or variants != {
                OutputVariant.GREEK,
                OutputVariant.FOREIGN,
            }:
                raise Phase4ValidationError(
                    f"{logical_id} must have exactly greek and foreign output targets"
                )
        elif len(targets) != 1 or variants != {None}:
            raise Phase4ValidationError(
                f"{logical_id} must have exactly one unsplit output target"
            )


def _allowed_values(definition: CharacteristicDefinition) -> set[str]:
    return {item.value for item in definition.value_schema.allowed_values}


def _validate_genre_variant_routing(
    policy: GenreVariantRoutingPolicyDocument,
    definitions: dict[str, CharacteristicDefinition],
) -> None:
    references = policy.characteristic_references
    expected_references = {
        "vocal_presence": "vocals_lyrics.vocal_presence",
        "lyrical_language_profile": "vocals_lyrics.lyrical_language_profile",
        "primary_project_scene_association": (
            "cultural_context.primary_project_scene_association"
        ),
        "regional_musical_association": (
            "cultural_context.regional_musical_association"
        ),
    }
    if references.model_dump(mode="json") != expected_references:
        raise Phase4ValidationError(
            "Genre variant routing must use the approved platform-neutral characteristics"
        )
    for characteristic_id in expected_references.values():
        definition = definitions.get(characteristic_id)
        if definition is None:
            raise Phase4ValidationError(
                f"routing policy references unknown characteristic {characteristic_id}"
            )
        if definition.value_schema.type is not CharacteristicValueType.ENUM:
            raise Phase4ValidationError("routing characteristics must use enum values")

    language_values = _allowed_values(
        definitions[expected_references["lyrical_language_profile"]]
    )
    expected_lexical_routes = {
        "greek": RoutingOutcome.GREEK,
        "non_greek": RoutingOutcome.FOREIGN,
        "mixed_greek_dominant": RoutingOutcome.GREEK,
        "mixed_non_greek_dominant": RoutingOutcome.FOREIGN,
        "mixed_balanced": RoutingOutcome.UNRESOLVED,
    }
    if language_values != set(expected_lexical_routes):
        raise Phase4ValidationError("lyrical language profile values are incomplete")
    if policy.lexical_routes != expected_lexical_routes:
        raise Phase4ValidationError("lexical routing does not match the approved policy")

    vocal_values = _allowed_values(
        definitions[expected_references["vocal_presence"]]
    )
    instrumental = policy.instrumental_nonlexical
    if set(instrumental.vocal_presence_values) != {"instrumental", "non_lexical"}:
        raise Phase4ValidationError(
            "instrumental routing must be limited to non-lexical recordings"
        )
    if not set(instrumental.vocal_presence_values) <= vocal_values:
        raise Phase4ValidationError("instrumental routing uses unknown vocal values")

    regional_routes = {
        "greek": RoutingOutcome.GREEK,
        "non_greek": RoutingOutcome.CONTINUE,
        "mixed_or_ambiguous": RoutingOutcome.UNRESOLVED,
    }
    project_routes = {
        "greek": RoutingOutcome.GREEK,
        "non_greek": RoutingOutcome.FOREIGN,
        "mixed_or_ambiguous": RoutingOutcome.UNRESOLVED,
    }
    for reference_name, routes, expected_routes in (
        (
            "regional_musical_association",
            instrumental.regional_routes,
            regional_routes,
        ),
        (
            "primary_project_scene_association",
            instrumental.project_scene_routes,
            project_routes,
        ),
    ):
        characteristic_id = expected_references[reference_name]
        if _allowed_values(definitions[characteristic_id]) != set(expected_routes):
            raise Phase4ValidationError(
                f"{characteristic_id} values are incomplete for routing"
            )
        if routes != expected_routes:
            raise Phase4ValidationError(
                f"{reference_name} routing does not match the approved policy"
            )


def load_phase4_bundle(config_path: Path) -> Phase4Bundle:
    config_path = config_path.resolve()
    config_root = config_path.parent.resolve()
    config = load_application_config(config_path)
    taxonomy_path = _resolve_source_path(
        config_root, config.taxonomy_path, label="taxonomy"
    )
    taxonomy = load_taxonomy_document(taxonomy_path)
    vocabulary_path = _resolve_source_path(
        config_root,
        config.characteristic_vocabulary_path,
        label="characteristic vocabulary",
    )
    manifest_path = _resolve_source_path(
        config_root,
        config.specification_manifest_path,
        label="specification manifest",
    )
    vocabulary = load_characteristic_vocabulary_document(vocabulary_path)
    manifest = load_specification_manifest_document(manifest_path)
    overlap_path = _resolve_source_path(
        config_root, manifest.overlap_path, label="overlap guidance"
    )
    overlaps = load_overlap_guidance_document(overlap_path)
    output_targets_path = _resolve_source_path(
        config_root, manifest.output_targets_path, label="output target registry"
    )
    output_targets = load_output_target_registry_document(output_targets_path)
    genre_variant_routing_path = _resolve_source_path(
        config_root,
        manifest.genre_variant_routing_path,
        label="Genre variant routing policy",
    )
    genre_variant_routing = load_genre_variant_routing_policy_document(
        genre_variant_routing_path
    )

    specification_paths = tuple(
        _resolve_source_path(config_root, item.path, label="playlist specification")
        for item in manifest.specifications
    )
    specifications = tuple(load_specification_document(path) for path in specification_paths)

    taxonomy_entries = {item.logical_id: item for item in taxonomy.playlists}
    manifest_ids = {item.logical_playlist_id for item in manifest.specifications}
    specification_ids = {item.logical_playlist_id for item in specifications}
    if manifest.taxonomy_version != taxonomy.taxonomy_version:
        raise Phase4ValidationError("manifest taxonomy_version does not match taxonomy")
    if manifest.characteristic_vocabulary_version != vocabulary.vocabulary_version:
        raise Phase4ValidationError("manifest vocabulary version does not match vocabulary")
    if manifest.output_target_set_version != output_targets.output_target_set_version:
        raise Phase4ValidationError(
            "manifest output-target version does not match the registry"
        )
    if (
        manifest.genre_variant_routing_policy_version
        != genre_variant_routing.routing_policy_version
    ):
        raise Phase4ValidationError(
            "manifest routing-policy version does not match the routing document"
        )
    if manifest_ids != set(taxonomy_entries):
        raise Phase4ValidationError("manifest must reference exactly every taxonomy playlist")
    if specification_ids != manifest_ids or len(specifications) != len(manifest_ids):
        raise Phase4ValidationError("specification files must map one-to-one to the manifest")
    for reference, specification in zip(manifest.specifications, specifications, strict=True):
        if reference.logical_playlist_id != specification.logical_playlist_id:
            raise Phase4ValidationError("manifest reference does not match specification content")
        if specification.characteristic_vocabulary_version != vocabulary.vocabulary_version:
            raise Phase4ValidationError("specification vocabulary version mismatch")
        if specification.status is not manifest.status:
            raise Phase4ValidationError("specification status must match manifest status")
        if taxonomy_entries[specification.logical_playlist_id].specification_status is not specification.status:
            raise Phase4ValidationError("taxonomy and specification status must match")

    definitions = {item.characteristic_id: item for item in vocabulary.characteristics}
    for definition in vocabulary.characteristics:
        _validate_aliases(definition)
    for specification in specifications:
        _validate_specification(
            specification,
            taxonomy_entry=taxonomy_entries[specification.logical_playlist_id],
            definitions=definitions,
        )
    _validate_output_targets(output_targets, taxonomy)
    _validate_genre_variant_routing(genre_variant_routing, definitions)

    if overlaps.specification_set_version != manifest.specification_set_version:
        raise Phase4ValidationError("overlap specification set version mismatch")
    overlap_ids = {item.overlap_id for item in overlaps.overlaps}
    missing_overlap_groups = REQUIRED_OVERLAP_IDS - overlap_ids
    if missing_overlap_groups:
        raise Phase4ValidationError(
            "required overlap guidance is missing: " + ", ".join(sorted(missing_overlap_groups))
        )
    for overlap in overlaps.overlaps:
        if any(item.startswith("output.") for item in overlap.playlist_ids):
            raise Phase4ValidationError(
                "overlap guidance must reference semantic playlist IDs, not output targets"
            )
        unknown_playlists = set(overlap.playlist_ids) - set(taxonomy_entries)
        if unknown_playlists:
            raise Phase4ValidationError("overlap references unknown playlists")
        unknown_characteristics = set(overlap.distinguishing_characteristics) - set(definitions)
        if unknown_characteristics:
            raise Phase4ValidationError("overlap references unknown characteristics")
        for playlist_id in overlap.playlist_ids:
            specification = next(
                item for item in specifications if item.logical_playlist_id == playlist_id
            )
            if overlap.overlap_id not in specification.overlap_refs:
                raise Phase4ValidationError(
                    f"{playlist_id} does not reference overlap {overlap.overlap_id}"
                )
    for specification in specifications:
        unknown = set(specification.overlap_refs) - overlap_ids
        if unknown:
            raise Phase4ValidationError(
                f"{specification.logical_playlist_id} references unknown overlap guidance"
            )

    component_digests = {
        "taxonomy": document_digest(taxonomy),
        "vocabulary": document_digest(vocabulary),
        "manifest": document_digest(manifest),
        "overlaps": document_digest(overlaps),
        "output_targets": document_digest(output_targets),
        "genre_variant_routing": document_digest(genre_variant_routing),
    }
    component_digests.update(
        {
            f"specification:{item.logical_playlist_id}": document_digest(item)
            for item in specifications
        }
    )
    return Phase4Bundle(
        config=config,
        taxonomy=taxonomy,
        vocabulary=vocabulary,
        manifest=manifest,
        overlaps=overlaps,
        output_targets=output_targets,
        genre_variant_routing=genre_variant_routing,
        specifications=specifications,
        taxonomy_path=taxonomy_path,
        vocabulary_path=vocabulary_path,
        manifest_path=manifest_path,
        overlap_path=overlap_path,
        output_targets_path=output_targets_path,
        genre_variant_routing_path=genre_variant_routing_path,
        specification_paths=specification_paths,
        digest=_bundle_digest(component_digests),
    )
