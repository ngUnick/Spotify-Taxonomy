"""Provider-neutral contracts for exact-recording research."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from spotify_playlist_taxonomy.domain.models import EvidenceKind, ResearchSource
from spotify_playlist_taxonomy.domain.research import (
    EvidenceType,
    QualityBand,
    SubjectMatch,
    SubjectScope,
)


class ProviderOutcomeStatus(StrEnum):
    SUCCESS = "success"
    NO_RESULT = "no_result"
    AMBIGUOUS_SUBJECT = "ambiguous_subject"
    TRANSIENT_ERROR = "transient_error"
    UNAVAILABLE = "unavailable"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class IndependentIdentifier:
    namespace: str
    value: str

    def __post_init__(self) -> None:
        if not self.namespace.strip() or not self.value.strip():
            raise ValueError("independent identifier fields must not be blank")


@dataclass(frozen=True, slots=True)
class ProviderDescriptor:
    provider_id: str
    adapter_version: str
    contract_version: str
    supported_evidence_types: tuple[EvidenceType, ...]
    credential_required: bool = False
    minimum_interval_seconds: float = 0.0

    def __post_init__(self) -> None:
        if not self.provider_id.strip() or not self.adapter_version.strip():
            raise ValueError("provider identity/version must not be blank")
        if not self.contract_version.strip():
            raise ValueError("provider contract version must not be blank")
        if not self.supported_evidence_types:
            raise ValueError("provider must declare at least one capability")
        if self.minimum_interval_seconds < 0:
            raise ValueError("minimum_interval_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class ResearchRequest:
    request_id: str
    schema_version: str
    canonical_track_id: str
    subject_fingerprint: str
    title: str
    artists: tuple[str, ...]
    version_cues: tuple[str, ...]
    independent_identifiers: tuple[IndependentIdentifier, ...]
    requested_evidence_types: tuple[EvidenceType, ...]
    requested_at: str

    def __post_init__(self) -> None:
        text_fields = (
            self.request_id, self.schema_version, self.canonical_track_id,
            self.subject_fingerprint, self.title, self.requested_at,
        )
        if any(not value.strip() for value in text_fields):
            raise ValueError("research request fields must not be blank")
        if not self.artists or any(not value.strip() for value in self.artists):
            raise ValueError("research request requires ordered artists")
        if not self.requested_evidence_types:
            raise ValueError("research request requires evidence types")


@dataclass(frozen=True, slots=True)
class ProviderAssertion:
    assertion_key: str
    evidence_type: EvidenceType
    asserted_value: dict[str, object]
    kind: EvidenceKind
    source: ResearchSource
    source_reference: str
    subject_scope: SubjectScope
    subject_match: SubjectMatch
    native_confidence: float | None = None
    quality_band: QualityBand = QualityBand.UNKNOWN
    observed_at: str | None = None
    cacheable: bool = True

    def __post_init__(self) -> None:
        if not self.assertion_key.strip() or not self.source_reference.strip():
            raise ValueError("assertion identity/reference must not be blank")
        if not self.asserted_value:
            raise ValueError("asserted_value must not be empty")
        try:
            json.dumps(self.asserted_value, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError) as error:
            raise ValueError("asserted_value must be JSON serializable") from error
        if self.native_confidence is not None and not 0 <= self.native_confidence <= 1:
            raise ValueError("native_confidence must be between 0 and 1")


@dataclass(frozen=True, slots=True)
class ResearchResult:
    request_id: str
    provider_id: str
    adapter_version: str
    query_id: str
    query_fingerprint: str
    retrieved_at: str
    status: ProviderOutcomeStatus
    assertions: tuple[ProviderAssertion, ...] = ()
    retry_after_seconds: float | None = None
    error_category: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.request_id, self.provider_id, self.adapter_version,
            self.query_id, self.query_fingerprint, self.retrieved_at,
        )
        if any(not value.strip() for value in required):
            raise ValueError("research result identity fields must not be blank")
        if self.status is ProviderOutcomeStatus.SUCCESS and not self.assertions:
            raise ValueError("successful research result requires assertions")
        if self.status is not ProviderOutcomeStatus.SUCCESS and self.assertions:
            raise ValueError("non-success research result cannot contain assertions")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        if self.error_category is not None and not self.error_category.strip():
            raise ValueError("error_category must not be blank")


class ResearchProvider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    def research(self, request: ResearchRequest) -> ResearchResult: ...
