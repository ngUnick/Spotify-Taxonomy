"""Bounded explicit human Genre-output routing, separate from semantic review."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spotify_playlist_taxonomy.config import Phase4Bundle, RoutingOutcome
from spotify_playlist_taxonomy.domain.models import MAX_JUSTIFICATION_LENGTH
from spotify_playlist_taxonomy.persistence.database import utc_now


ROUTING_BATCH_SCHEMA_VERSION = "genre-routing-batch.v1"
ROUTING_BATCH_MAX_DECISIONS = 250


class GenreRoutingBatchValidationError(ValueError):
    """A sanitized validation error for an untrusted local routing file."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _BatchDecision(_StrictModel):
    canonical_id: str = Field(min_length=1)
    route: RoutingOutcome
    note: str | None = None

    @field_validator("canonical_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("route", mode="before")
    @classmethod
    def _route(cls, value: object) -> RoutingOutcome:
        if not isinstance(value, str):
            raise ValueError("route must be a string")
        try:
            route = RoutingOutcome(value)
        except ValueError:
            raise ValueError("unsupported route") from None
        if route not in {RoutingOutcome.GREEK, RoutingOutcome.FOREIGN}:
            raise ValueError("unsupported route")
        return route

    @field_validator("note")
    @classmethod
    def _note(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value) > MAX_JUSTIFICATION_LENGTH):
            raise ValueError(f"note must be nonblank and <= {MAX_JUSTIFICATION_LENGTH} characters")
        return value


class _BatchDocument(_StrictModel):
    schema_version: str = Field(alias="schema")
    decisions: list[_BatchDecision] = Field(min_length=1, max_length=ROUTING_BATCH_MAX_DECISIONS)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != ROUTING_BATCH_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value


@dataclass(frozen=True, slots=True)
class GenreRoutingBatchDecision:
    canonical_id: str
    route: RoutingOutcome
    note: str | None


@dataclass(frozen=True, slots=True)
class ExplicitGenreRouting:
    canonical_track_id: str
    route: RoutingOutcome | None
    state: str
    routing_policy_version: str | None
    bundle_digest: str | None
    provenance: str | None


@dataclass(frozen=True, slots=True)
class GenreRoutingBatchOutcome:
    decision_count: int
    canonical_track_count: int
    greek_count: int
    foreign_count: int


def parse_genre_routing_batch(raw: object) -> tuple[GenreRoutingBatchDecision, ...]:
    try:
        document = _BatchDocument.model_validate(raw)
    except ValidationError as error:
        locations = sorted(".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False))
        raise GenreRoutingBatchValidationError(
            f"genre routing batch schema is invalid: {', '.join(locations) or 'root'}"
        ) from None
    decisions = tuple(GenreRoutingBatchDecision(item.canonical_id, item.route, item.note) for item in document.decisions)
    if len({item.canonical_id for item in decisions}) != len(decisions):
        raise GenreRoutingBatchValidationError("genre routing batch contains duplicate canonical decisions")
    return decisions


class GenreRoutingService:
    """Append-only local routing decisions for exact canonical recordings."""

    def __init__(self, connection: sqlite3.Connection, bundle: Phase4Bundle) -> None:
        self.connection = connection
        self.bundle = bundle

    def show(self, canonical_id: str) -> ExplicitGenreRouting:
        row = self._latest_event(canonical_id)
        if row is None:
            return ExplicitGenreRouting(canonical_id, None, "absent", None, None, None)
        current = (
            row["routing_policy_version"] == self.bundle.genre_variant_routing.routing_policy_version
            and row["bundle_digest"] == self.bundle.digest
        )
        return ExplicitGenreRouting(
            canonical_id, RoutingOutcome(row["route"]),
            "current" if current else "stale", row["routing_policy_version"],
            row["bundle_digest"], row["origin"],
        )

    def apply_batch(self, decisions: tuple[GenreRoutingBatchDecision, ...]) -> GenreRoutingBatchOutcome:
        if not decisions or len(decisions) > ROUTING_BATCH_MAX_DECISIONS:
            raise ValueError(f"genre routing batch requires 1-{ROUTING_BATCH_MAX_DECISIONS} decisions")
        if len({item.canonical_id for item in decisions}) != len(decisions):
            raise ValueError("genre routing batch contains duplicate canonical decisions")
        for item in decisions:
            self._validate_decision(item)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            for item in decisions:
                self._record_decision(item)
            self.connection.commit()
        except sqlite3.Error as error:
            self.connection.rollback()
            raise ValueError("genre routing batch could not be applied") from error
        return GenreRoutingBatchOutcome(
            len(decisions), len({item.canonical_id for item in decisions}),
            sum(item.route is RoutingOutcome.GREEK for item in decisions),
            sum(item.route is RoutingOutcome.FOREIGN for item in decisions),
        )

    def _validate_decision(self, item: GenreRoutingBatchDecision) -> None:
        if item.route not in {RoutingOutcome.GREEK, RoutingOutcome.FOREIGN}:
            raise ValueError("unsupported route")
        if item.note is not None and (not item.note.strip() or len(item.note) > MAX_JUSTIFICATION_LENGTH):
            raise ValueError(f"note must be nonblank and <= {MAX_JUSTIFICATION_LENGTH} characters")
        if not self.connection.execute(
            "SELECT 1 FROM canonical_tracks WHERE canonical_track_id=?", (item.canonical_id,)
        ).fetchone():
            raise ValueError("unknown canonical ID")

    def _latest_event(self, canonical_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM genre_routing_events WHERE canonical_track_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1", (canonical_id,),
        ).fetchone()

    def _record_decision(self, item: GenreRoutingBatchDecision) -> bool:
        current = self._latest_event(item.canonical_id)
        if current is not None and (
            current["route"] == item.route.value
            and current["routing_policy_version"] == self.bundle.genre_variant_routing.routing_policy_version
            and current["bundle_digest"] == self.bundle.digest
            and current["note"] == item.note
        ):
            return False
        self.connection.execute(
            "INSERT INTO genre_routing_events VALUES (?, ?, ?, 'user_explicit', ?, ?, ?, ?)",
            (f"genre-routing.{uuid.uuid4().hex}", item.canonical_id, item.route.value,
             self.bundle.genre_variant_routing.routing_policy_version, self.bundle.digest,
             item.note, utc_now()),
        )
        return True
