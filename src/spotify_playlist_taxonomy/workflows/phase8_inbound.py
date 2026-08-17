"""Strict, offline validation for supervised Phase 8B inbound results.

This module deliberately has no persistence, provider, platform, or API dependency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from spotify_playlist_taxonomy.domain.models import AssignmentDecision, MAX_JUSTIFICATION_LENGTH


RESULT_SCHEMA_VERSION = "codex-result.v1"


class InboundResultValidationError(ValueError):
    """A deterministic, sanitized inbound-result validation failure."""


class _StrictInboundModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class InboundClassification(_StrictInboundModel):
    semantic_specification_id: str = Field(min_length=1)
    decision: AssignmentDecision
    justification_summary: str = Field(min_length=1, max_length=MAX_JUSTIFICATION_LENGTH)
    evidence_ids: list[str]
    characteristic_ids: list[str]

    @field_validator(
        "semantic_specification_id",
        "justification_summary",
    )
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("decision", mode="before")
    @classmethod
    def _parse_existing_decision(cls, value: object) -> AssignmentDecision:
        if not isinstance(value, str):
            raise ValueError("decision must be a string")
        try:
            return AssignmentDecision(value)
        except ValueError:
            raise ValueError("unsupported decision") from None

    @field_validator("evidence_ids", "characteristic_ids")
    @classmethod
    def _require_nonblank_references(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("references must not contain blank values")
        return values


class InboundResultItem(_StrictInboundModel):
    item_id: str = Field(min_length=1)
    classifications: list[InboundClassification]

    @field_validator("item_id")
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class InboundResult(_StrictInboundModel):
    result_schema_version: str
    batch_id: str = Field(min_length=1)
    outbound_batch_digest: str = Field(min_length=1)
    taxonomy_version: str = Field(min_length=1)
    specification_bundle_digest: str = Field(min_length=1)
    items: list[InboundResultItem]

    @field_validator(
        "result_schema_version",
        "batch_id",
        "outbound_batch_digest",
        "taxonomy_version",
        "specification_bundle_digest",
    )
    @classmethod
    def _require_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


@dataclass(frozen=True, slots=True)
class OutboundBatchItemContext:
    """The reference IDs exposed for a single actual Phase 8A outbound item."""

    item_id: str
    evidence_ids: frozenset[str]
    characteristic_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class OutboundBatchContext:
    """The immutable metadata and exported context to which an inbound result binds."""

    batch_id: str
    outbound_batch_digest: str
    taxonomy_version: str
    specification_bundle_digest: str
    items: tuple[OutboundBatchItemContext, ...]
    semantic_specification_ids: frozenset[str]
    specification_versions: Mapping[str, str]
    output_target_ids: frozenset[str] = frozenset()

    @classmethod
    def from_phase8a_export(
        cls,
        *,
        batch_metadata: Mapping[str, Any],
        outbound_document: Mapping[str, Any],
        output_target_ids: frozenset[str] = frozenset(),
    ) -> "OutboundBatchContext":
        """Build validation context from persisted Phase 8A metadata and its outbound JSON."""
        try:
            batch_id = _nonblank_text(batch_metadata["batch_id"])
            outbound_batch_digest = _nonblank_text(batch_metadata["outbound_digest"])
            taxonomy_version = _nonblank_text(batch_metadata["taxonomy_version"])
            specification_bundle_digest = _nonblank_text(batch_metadata["bundle_digest"])
            if _nonblank_text(outbound_document["batch_id"]) != batch_id:
                raise ValueError
            if _nonblank_text(outbound_document["taxonomy_version"]) != taxonomy_version:
                raise ValueError
            if _nonblank_text(outbound_document["bundle_digest"]) != specification_bundle_digest:
                raise ValueError
            raw_items = outbound_document["items"]
            raw_specifications = outbound_document["specifications"]
            if not isinstance(raw_items, list) or not isinstance(raw_specifications, list):
                raise ValueError
            items = tuple(_outbound_item_context(item) for item in raw_items)
            specification_versions = {
                _nonblank_text(specification["logical_playlist_id"]): _nonblank_text(
                    specification["specification_version"]
                )
                for specification in raw_specifications
                if isinstance(specification, Mapping)
            }
            semantic_ids = frozenset(specification_versions)
            if len(semantic_ids) != len(raw_specifications):
                raise ValueError
            if not items or not semantic_ids or len({item.item_id for item in items}) != len(items):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise InboundResultValidationError("outbound batch context is invalid") from None
        return cls(
            batch_id=batch_id,
            outbound_batch_digest=outbound_batch_digest,
            taxonomy_version=taxonomy_version,
            specification_bundle_digest=specification_bundle_digest,
            items=items,
            semantic_specification_ids=semantic_ids,
            specification_versions=specification_versions,
            output_target_ids=frozenset(output_target_ids),
        )


def parse_inbound_result(raw: object) -> InboundResult:
    """Strictly parse untrusted inbound data without retaining the raw input."""
    try:
        return InboundResult.model_validate(raw)
    except ValidationError as error:
        locations = sorted(
            ".".join(str(part) for part in issue["loc"])
            for issue in error.errors(include_input=False)
        )
        detail = ", ".join(locations) if locations else "root"
        raise InboundResultValidationError(f"inbound result schema is invalid: {detail}") from None


def validate_inbound_result(
    result: InboundResult,
    context: OutboundBatchContext,
) -> InboundResult:
    """Validate an already parsed result against its actual Phase 8A export context."""
    errors: list[str] = []
    if result.result_schema_version != RESULT_SCHEMA_VERSION:
        errors.append("unsupported result schema version")
    if result.batch_id != context.batch_id:
        errors.append("batch ID does not match outbound batch")
    if result.outbound_batch_digest != context.outbound_batch_digest:
        errors.append("outbound batch digest does not match")
    if result.taxonomy_version != context.taxonomy_version:
        errors.append("taxonomy version does not match")
    if result.specification_bundle_digest != context.specification_bundle_digest:
        errors.append("specification bundle digest does not match")

    item_contexts = {item.item_id: item for item in context.items}
    seen_item_ids: set[str] = set()
    for item in result.items:
        if item.item_id in seen_item_ids:
            errors.append(f"duplicate item ID: {item.item_id}")
            continue
        seen_item_ids.add(item.item_id)
        item_context = item_contexts.get(item.item_id)
        if item_context is None:
            errors.append(f"item does not belong to outbound batch: {item.item_id}")
            continue

        seen_specification_ids: set[str] = set()
        for classification in item.classifications:
            specification_id = classification.semantic_specification_id
            if specification_id in context.output_target_ids or specification_id.startswith("output."):
                errors.append(f"output target ID is not a semantic specification ID: {specification_id}")
            elif specification_id not in context.semantic_specification_ids:
                errors.append(f"unknown semantic specification ID: {specification_id}")
            if specification_id in seen_specification_ids:
                errors.append(
                    f"duplicate semantic classification for item {item.item_id}: {specification_id}"
                )
            seen_specification_ids.add(specification_id)
            for evidence_id in classification.evidence_ids:
                if evidence_id not in item_context.evidence_ids:
                    errors.append(f"unexported evidence reference for item {item.item_id}: {evidence_id}")
            for characteristic_id in classification.characteristic_ids:
                if characteristic_id not in item_context.characteristic_ids:
                    errors.append(
                        f"unexported characteristic reference for item {item.item_id}: {characteristic_id}"
                    )
    if errors:
        raise InboundResultValidationError("; ".join(sorted(set(errors))))
    return result


def _nonblank_text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value


def _outbound_item_context(raw_item: object) -> OutboundBatchItemContext:
    if not isinstance(raw_item, Mapping):
        raise ValueError
    evidence = raw_item["evidence"]
    characteristics = raw_item["characteristics"]
    if not isinstance(evidence, list) or not isinstance(characteristics, list):
        raise ValueError
    evidence_ids = frozenset(
        _nonblank_text(item["evidence_id"])
        for item in evidence
        if isinstance(item, Mapping)
    )
    characteristic_ids = frozenset(
        _nonblank_text(item["characteristic_id"])
        for item in characteristics
        if isinstance(item, Mapping)
    )
    if len(evidence_ids) != len(evidence) or len(characteristic_ids) != len(characteristics):
        raise ValueError
    return OutboundBatchItemContext(
        item_id=_nonblank_text(raw_item["item_id"]),
        evidence_ids=evidence_ids,
        characteristic_ids=characteristic_ids,
    )
