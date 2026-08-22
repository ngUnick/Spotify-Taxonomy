"""Read-only target projection for a future external-recording evidence fetch."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
import re
from typing import Callable, Protocol

from spotify_playlist_taxonomy.persistence.identity_external_evidence import (
    IdentityExternalEntityEvidence,
    IdentityExternalEvidenceStore,
    normalize_external_entity_id,
)
from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store, _effective_version_cue, _versions_conflict
from spotify_playlist_taxonomy.workflows.deterministic_match import ComparisonConsistency, evaluate_comparison_consistency
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


def _has_usable_isrc(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Z]{2}[A-Z0-9]{3}\d{7}", value) is not None


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


@dataclass(frozen=True, slots=True)
class ExternalRecordingFailedRetryReport:
    planned: int
    processed: int
    resolved: int
    ambiguous: int
    not_found: int
    insufficient_input: int
    lookup_failed: int
    skipped_not_failed: int


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

    def failed_retry_plan(self, limit: int) -> tuple[ExternalRecordingTarget, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return tuple(
            item for item in ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets
            if item.evidence_state == "fresh_lookup_failed"
        )[:limit]

    def fetch(self, limit: int, lookup: RecordingLookup) -> ExternalRecordingFetchReport:
        return self.fetch_targets(self.plan(limit), lookup)

    def fetch_targets(self, selected: tuple[ExternalRecordingTarget, ...], lookup: RecordingLookup) -> ExternalRecordingFetchReport:
        """Fetch an explicit, already-bounded target set without changing matching state."""
        counts: Counter[str] = Counter(planned=len(selected))
        evidence_store = IdentityExternalEvidenceStore(self._store.connection)
        for target in selected:
            current = {item.identity_id: item for item in ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets}
            if not current.get(target.identity_id, target).requires_lookup:
                counts["skipped_fresh"] += 1
                continue
            counts["processed"] += 1
            counts[self._fetch_target(target.identity_id, evidence_store, lookup)] += 1
        return ExternalRecordingFetchReport(*(counts[name] for name in ("planned", "processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh")))

    def retry_failed(self, limit: int, lookup: RecordingLookup) -> ExternalRecordingFailedRetryReport:
        return self.retry_failed_targets(self.failed_retry_plan(limit), lookup)

    def retry_failed_targets(self, selected: tuple[ExternalRecordingTarget, ...], lookup: RecordingLookup) -> ExternalRecordingFailedRetryReport:
        counts: Counter[str] = Counter(planned=len(selected))
        evidence_store = IdentityExternalEvidenceStore(self._store.connection)
        for target in selected:
            current = {item.identity_id: item for item in ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets}
            current_target = current.get(target.identity_id)
            if current_target is None or current_target.evidence_state != "fresh_lookup_failed":
                counts["skipped_not_failed"] += 1
                continue
            counts["processed"] += 1
            counts[self._fetch_target(target.identity_id, evidence_store, lookup)] += 1
        return ExternalRecordingFailedRetryReport(*(counts[name] for name in ("planned", "processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_not_failed")))

    def _fetch_target(self, identity_id: str, evidence_store: IdentityExternalEvidenceStore, lookup: RecordingLookup) -> str:
        row = self._store.connection.execute("SELECT isrc FROM track_identities WHERE identity_id = ?", (identity_id,)).fetchone()
        isrc = row[0] if row is not None else None
        if not _has_usable_isrc(isrc):
            self._persist(evidence_store, identity_id, "insufficient_input", None, "local-input", self._fingerprint({"identity_id": identity_id, "reason": "missing_isrc"}), "local.v1", None, None)
            return "insufficient_input"
        try:
            result = lookup.lookup_recordings_by_isrc(isrc)
        except Exception:
            self._persist(evidence_store, identity_id, "lookup_failed", None, "isrc", self._fingerprint({"isrc": isrc}), "musicbrainz-lookup.v1", None, None)
            return "lookup_failed"
        if result.failure_category is not None:
            self._persist(evidence_store, identity_id, "lookup_failed", None, "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
            return "lookup_failed"
        if len(result.recording_ids) == 1:
            self._persist(evidence_store, identity_id, "resolved", result.recording_ids[0], "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
            return "resolved"
        if not result.recording_ids:
            self._persist(evidence_store, identity_id, "not_found", None, "isrc", result.query_fingerprint, result.adapter_version, None, None, result.retrieved_at)
            return "not_found"
        digest = self._fingerprint({"recording_ids": list(result.recording_ids)})
        self._persist(evidence_store, identity_id, "ambiguous", None, "isrc", result.query_fingerprint, result.adapter_version, len(result.recording_ids), digest, result.retrieved_at)
        return "ambiguous"

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


EXTERNAL_RECORDING_COMPARISON_CATEGORIES = (
    "all-resolved-same-recording", "all-resolved-disagree", "partial-resolution", "no-resolution",
    "has-ambiguous", "has-not-found", "has-insufficient-input", "has-lookup-failed", "has-stale", "has-no-evidence",
)

EXTERNAL_RECORDING_CANDIDATE_STRATUM = "different-isrc-duration-le-500ms-no-version-conflict-single-comparison"


def _fresh_resolved_recording_id(evidence_state: str, external_recording_id: str | None) -> str | None:
    if evidence_state != "fresh_resolved" or external_recording_id is None:
        return None
    try:
        return normalize_external_entity_id("musicbrainz", "recording", external_recording_id)
    except ValueError:
        return None


def _external_recording_relationship(candidate: "ExternalRecordingComparisonMember", comparison: "ExternalRecordingComparisonMember") -> str:
    candidate_id = _fresh_resolved_recording_id(candidate.evidence_state, candidate.external_recording_id)
    comparison_id = _fresh_resolved_recording_id(comparison.evidence_state, comparison.external_recording_id)
    if candidate_id is None or comparison_id is None:
        return "unavailable"
    return "same" if candidate_id == comparison_id else "different"


@dataclass(frozen=True, slots=True)
class ExternalRecordingComparisonMember:
    identity_id: str
    title: str
    artists: tuple[str, ...]
    effective_version_cue: str | None
    duration_ms: int | None
    isrc: str | None
    evidence_state: str
    external_recording_id: str | None
    duration_delta_ms: int | None = None
    isrc_relationship: str | None = None
    version_relationship: str | None = None
    comparison_consistency: str | None = None
    external_recording_relationship: str | None = None


@dataclass(frozen=True, slots=True)
class ExternalRecordingCandidateComparison:
    candidate_id: str
    canonical_track_id: str
    primary_outcome: str
    candidate: ExternalRecordingComparisonMember
    comparisons: tuple[ExternalRecordingComparisonMember, ...]
    comparison_consistency: ComparisonConsistency
    has_ambiguous: bool
    has_not_found: bool
    has_insufficient_input: bool
    has_lookup_failed: bool
    has_stale: bool
    has_no_evidence: bool

    @property
    def identity_id(self) -> str:
        return self.candidate.identity_id

    @property
    def members(self) -> tuple[ExternalRecordingComparisonMember, ...]:
        return (self.candidate, *self.comparisons)

    @property
    def resolved_identity_count(self) -> int:
        return sum(_fresh_resolved_recording_id(item.evidence_state, item.external_recording_id) is not None for item in self.members)

    @property
    def distinct_resolved_recording_count(self) -> int:
        return len({recording_id for item in self.members if (recording_id := _fresh_resolved_recording_id(item.evidence_state, item.external_recording_id)) is not None})


@dataclass(frozen=True, slots=True)
class ExternalRecordingComparisonStatus:
    target_preview: ExternalRecordingTargetPreview
    primary_counts: tuple[tuple[str, int], ...]
    flag_counts: tuple[tuple[str, int], ...]
    comparisons: tuple[ExternalRecordingCandidateComparison, ...]


class ExternalRecordingComparisonService:
    """Pure local candidate-level external-evidence analysis; no matching authority."""

    def __init__(self, store: Phase3Store, binding_id: str, now: Callable[[], datetime] | None = None) -> None:
        self._store, self._binding_id = store, binding_id
        self._now = now or (lambda: datetime.now(UTC))

    def status(self) -> ExternalRecordingComparisonStatus:
        targets = ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview()
        target_by_id = {item.identity_id: item for item in targets.targets}
        comparisons = tuple(self._comparison(item, target_by_id) for item in UnresolvedMatchTopologyService(self._store, self._binding_id).candidates())
        primary = Counter(item.primary_outcome for item in comparisons)
        flags = Counter()
        for item in comparisons:
            for name in ("has_ambiguous", "has_not_found", "has_insufficient_input", "has_lookup_failed", "has_stale", "has_no_evidence"):
                flags[name] += bool(getattr(item, name))
        return ExternalRecordingComparisonStatus(targets, tuple(sorted(primary.items())), tuple(sorted(flags.items())), comparisons)

    def compare(self, category: str, limit: int) -> tuple[ExternalRecordingCandidateComparison, ...]:
        if category not in EXTERNAL_RECORDING_COMPARISON_CATEGORIES:
            raise ValueError("unknown external recording comparison category")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        attr = category.replace("-", "_")
        return tuple(item for item in self.status().comparisons if item.primary_outcome == category or bool(getattr(item, attr, False)))[:limit]

    def _comparison(self, topology, targets: dict[str, ExternalRecordingTarget]) -> ExternalRecordingCandidateComparison:
        ids = (topology.identity_id, *topology.comparison_identity_ids)
        contexts = self._identity_contexts(ids)
        members = tuple(
            ExternalRecordingComparisonMember(
                item, contexts[item][0], contexts[item][1], contexts[item][2], contexts[item][3], contexts[item][4],
                targets[item].evidence_state, targets[item].external_recording_id,
            )
            for item in ids
        )
        candidate = members[0]
        comparisons = tuple(self._pairwise_member(candidate, item) for item in members[1:])
        consistency = evaluate_comparison_consistency(tuple(
            (
                candidate.isrc is not None and item.isrc is not None and candidate.isrc == item.isrc,
                item.duration_delta_ms,
                item.version_relationship == "conflict",
            )
            for item in comparisons
        ))
        resolved = tuple(recording_id for item in members if (recording_id := _fresh_resolved_recording_id(item.evidence_state, item.external_recording_id)) is not None)
        if len(resolved) == len(members) and len(set(resolved)) == 1:
            outcome = "all-resolved-same-recording"
        elif len(resolved) == len(members):
            outcome = "all-resolved-disagree"
        elif resolved:
            outcome = "partial-resolution"
        else:
            outcome = "no-resolution"
        states = {item.evidence_state for item in members}
        return ExternalRecordingCandidateComparison(
            topology.candidate_id, topology.canonical_track_id, outcome, candidate, comparisons, consistency,
            "fresh_ambiguous" in states, "fresh_not_found" in states,
            "fresh_insufficient_input" in states, "fresh_lookup_failed" in states,
            "stale" in states, "no_evidence" in states,
        )

    def _identity_contexts(self, identity_ids: tuple[str, ...]) -> dict[str, tuple[str, tuple[str, ...], str | None, int | None, str | None]]:
        placeholders = ",".join("?" for _ in identity_ids)
        rows = self._store.connection.execute(
            f"SELECT identity_id, title, version_label, duration_ms, isrc FROM track_identities WHERE identity_id IN ({placeholders})",
            identity_ids,
        ).fetchall()
        artists = {
            identity_id: tuple(row[0] for row in self._store.connection.execute(
                "SELECT artist_name FROM track_identity_artists WHERE identity_id = ? ORDER BY position", (identity_id,),
            ))
            for identity_id in identity_ids
        }
        return {
            row["identity_id"]: (
                row["title"], artists[row["identity_id"]], _effective_version_cue(row["version_label"], row["title"]),
                row["duration_ms"], row["isrc"],
            )
            for row in rows
        }

    @staticmethod
    def _pairwise_member(candidate: ExternalRecordingComparisonMember, comparison: ExternalRecordingComparisonMember) -> ExternalRecordingComparisonMember:
        duration_delta = None if candidate.duration_ms is None or comparison.duration_ms is None else abs(candidate.duration_ms - comparison.duration_ms)
        isrc_relationship = "missing" if candidate.isrc is None or comparison.isrc is None else "same" if candidate.isrc == comparison.isrc else "different"
        version_relationship = "unknown" if candidate.effective_version_cue is None or comparison.effective_version_cue is None else "conflict" if _versions_conflict(candidate.effective_version_cue, comparison.effective_version_cue) else "compatible"
        pair = evaluate_comparison_consistency(((isrc_relationship == "same", duration_delta, version_relationship == "conflict"),))
        external_relationship = _external_recording_relationship(candidate, comparison)
        return replace(
            comparison,
            duration_delta_ms=duration_delta,
            isrc_relationship=isrc_relationship,
            version_relationship=version_relationship,
            comparison_consistency="consistent" if not (pair.any_isrc_mismatch or pair.any_duration_over_threshold or pair.any_version_conflict) else "inconsistent",
            external_recording_relationship=external_relationship,
        )


@dataclass(frozen=True, slots=True)
class ExternalRecordingCandidateFetchBundle:
    candidate_id: str
    primary_outcome: str
    member_identity_ids: tuple[str, ...]
    lookup_identity_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExternalRecordingCandidateFetchPlan:
    completable_partial_candidates: int
    completable_no_resolution_candidates: int
    blocked_candidates: int
    selected_bundles: tuple[ExternalRecordingCandidateFetchBundle, ...]
    selected_member_identities: int
    already_resolved_members: int
    required_lookup_identity_ids: tuple[str, ...]
    candidate_limit: int
    identity_limit: int
    exceeds_identity_limit: bool
    stratum: str | None = None
    stratum_matching_candidates: int = 0
    stratum_completable_candidates: int = 0
    stratum_blocked_candidates: int = 0
    stratum_completed_candidates: int = 0
    stratum_completable_required_lookup_identity_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExternalRecordingCandidateFetchReport:
    planned_candidates: int
    processed_candidates: int
    planned_lookup_identities: int
    processed_lookup_identities: int
    resolved: int
    ambiguous: int
    not_found: int
    insufficient_input: int
    lookup_failed: int
    skipped_fresh: int
    skipped_candidate_not_completable: int
    skipped_candidate_not_in_stratum: int = 0


class ExternalRecordingCandidateFetchService:
    """Bounded complete-candidate evidence acquisition; it has no matching authority."""

    def __init__(self, store: Phase3Store, binding_id: str, *, positive_ttl_hours: int, negative_ttl_hours: int, ambiguous_ttl_hours: int, now: Callable[[], datetime] | None = None) -> None:
        self._store, self._binding_id = store, binding_id
        self._positive_ttl_hours, self._negative_ttl_hours = positive_ttl_hours, negative_ttl_hours
        self._ambiguous_ttl_hours, self._now = ambiguous_ttl_hours, now or (lambda: datetime.now(UTC))

    def plan(self, candidate_limit: int, identity_limit: int, *, stratum: str | None = None) -> ExternalRecordingCandidateFetchPlan:
        if not 1 <= candidate_limit <= 20:
            raise ValueError("candidate_limit must be between 1 and 20")
        if not 1 <= identity_limit <= 100:
            raise ValueError("identity_limit must be between 1 and 100")
        if stratum not in (None, EXTERNAL_RECORDING_CANDIDATE_STRATUM):
            raise ValueError("unknown external recording candidate stratum")
        status = ExternalRecordingComparisonService(self._store, self._binding_id, self._now).status()
        targets = {item.identity_id: item for item in status.target_preview.targets}
        source = status.comparisons if stratum is None else tuple(item for item in status.comparisons if self._matches_stratum(item, stratum))
        partial_source = tuple(item for item in source if item.primary_outcome == "partial-resolution")
        no_resolution_source = tuple(item for item in source if item.primary_outcome == "no-resolution")
        partial = tuple(self._bundle(item, targets) for item in partial_source)
        no_resolution = tuple(self._bundle(item, targets) for item in no_resolution_source)
        partial = tuple(item for item in partial if item is not None)
        no_resolution = tuple(item for item in no_resolution if item is not None)
        selected = (partial + no_resolution)[:candidate_limit]
        lookup_ids = tuple(sorted({identity_id for item in selected for identity_id in item.lookup_identity_ids}))
        selected_ids = {identity_id for item in selected for identity_id in item.member_identity_ids}
        resolved = sum(targets[identity_id].evidence_state == "fresh_resolved" for identity_id in selected_ids)
        completed = tuple(
            item for item in source
            if (bundle := self._bundle(item, targets, allow_complete=True)) is not None and not bundle.lookup_identity_ids
        )
        completable = partial + no_resolution
        completable_lookup_ids = tuple(sorted({identity_id for item in completable for identity_id in item.lookup_identity_ids}))
        blocked = len(source) - len(completable) - len(completed)
        return ExternalRecordingCandidateFetchPlan(
            len(partial), len(no_resolution), len(partial_source) + len(no_resolution_source) - len(partial) - len(no_resolution),
            selected, len(selected_ids), resolved, lookup_ids, candidate_limit, identity_limit,
            len(lookup_ids) > identity_limit,
            stratum, len(source), len(completable), blocked, len(completed), completable_lookup_ids,
        )

    @staticmethod
    def _matches_stratum(item: ExternalRecordingCandidateComparison, stratum: str) -> bool:
        if stratum != EXTERNAL_RECORDING_CANDIDATE_STRATUM:
            return False
        if len(item.comparisons) != 1:
            return False
        comparison = item.comparisons[0]
        return (
            _has_usable_isrc(item.candidate.isrc)
            and _has_usable_isrc(comparison.isrc)
            and item.candidate.isrc != comparison.isrc
            and item.candidate.duration_ms is not None
            and comparison.duration_ms is not None
            and comparison.duration_delta_ms is not None
            and comparison.duration_delta_ms <= 500
            and comparison.version_relationship != "conflict"
        )

    @staticmethod
    def _bundle(item: ExternalRecordingCandidateComparison, targets: dict[str, ExternalRecordingTarget], *, allow_complete: bool = False) -> ExternalRecordingCandidateFetchBundle | None:
        members = tuple(member.identity_id for member in item.members)
        member_targets = tuple(targets[identity_id] for identity_id in members)
        if not all(target.evidence_state == "fresh_resolved" or target.requires_lookup for target in member_targets):
            return None
        if not any(target.requires_lookup for target in member_targets) and not allow_complete:
            return None
        return ExternalRecordingCandidateFetchBundle(item.candidate_id, item.primary_outcome, members, tuple(target.identity_id for target in member_targets if target.requires_lookup))

    def fetch(self, plan: ExternalRecordingCandidateFetchPlan, lookup: RecordingLookup) -> ExternalRecordingCandidateFetchReport:
        if plan.exceeds_identity_limit:
            raise ValueError("candidate_bundle_exceeds_identity_limit")
        counts: Counter[str] = Counter(planned_candidates=len(plan.selected_bundles), planned_lookup_identities=len(plan.required_lookup_identity_ids))
        seen_lookup_ids: set[str] = set()
        for bundle in plan.selected_bundles:
            refreshed = self.plan_for_candidate(bundle.candidate_id, stratum=plan.stratum)
            if refreshed is None:
                if plan.stratum is not None and not self._candidate_matches_stratum(bundle.candidate_id, plan.stratum):
                    counts["skipped_candidate_not_in_stratum"] += 1
                else:
                    counts["skipped_candidate_not_completable"] += 1
                continue
            targets = ExternalRecordingTargetProjectionService(self._store, self._binding_id, self._now).preview().targets
            current_by_id = {item.identity_id: item for item in targets}
            pending = tuple(current_by_id[identity_id] for identity_id in refreshed.member_identity_ids if current_by_id[identity_id].requires_lookup and identity_id not in seen_lookup_ids)
            if not pending:
                counts["skipped_fresh"] += len(refreshed.member_identity_ids)
                continue
            report = ExternalRecordingEvidenceFetchService(
                self._store, self._binding_id, positive_ttl_hours=self._positive_ttl_hours,
                negative_ttl_hours=self._negative_ttl_hours, ambiguous_ttl_hours=self._ambiguous_ttl_hours, now=self._now,
            ).fetch_targets(pending, lookup)
            seen_lookup_ids.update(item.identity_id for item in pending)
            counts["processed_candidates"] += 1
            for name in ("processed", "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh"):
                counts["processed_lookup_identities" if name == "processed" else name] += getattr(report, name)
        return ExternalRecordingCandidateFetchReport(*(counts[name] for name in (
            "planned_candidates", "processed_candidates", "planned_lookup_identities", "processed_lookup_identities",
            "resolved", "ambiguous", "not_found", "insufficient_input", "lookup_failed", "skipped_fresh", "skipped_candidate_not_completable", "skipped_candidate_not_in_stratum",
        )))

    def plan_for_candidate(self, candidate_id: str, *, stratum: str | None = None) -> ExternalRecordingCandidateFetchBundle | None:
        status = ExternalRecordingComparisonService(self._store, self._binding_id, self._now).status()
        targets = {item.identity_id: item for item in status.target_preview.targets}
        for item in status.comparisons:
            if item.candidate_id == candidate_id:
                if stratum is not None and not self._matches_stratum(item, stratum):
                    return None
                return self._bundle(item, targets, allow_complete=True)
        return None

    def _candidate_matches_stratum(self, candidate_id: str, stratum: str) -> bool:
        status = ExternalRecordingComparisonService(self._store, self._binding_id, self._now).status()
        return any(item.candidate_id == candidate_id and self._matches_stratum(item, stratum) for item in status.comparisons)
