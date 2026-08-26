"""Strict local input contract for bounded Phase 7 human-review batches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spotify_playlist_taxonomy.domain.models import AssignmentDecision, MAX_JUSTIFICATION_LENGTH


BATCH_SCHEMA_VERSION = "phase7-review-batch.v1"
BATCH_MAX_DECISIONS = 250


class ReviewBatchValidationError(ValueError):
    """A sanitized validation error for an untrusted local review file."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _BatchDecision(_StrictModel):
    canonical_id: str = Field(min_length=1)
    semantic_id: str = Field(min_length=1)
    decision: AssignmentDecision
    note: str | None = None

    @field_validator("canonical_id", "semantic_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
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


class _BatchDocument(_StrictModel):
    schema_version: str = Field(alias="schema")
    decisions: list[_BatchDecision] = Field(min_length=1, max_length=BATCH_MAX_DECISIONS)

    @field_validator("schema_version")
    @classmethod
    def _version(cls, value: str) -> str:
        if value != BATCH_SCHEMA_VERSION:
            raise ValueError("unsupported schema version")
        return value


@dataclass(frozen=True, slots=True)
class ReviewBatchDecision:
    canonical_id: str
    semantic_id: str
    decision: AssignmentDecision
    note: str | None


def parse_review_batch(raw: object) -> tuple[ReviewBatchDecision, ...]:
    """Parse a bounded untrusted document without retaining its raw contents."""

    try:
        document = _BatchDocument.model_validate(raw)
    except ValidationError as error:
        locations = sorted(".".join(str(part) for part in item["loc"]) for item in error.errors(include_input=False))
        raise ReviewBatchValidationError(
            f"review batch schema is invalid: {', '.join(locations) or 'root'}"
        ) from None
    decisions = tuple(
        ReviewBatchDecision(item.canonical_id, item.semantic_id, item.decision, item.note)
        for item in document.decisions
    )
    pairs = {(item.canonical_id, item.semantic_id) for item in decisions}
    if len(pairs) != len(decisions):
        raise ReviewBatchValidationError("review batch contains duplicate canonical/semantic decisions")
    return decisions
