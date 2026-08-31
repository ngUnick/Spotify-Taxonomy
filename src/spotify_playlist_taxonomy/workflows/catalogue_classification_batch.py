"""Atomic combined explicit semantic-review and Genre-routing imports."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spotify_playlist_taxonomy.config import Phase4Bundle, RoutingOutcome
from spotify_playlist_taxonomy.domain.models import AssignmentDecision, MAX_JUSTIFICATION_LENGTH
from spotify_playlist_taxonomy.workflows.genre_routing import GenreRoutingBatchDecision, GenreRoutingService
from spotify_playlist_taxonomy.workflows.phase7 import Phase7Service
from spotify_playlist_taxonomy.workflows.phase7_batch import ReviewBatchDecision


CATALOGUE_CLASSIFICATION_BATCH_SCHEMA_VERSION = "catalogue-classification-batch.v1"
CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS = 250


class CatalogueClassificationBatchValidationError(ValueError):
    """A sanitized validation error for an untrusted local combined batch."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Decision(_StrictModel):
    semantic_id: str = Field(min_length=1)
    decision: AssignmentDecision
    note: str | None = None

    @field_validator("semantic_id")
    @classmethod
    def _semantic(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def _decision(cls, value: object) -> AssignmentDecision:
        if not isinstance(value, str):
            raise ValueError("decision must be a string")
        try:
            return AssignmentDecision(value)
        except ValueError:
            raise ValueError("unsupported decision") from None

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > MAX_JUSTIFICATION_LENGTH):
            raise ValueError(f"note must be nonblank and <= {MAX_JUSTIFICATION_LENGTH} characters")
        return value


class _Track(_StrictModel):
    canonical_id: str = Field(min_length=1)
    decisions: list[_Decision] = Field(default_factory=list, max_length=36)
    genre_route: RoutingOutcome | None = None
    genre_route_note: str | None = None

    @field_validator("canonical_id")
    @classmethod
    def _canonical(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("genre_route", mode="before")
    @classmethod
    def _route(cls, value: object) -> RoutingOutcome | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("genre_route must be a string")
        try:
            route = RoutingOutcome(value)
        except ValueError:
            raise ValueError("unsupported genre_route") from None
        if route not in {RoutingOutcome.GREEK, RoutingOutcome.FOREIGN}:
            raise ValueError("unsupported genre_route")
        return route

    @field_validator("genre_route_note")
    @classmethod
    def _route_note(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > MAX_JUSTIFICATION_LENGTH):
            raise ValueError(f"genre_route_note must be nonblank and <= {MAX_JUSTIFICATION_LENGTH} characters")
        return value

    @field_validator("decisions")
    @classmethod
    def _has_content(cls, value: list[_Decision], info) -> list[_Decision]:
        # The cross-field requirement is enforced below after all fields are parsed.
        if len({item.semantic_id for item in value}) != len(value):
            raise ValueError("duplicate semantic decisions")
        return value


class _Document(_StrictModel):
    schema_version: str = Field(alias="schema")
    tracks: list[_Track] = Field(min_length=1, max_length=CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS)

    @field_validator("schema_version")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != CATALOGUE_CLASSIFICATION_BATCH_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value


@dataclass(frozen=True, slots=True)
class CatalogueClassificationTrack:
    canonical_id: str
    decisions: tuple[ReviewBatchDecision, ...]
    genre_routing: GenreRoutingBatchDecision | None


@dataclass(frozen=True, slots=True)
class CatalogueClassificationBatchOutcome:
    track_count: int
    review_decision_count: int
    routing_decision_count: int
    include_count: int
    exclude_count: int
    abstain_count: int


def parse_catalogue_classification_batch(raw: object) -> tuple[CatalogueClassificationTrack, ...]:
    """Parse only bounded validated local decisions; retain no raw input document."""

    try:
        document = _Document.model_validate(raw)
    except ValidationError as error:
        locations = sorted(".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False))
        raise CatalogueClassificationBatchValidationError(
            f"catalogue classification batch schema is invalid: {', '.join(locations) or 'root'}"
        ) from None
    if len({item.canonical_id for item in document.tracks}) != len(document.tracks):
        raise CatalogueClassificationBatchValidationError("catalogue classification batch contains duplicate canonical tracks")
    tracks: list[CatalogueClassificationTrack] = []
    for item in document.tracks:
        if not item.decisions and item.genre_route is None:
            raise CatalogueClassificationBatchValidationError("each track requires a decision or genre_route")
        if item.genre_route is None and item.genre_route_note is not None:
            raise CatalogueClassificationBatchValidationError("genre_route_note requires genre_route")
        tracks.append(CatalogueClassificationTrack(
            item.canonical_id,
            tuple(ReviewBatchDecision(item.canonical_id, decision.semantic_id, decision.decision, decision.note) for decision in item.decisions),
            None if item.genre_route is None else GenreRoutingBatchDecision(item.canonical_id, item.genre_route, item.genre_route_note),
        ))
    return tuple(tracks)


class CatalogueClassificationBatchService:
    """Commits ordinary Phase 7 and routing event semantics in one transaction."""

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection = connection
        self.review = Phase7Service(connection, bundle)
        self.routing = GenreRoutingService(connection, bundle)

    def apply(self, tracks: tuple[CatalogueClassificationTrack, ...]) -> CatalogueClassificationBatchOutcome:
        if not tracks or len(tracks) > CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS:
            raise ValueError(f"catalogue classification batch requires 1-{CATALOGUE_CLASSIFICATION_BATCH_MAX_TRACKS} tracks")
        if len({item.canonical_id for item in tracks}) != len(tracks):
            raise ValueError("catalogue classification batch contains duplicate canonical tracks")
        reviews = tuple(decision for item in tracks for decision in item.decisions)
        routing = tuple(item.genre_routing for item in tracks if item.genre_routing is not None)
        for decision in reviews:
            self.review._validate_decision(decision.canonical_id, decision.semantic_id, decision.note)
        for decision in routing:
            self.routing._validate_decision(decision)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for decision in reviews:
                self.review._record_decision(decision.canonical_id, decision.semantic_id, decision.decision, decision.note)
            for decision in routing:
                self.routing._record_decision(decision)
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ValueError("catalogue classification batch could not be applied") from error
        return CatalogueClassificationBatchOutcome(
            len(tracks), len(reviews), len(routing),
            sum(item.decision is AssignmentDecision.INCLUDE for item in reviews),
            sum(item.decision is AssignmentDecision.EXCLUDE for item in reviews),
            sum(item.decision is AssignmentDecision.ABSTAIN for item in reviews),
        )
