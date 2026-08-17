"""Research lifecycle and provider-independent sufficiency state."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

class EvidenceType(StrEnum):
    RECORDING_IDENTITY = "recording_identity"
    VERSION_INFORMATION = "version_information"
    INDEPENDENT_IDENTIFIER = "independent_identifier"
    RELEASE = "release"
    CREDITS = "credits"
    RELATIONSHIP = "relationship"
    STYLE_TRADITION = "style_tradition"
    WORK_CONTEXT = "work_context"
    LYRICAL_LANGUAGE = "lyrical_language"
    INSTRUMENTATION = "instrumentation"
    ARRANGEMENT = "arrangement"
    TEMPO_RHYTHM = "tempo_rhythm"
    ATMOSPHERE = "atmosphere"
    AFFECT_MOOD = "affect_mood"
    CULTURAL_REGIONAL_CONTEXT = "cultural_regional_context"
    PROJECT_SCENE_CONTEXT = "project_scene_context"


class SubjectScope(StrEnum):
    RECORDING = "recording"
    WORK = "work"
    RELEASE = "release"
    ARTIST = "artist"
    PROJECT = "project"


class SubjectMatch(StrEnum):
    EXACT = "exact"
    PROBABLE = "probable"
    AMBIGUOUS = "ambiguous"
    CONTEXTUAL = "contextual"


class QualityBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class ResearchJobStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


class SufficiencyState(StrEnum):
    SATISFIED = "satisfied"
    MISSING = "missing"
    LOW_QUALITY = "low_quality"
    CONTRADICTORY = "contradictory"
    STALE = "stale"
    SUBJECT_AMBIGUOUS = "subject_ambiguous"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    NOT_ASSESSED = "not_assessed"
    NOT_APPLICABLE = "not_applicable"


class SufficiencyAction(StrEnum):
    NONE = "none"
    QUERY_PROVIDER = "query_provider"
    REFRESH = "refresh"
    HUMAN_REVIEW = "human_review"


@dataclass(frozen=True, slots=True)
class EvidenceNeedAssessment:
    evidence_type: EvidenceType
    state: SufficiencyState
    evidence_ids: tuple[str, ...]
    providers_consulted: tuple[str, ...]
    reason_codes: tuple[str, ...]
    next_action: SufficiencyAction


@dataclass(frozen=True, slots=True)
class ResearchRunSummary:
    job_ids: tuple[str, ...]
    provider_calls: int
    cache_hits: int
    evidence_created: int


@dataclass(frozen=True, slots=True)
class SufficiencyReport:
    canonical_track_id: str
    phase4_bundle_digest: str
    assessments: tuple[EvidenceNeedAssessment, ...]
