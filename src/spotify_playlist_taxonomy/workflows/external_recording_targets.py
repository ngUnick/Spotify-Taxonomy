"""Read-only target projection for a future external-recording evidence fetch."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Callable, Protocol

from spotify_playlist_taxonomy.persistence.identity_external_evidence import (
    IdentityExternalEntityEvidence,
    IdentityExternalEvidenceStore,
)
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store
from spotify_playlist_taxonomy.workflows.match_topology import UnresolvedMatchTopologyService


class RecordingLookup(Protocol):
    def lookup_recordings_by_isrc(self, isrc: str): ...


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


@dataclass(frozen=True, slots=True)
class ExternalRecordingFetchReport:
    planned: int
    processed: int
    resolved: int
    ambiguous: int
    not_found: int
    insufficient_input: int
    lookup_failed: int
    skipped_fresh: int


class ExternalRecordingEvidenceFetchService:
    """Explicit bounded external-evidence fetch; it cannot alter matching state."""

    def __init__(self, store: Phase3Store, binding_id: str, *, positive_ttl_hours: int, negative_ttl_hours: int, ambiguous_ttl_hours: int, now: Callable[[], datetime] | None = None) -> None:
        self._store, self._binding_id = store, binding_id
        self._positive_ttl_hours, self._negative_ttl_hours = positive_ttl_hours, negative_ttl_hours
        self._ambiguous_ttl_hours, self._now = ambiguous_ttl_hours, now or (lambda: datetime.now(UTC))

    def plan(self, limit: int) -> tuple[ExternalRecordingTarget, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return tuple(item for item in ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets if item.requires_lookup)[:limit]

    def fetch(self, limit: int, lookup: RecordingLookup) -> ExternalRecordingFetchReport:
        selected = self.plan(limit)
        counts: Counter[str] = Counter(planned=len(selected))
        evidence_store = IdentityExternalEvidenceStore(self._store.connection)
        for target in selected:
            current = {item.identity_id: item for item in ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets}
            if not current.get(target.identity_id, target).requires_lookup:
                counts["skipped_fresh"] += 1
                continue
            isrc = self._store.connection.execute("SELECT isrc FROM track_identities WHERE identity_id = ?", (target.identity_id,)).fetchone()[0]
            counts["processed"] += 1
            if not isinstance(isrc, str) or not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", isrc):
                self._persist(evidence_store, target.identity_id, "insufficient_input", None, "local-input", self._fingerprint({"identity_id": target.identity_id, "reason": "missing_isrc"}), "local.v1", None, None)
                counts["insufficient_input"] += 1
                continue
            try:
                result = lookup.lookup_recordings_by_isrc(isrc)
            except Exception:
                self._persist(evidence_store, target.identity_id, "lookup_failed", None, "isrc", self._fingerprint({"isrc": isrc}), "musicbrainz-lookup.v1", None, None)
                counts["lookup_failed"] += 1
                continue
            if result.failure_category is not None:
                self._persist(evidence_store, target.identity_id, "lookup_failed", None, "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
                counts["lookup_failed"] += 1
            elif len(result.recording_ids) == 1:
                self._persist(evidence_store, target.identity_id, "resolved", result.recording_ids[0], "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
                counts["resolved"] += 1
            elif not result.recording_ids:
                self._persist(evidence_store, target.identity_id, "not_found", None, "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
                counts["not_found"] += 1
            else:
                digest = self._fingerprint({"recording_ids": list(result.recording_ids)})
                self._persist(evidence_store, target.identity_id, "ambiguous", None, "isrc", result.query_fingerprint, result.adapter_version, len(result.recording_ids), digest, result.retrieved_at)
                counts["ambiguous"] += 1
        return ExternalRecordingFetchReport(*(counts[name] for name in ("planned", "processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh")))

    def _persist(self, store: IdentityExternalEvidenceStore, identity_id: str, state: str, external_id: str | None, method: str, fingerprint: str, version: str, ambiguity_count: int | None, ambiguity_digest: str | None, retrieved_at: str | None = None) -> None:
        now = _parse_time(retrieved_at) if retrieved_at else self._now()
        ttl = self._positive_ttl_hours if state == "resolved" else self._ambiguous_ttl_hours if state == "ambiguous" else self._negative_ttl_hours
        stamp = now.isoformat().replace("+00:00", "Z")
        store.upsert(IdentityExternalEntityEvidence(
            "identity-external-evidence." + self._fingerprint({"identity_id": identity_id, "provider": "musicbrainz", "entity_type": "recording"})[:24],
            identity_id, "musicbrainz", "recording", external_id, state, method, fingerprint, version,
            stamp, (now + timedelta(hours=ttl)).isoformat().replace("+00:00", "Z"), ambiguity_count, ambiguity_digest,
        ))

    @staticmethod
    def _fingerprint(value: object) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
