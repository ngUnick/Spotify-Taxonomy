"""Pydantic schemas for repository and local non-secret configuration."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Literal, TypeVar

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    # YAML represents paths and string-valued enums as strings.  Keep the
    # document shape strict while allowing Pydantic to parse those declared
    # field types from their normal YAML representation.
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class PlaylistDimension(StrEnum):
    GENRE = "genre"
    VIBE = "vibe"
    ACTIVITY = "activity"


class SpecificationStatus(StrEnum):
    DRAFT = "draft"
    REVIEWED = "reviewed"
    RETIRED = "retired"


class SpotifyRuntimeConfig(StrictModel):
    """Non-secret Spotify transport settings."""

    request_timeout_seconds: float = Field(default=20, ge=1, le=120)
    max_read_attempts: int = Field(default=3, ge=1, le=10)
    callback_timeout_seconds: float = Field(default=300, ge=30, le=900)
    callback_port: int = Field(default=8888, ge=1024, le=65535, strict=True)


class ApplicationConfig(StrictModel):
    schema_version: Literal["3"]
    taxonomy_path: Path
    characteristic_vocabulary_path: Path
    specification_manifest_path: Path
    database_filename: str = "app.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    file_logging: bool = False
    backup_retention_count: int = Field(default=5, ge=1, le=100)
    spotify: SpotifyRuntimeConfig = Field(default_factory=SpotifyRuntimeConfig)

    @field_validator("database_filename")
    @classmethod
    def validate_database_filename(cls, value: str) -> str:
        if not value.strip() or Path(value).name != value:
            raise ValueError("database_filename must be a nonblank filename")
        return value


class TaxonomyPlaylistEntry(StrictModel):
    logical_id: str = Field(min_length=1)
    dimension: PlaylistDimension
    display_name: str = Field(min_length=1)
    enabled: bool
    specification_status: SpecificationStatus

    @field_validator("logical_id", "display_name")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @model_validator(mode="after")
    def validate_prefix(self) -> "TaxonomyPlaylistEntry":
        if not self.logical_id.startswith(f"{self.dimension.value}."):
            raise ValueError("logical_id prefix must match dimension")
        return self


class TaxonomyDocument(StrictModel):
    schema_version: Literal["1"]
    taxonomy_version: str = Field(min_length=1)
    playlists: list[TaxonomyPlaylistEntry] = Field(min_length=1)

    @field_validator("taxonomy_version")
    @classmethod
    def reject_blank_version(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("taxonomy_version must not be blank")
        return value

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "TaxonomyDocument":
        ids = [item.logical_id for item in self.playlists]
        if len(ids) != len(set(ids)):
            raise ValueError("logical playlist IDs must be unique")
        return self


class CharacteristicValueType(StrEnum):
    BOOLEAN = "boolean"
    ENUM = "enum"
    MULTI_ENUM = "multi_enum"
    ORDINAL = "ordinal"
    INTEGER = "integer"
    DECIMAL = "decimal"


class CharacteristicEpistemicRole(StrEnum):
    NORMALIZED_FACT = "normalized_fact"
    DERIVED_JUDGMENT = "derived_judgment"
    EITHER = "either"


class VersionSensitivity(StrEnum):
    INVARIANT = "invariant"
    MAY_VARY = "may_vary"
    VERSION_DEFINING = "version_defining"


class EvidenceKindValue(StrEnum):
    SOURCED_FACT = "sourced_fact"
    COMMUNITY_TAG = "community_tag"
    EDITORIAL_DESCRIPTION = "editorial_description"
    USER_ASSERTION = "user_assertion"


class CharacteristicFamilyDefinition(StrictModel):
    family_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    description: str = Field(min_length=1)

    @field_validator("display_name", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value


class AllowedCharacteristicValue(StrictModel):
    value: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    rank: int | None = Field(default=None, ge=1)


class CharacteristicValueSchema(StrictModel):
    type: CharacteristicValueType
    allowed_values: list[AllowedCharacteristicValue] = Field(default_factory=list)
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = Field(default=None, gt=0)
    unit: str | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "CharacteristicValueSchema":
        categorical = {
            CharacteristicValueType.ENUM,
            CharacteristicValueType.MULTI_ENUM,
            CharacteristicValueType.ORDINAL,
        }
        numeric = {CharacteristicValueType.INTEGER, CharacteristicValueType.DECIMAL}
        values = [item.value for item in self.allowed_values]
        if len(values) != len(set(values)):
            raise ValueError("allowed characteristic values must be unique")
        if self.type in categorical:
            if len(self.allowed_values) < 2:
                raise ValueError("categorical characteristics require at least two values")
            if any(value is not None for value in (self.minimum, self.maximum, self.step, self.unit)):
                raise ValueError("categorical characteristics cannot declare numeric fields")
            ranks = [item.rank for item in self.allowed_values]
            if self.type is CharacteristicValueType.ORDINAL:
                if ranks != list(range(1, len(ranks) + 1)):
                    raise ValueError("ordinal ranks must be contiguous and ordered from one")
            elif any(rank is not None for rank in ranks):
                raise ValueError("only ordinal values may declare ranks")
        elif self.type in numeric:
            if self.allowed_values:
                raise ValueError("numeric characteristics cannot declare allowed values")
            if self.minimum is None or self.maximum is None or self.minimum >= self.maximum:
                raise ValueError("numeric characteristics require an increasing minimum and maximum")
        else:
            if self.allowed_values or any(
                value is not None for value in (self.minimum, self.maximum, self.step, self.unit)
            ):
                raise ValueError("boolean characteristics cannot declare values or numeric fields")
        return self


class CharacteristicNormalization(StrictModel):
    aliases: dict[str, str] = Field(default_factory=dict)


class ProvenanceExpectations(StrictModel):
    evidence_required: bool
    allowed_evidence_kinds: list[EvidenceKindValue] = Field(min_length=1)

    @field_validator("allowed_evidence_kinds")
    @classmethod
    def reject_duplicate_kinds(
        cls, values: list[EvidenceKindValue]
    ) -> list[EvidenceKindValue]:
        if len(values) != len(set(values)):
            raise ValueError("allowed evidence kinds must be unique")
        return values


class CharacteristicDefinition(StrictModel):
    characteristic_id: str = Field(
        pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"
    )
    family_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    display_name: str = Field(min_length=1)
    definition: str = Field(min_length=1)
    epistemic_role: CharacteristicEpistemicRole
    version_sensitivity: VersionSensitivity
    value_schema: CharacteristicValueSchema
    normalization: CharacteristicNormalization = Field(
        default_factory=CharacteristicNormalization
    )
    provenance_expectations: ProvenanceExpectations
    missing_semantics: Literal["absent_means_unknown"]
    contradiction_policy: Literal["preserve_assertions"]

    @model_validator(mode="after")
    def validate_family_prefix(self) -> "CharacteristicDefinition":
        if not self.characteristic_id.startswith(f"{self.family_id}."):
            raise ValueError("characteristic_id prefix must match family_id")
        if not self.display_name.strip() or not self.definition.strip():
            raise ValueError("characteristic text must not be blank")
        return self


class CharacteristicVocabularyDocument(StrictModel):
    schema_version: Literal["1"]
    vocabulary_version: str = Field(pattern=r"^characteristics\.v[1-9][0-9]*$")
    status: SpecificationStatus
    families: list[CharacteristicFamilyDefinition] = Field(min_length=1)
    characteristics: list[CharacteristicDefinition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_registry(self) -> "CharacteristicVocabularyDocument":
        family_ids = [item.family_id for item in self.families]
        characteristic_ids = [item.characteristic_id for item in self.characteristics]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("characteristic family IDs must be unique")
        if len(characteristic_ids) != len(set(characteristic_ids)):
            raise ValueError("characteristic IDs must be unique")
        unknown = {item.family_id for item in self.characteristics} - set(family_ids)
        if unknown:
            raise ValueError("characteristics reference unknown families")
        return self


class SpecificationReference(StrictModel):
    logical_playlist_id: str = Field(
        pattern=r"^(genre|vibe|activity)\.[a-z][a-z0-9_]*$"
    )
    path: Path


class SpecificationManifestDocument(StrictModel):
    schema_version: Literal["1"]
    specification_set_version: str = Field(pattern=r"^specifications\.v[1-9][0-9]*$")
    taxonomy_version: str = Field(min_length=1)
    characteristic_vocabulary_version: str = Field(min_length=1)
    output_target_set_version: str = Field(pattern=r"^output-targets\.v[1-9][0-9]*$")
    genre_variant_routing_policy_version: str = Field(
        pattern=r"^genre-variant-routing\.v[1-9][0-9]*$"
    )
    status: SpecificationStatus
    overlap_path: Path
    output_targets_path: Path
    genre_variant_routing_path: Path
    specifications: list[SpecificationReference] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_references(self) -> "SpecificationManifestDocument":
        ids = [item.logical_playlist_id for item in self.specifications]
        paths = [item.path.as_posix().casefold() for item in self.specifications]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest logical playlist IDs must be unique")
        if len(paths) != len(set(paths)):
            raise ValueError("manifest specification paths must be unique")
        return self


class OutputVariant(StrEnum):
    GREEK = "greek"
    FOREIGN = "foreign"


class OutputTarget(StrictModel):
    output_target_id: str = Field(
        pattern=r"^output\.(genre|vibe|activity)\.[a-z][a-z0-9_]*(\.(greek|foreign))?$"
    )
    logical_playlist_id: str = Field(
        pattern=r"^(genre|vibe|activity)\.[a-z][a-z0-9_]*$"
    )
    variant_id: OutputVariant | None = None
    display_name: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_identity_shape(self) -> "OutputTarget":
        if not self.display_name.strip():
            raise ValueError("output target display name must not be blank")
        base_id = f"output.{self.logical_playlist_id}"
        if self.logical_playlist_id.startswith("genre."):
            if self.variant_id is None:
                raise ValueError("Genre output targets require a variant_id")
            if self.output_target_id != f"{base_id}.{self.variant_id.value}":
                raise ValueError("Genre output target ID must include its variant")
        else:
            if self.variant_id is not None:
                raise ValueError("Vibe and Activity output targets must remain unsplit")
            if self.output_target_id != base_id:
                raise ValueError("unsplit output target ID must match its semantic ID")
        return self


class OutputTargetRegistryDocument(StrictModel):
    schema_version: Literal["1"]
    output_target_set_version: str = Field(pattern=r"^output-targets\.v[1-9][0-9]*$")
    targets: list[OutputTarget] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_targets(self) -> "OutputTargetRegistryDocument":
        ids = [item.output_target_id for item in self.targets]
        keys = [(item.logical_playlist_id, item.variant_id) for item in self.targets]
        if len(ids) != len(set(ids)):
            raise ValueError("output target IDs must be unique")
        if len(keys) != len(set(keys)):
            raise ValueError("semantic playlist/variant target pairs must be unique")
        return self


class RoutingOutcome(StrEnum):
    GREEK = "greek"
    FOREIGN = "foreign"
    UNRESOLVED = "unresolved"
    CONTINUE = "continue"


class RoutingStage(StrEnum):
    LYRICAL_LANGUAGE = "lyrical_language"
    REGIONAL_MUSICAL_ASSOCIATION = "regional_musical_association"
    PRIMARY_PROJECT_SCENE_ASSOCIATION = "primary_project_scene_association"
    UNRESOLVED = "unresolved"


class RoutingCharacteristicReferences(StrictModel):
    vocal_presence: str = Field(min_length=1)
    lyrical_language_profile: str = Field(min_length=1)
    primary_project_scene_association: str = Field(min_length=1)
    regional_musical_association: str = Field(min_length=1)


class InstrumentalRoutingPolicy(StrictModel):
    vocal_presence_values: list[str] = Field(min_length=1)
    regional_routes: dict[str, RoutingOutcome]
    project_scene_routes: dict[str, RoutingOutcome]
    unresolved_result: Literal[RoutingOutcome.UNRESOLVED]

    @field_validator("vocal_presence_values")
    @classmethod
    def reject_duplicate_vocal_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("instrumental vocal-presence values must be unique")
        return values


class CollaborationRoutingPolicy(StrictModel):
    lyrical_language_first: Literal[True]
    fallback_to_primary_project_scene: Literal[True]
    billing_order_authoritative: Literal[False]
    co_equal_result: Literal[RoutingOutcome.UNRESOLVED]


class GenreVariantRoutingPolicyDocument(StrictModel):
    schema_version: Literal["1"]
    routing_policy_version: str = Field(
        pattern=r"^genre-variant-routing\.v[1-9][0-9]*$"
    )
    applies_to_dimension: Literal[PlaylistDimension.GENRE]
    valid_variants: list[OutputVariant]
    characteristic_references: RoutingCharacteristicReferences
    precedence: list[RoutingStage]
    lexical_routes: dict[str, RoutingOutcome]
    instrumental_nonlexical: InstrumentalRoutingPolicy
    collaboration: CollaborationRoutingPolicy
    unknown_result: Literal[RoutingOutcome.UNRESOLVED]
    non_greek_scope: Literal["all_non_greek_languages_and_contexts"]
    version_scope: Literal["exact_canonical_recording"]

    @model_validator(mode="after")
    def validate_closed_policy(self) -> "GenreVariantRoutingPolicyDocument":
        if self.valid_variants != [OutputVariant.GREEK, OutputVariant.FOREIGN]:
            raise ValueError("routing variants must be exactly greek then foreign")
        expected_precedence = [
            RoutingStage.LYRICAL_LANGUAGE,
            RoutingStage.REGIONAL_MUSICAL_ASSOCIATION,
            RoutingStage.PRIMARY_PROJECT_SCENE_ASSOCIATION,
            RoutingStage.UNRESOLVED,
        ]
        if self.precedence != expected_precedence:
            raise ValueError("routing precedence does not match the approved hybrid policy")
        return self


class DimensionBasis(StrEnum):
    MUSICAL_STYLE = "musical_style"
    WORK_CONTEXT = "work_context"
    SUBJECTIVE_CHARACTER = "subjective_character"
    FUNCTIONAL_CONTEXT = "functional_context"


class ConditionOperator(StrEnum):
    EQUALS = "equals"
    ONE_OF = "one_of"
    NOT_ONE_OF = "not_one_of"
    CONTAINS_ANY = "contains_any"
    CONTAINS_ALL = "contains_all"
    CONTAINS_NONE = "contains_none"
    AT_LEAST = "at_least"
    AT_MOST = "at_most"
    BETWEEN = "between"
    IS = "is"


ScalarValue = str | bool | int | float


class CharacteristicCondition(StrictModel):
    characteristic_id: str = Field(min_length=1)
    operator: ConditionOperator
    values: list[ScalarValue] = Field(min_length=1)


class SpecificationIndicator(StrictModel):
    indicator_id: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[0-9]{3}$")
    summary: str = Field(min_length=1)
    match: Literal["all", "any"]
    conditions: list[CharacteristicCondition] = Field(min_length=1)
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def reject_blank_text(self) -> "SpecificationIndicator":
        if not self.summary.strip() or not self.rationale.strip():
            raise ValueError("indicator text must not be blank")
        return self


class AmbiguityGuidance(StrictModel):
    insufficient_evidence: Literal["abstain"]
    borderline_notes: list[str] = Field(min_length=1)

    @field_validator("borderline_notes")
    @classmethod
    def validate_notes(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized) or len(normalized) != len(set(normalized)):
            raise ValueError("borderline notes must be nonblank and unique")
        return values


class PlaylistSpecificationDocument(StrictModel):
    schema_version: Literal["2"]
    specification_version: str = Field(min_length=1)
    logical_playlist_id: str = Field(min_length=1)
    status: SpecificationStatus
    characteristic_vocabulary_version: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    dimension_basis: DimensionBasis
    strong_indicators: list[SpecificationIndicator] = Field(min_length=1)
    supporting_indicators: list[SpecificationIndicator] = Field(min_length=1)
    negative_indicators: list[SpecificationIndicator] = Field(min_length=1)
    hard_exclusions: list[SpecificationIndicator] = Field(default_factory=list)
    ambiguity_guidance: AmbiguityGuidance
    version_guidance: list[str] = Field(min_length=1)
    instrumental_vocal_guidance: list[str] = Field(min_length=1)
    overlap_refs: list[str] = Field(default_factory=list)

    @field_validator("specification_version", "logical_playlist_id", "purpose")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

    @field_validator("version_guidance", "instrumental_vocal_guidance", "overlap_refs")
    @classmethod
    def reject_blank_or_duplicate_items(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("guidance and references must not contain blank entries")
        if len(normalized) != len(set(normalized)):
            raise ValueError("guidance and references must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_indicator_ids(self) -> "PlaylistSpecificationDocument":
        indicators = (
            self.strong_indicators
            + self.supporting_indicators
            + self.negative_indicators
            + self.hard_exclusions
        )
        ids = [item.indicator_id for item in indicators]
        if len(ids) != len(set(ids)):
            raise ValueError("indicator IDs must be unique within a specification")
        return self


class OverlapRelationship(StrEnum):
    OVERLAPPING = "overlapping"
    NESTED = "nested"
    CONDITIONAL = "conditional"
    CROSS_DIMENSION = "cross_dimension"
    BOUNDARY = "boundary"


class OverlapGuidance(StrictModel):
    overlap_id: str = Field(pattern=r"^[a-z][a-z0-9_.]*__[a-z][a-z0-9_.]*$")
    playlist_ids: list[str] = Field(min_length=2)
    relationship: OverlapRelationship
    both_allowed: bool
    distinguishing_characteristics: list[str] = Field(min_length=1)
    guidance: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_content(self) -> "OverlapGuidance":
        if len(self.playlist_ids) != len(set(self.playlist_ids)):
            raise ValueError("overlap playlist IDs must be unique")
        if len(self.distinguishing_characteristics) != len(
            set(self.distinguishing_characteristics)
        ):
            raise ValueError("overlap distinguishing characteristics must be unique")
        if not self.guidance.strip():
            raise ValueError("overlap guidance must not be blank")
        return self


class OverlapGuidanceDocument(StrictModel):
    schema_version: Literal["1"]
    specification_set_version: str = Field(min_length=1)
    overlaps: list[OverlapGuidance] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "OverlapGuidanceDocument":
        ids = [item.overlap_id for item in self.overlaps]
        if len(ids) != len(set(ids)):
            raise ValueError("overlap IDs must be unique")
        return self


def _load_yaml_mapping(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a YAML mapping")
    return payload


def load_application_config(path: Path) -> ApplicationConfig:
    return ApplicationConfig.model_validate(_load_yaml_mapping(path))


def resolve_taxonomy_path(config_path: Path, config: ApplicationConfig) -> Path:
    path = config.taxonomy_path
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def load_taxonomy_document(path: Path) -> TaxonomyDocument:
    return TaxonomyDocument.model_validate(_load_yaml_mapping(path))


def load_specification_document(path: Path) -> PlaylistSpecificationDocument:
    return PlaylistSpecificationDocument.model_validate(_load_yaml_mapping(path))


def load_characteristic_vocabulary_document(
    path: Path,
) -> CharacteristicVocabularyDocument:
    return CharacteristicVocabularyDocument.model_validate(_load_yaml_mapping(path))


def load_specification_manifest_document(path: Path) -> SpecificationManifestDocument:
    return SpecificationManifestDocument.model_validate(_load_yaml_mapping(path))


def load_overlap_guidance_document(path: Path) -> OverlapGuidanceDocument:
    return OverlapGuidanceDocument.model_validate(_load_yaml_mapping(path))


def load_output_target_registry_document(path: Path) -> OutputTargetRegistryDocument:
    return OutputTargetRegistryDocument.model_validate(_load_yaml_mapping(path))


def load_genre_variant_routing_policy_document(
    path: Path,
) -> GenreVariantRoutingPolicyDocument:
    return GenreVariantRoutingPolicyDocument.model_validate(_load_yaml_mapping(path))


def taxonomy_digest(document: TaxonomyDocument) -> str:
    return document_digest(document)


DocumentModel = TypeVar("DocumentModel", bound=BaseModel)


def document_digest(document: DocumentModel) -> str:
    canonical = json.dumps(
        document.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
