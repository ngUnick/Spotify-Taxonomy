"""Read-only target projection for a future external-recording evidence fetch."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from spotify_playlist_taxonomy.persistence.identity_external_evidence import (
    IdentityExternalEntityEvidence,
    IdentityExternalEvidenceStore,
)
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.workflows.match_topology import UnresolvedMatchTopologyService


@dataclass(frozen=True, slots=True)
class ExternalRecordingTarget:
    identity_id: str
    roles: tuple[str, ...]
    candidate_count: int
    comparison_count: int
    evidence_state: str
    external_recording_id: str | None
    is_fresh: bool
    requires_lookup: bool
    lookup_reason: str


@dataclass(frozen=True, slots=True)
class ExternalRecordingTargetPreview:
    current_candidate_count: int
    unique_candidate_identity_count: int
    unique_comparison_identity_count: int
    unique_target_identity_count: int
    candidate_and_comparison_overlap: int
    requires_lookup_count: int
    evidence_state_counts: tuple[tuple[str, int], ...]
    targets: tuple[ExternalRecordingTarget, ...]


class ExternalRecordingTargetProjectionService:
    """Local-only projection; it has neither provider access nor mutation methods."""

    def __init__(self, store: Phase3Store, binding_id: str, now: Callable[[], datetime] | None = None) -> None:
        self._store, self._binding_id = store, binding_id
        self._now = now or (lambda: datetime.now(UTC))

    def preview(self) -> ExternalRecordingTargetPreview:
        candidates = UnresolvedMatchTopologyService(self._store, self._binding_id).candidates()
        candidate_ids = {item.identity_id for item in candidates}
        comparison_ids = {comparison_id for item in candidates for comparison_id in item.comparison_identity_ids}
        target_ids = tuple(sorted(candidate_ids | comparison_ids))
        evidence_by_identity = {
            item.identity_id: item
            for item in IdentityExternalEvidenceStore(self._store.connection).for_identities(target_ids)
            if item.provider == "musicbrainz" and item.entity_type == "recording"
        }
        candidate_counts = Counter(item.identity_id for item in candidates)
        comparison_counts = Counter(comparison_id for item in candidates for comparison_id in item.comparison_identity_ids)
        now = self._now()
        targets = tuple(
            self._target(identity_id, candidate_counts[identity_id], comparison_counts[identity_id], evidence_by_identity.get(identity_id), now)
            for identity_id in target_ids
        )
        counts = Counter(item.evidence_state for item in targets)
        return ExternalRecordingTargetPreview(
            len(candidates), len(candidate_ids), len(comparison_ids), len(targets),
            len(candidate_ids & comparison_ids), sum(item.requires_lookup for item in targets),
            tuple(sorted(counts.items())), targets,
        )

    @staticmethod
    def _target(identity_id: str, candidate_count: int, comparison_count: int, evidence: IdentityExternalEntityEvidence | None, now: datetime) -> ExternalRecordingTarget:
        roles = (() if not candidate_count else ("candidate",)) + (() if not comparison_count else ("comparison",))
        evidence_state, is_fresh = _evidence_state(evidence, now)
        requires_lookup = evidence_state in {"no_evidence", "stale"}
        reason = {
            "no_evidence": "no_current_musicbrainz_recording_evidence",
            "stale": "evidence_stale",
            "fresh_resolved": "fresh_resolved_evidence",
            "fresh_ambiguous": "fresh_ambiguous_evidence",
            "fresh_not_found": "fresh_not_found_evidence",
            "fresh_insufficient_input": "fresh_insufficient_input_evidence",
            "fresh_lookup_failed": "fresh_lookup_failed_evidence_fail_closed",
        }[evidence_state]
        return ExternalRecordingTarget(
            identity_id, roles, candidate_count, comparison_count, evidence_state,
            evidence.external_id if evidence_state == "fresh_resolved" and evidence else None,
            is_fresh, requires_lookup, reason,
        )


def _evidence_state(evidence: IdentityExternalEntityEvidence | None, now: datetime) -> tuple[str, bool]:
    if evidence is None:
        return "no_evidence", False
    if evidence.lookup_state == "stale" or (evidence.fresh_until is not None and _parse_time(evidence.fresh_until) <= now):
        return "stale", False
    return f"fresh_{evidence.lookup_state}", True


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
