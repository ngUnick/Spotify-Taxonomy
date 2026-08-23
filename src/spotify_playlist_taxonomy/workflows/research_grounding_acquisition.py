"""Bounded acquisition of exact MusicBrainz Recording research grounding."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
import sqlite3
from typing import Callable

from spotify_playlist_taxonomy.persistence.identity_external_evidence import (
    IdentityExternalEvidenceStore,
)
from spotify_playlist_taxonomy.workflows.classification_readiness import (
    ClassificationReadinessService,
)
from spotify_playlist_taxonomy.workflows.external_recording_targets import (
    ExternalRecordingIdentityEvidenceFetcher,
    RecordingLookup,
    _evidence_state,
    _has_usable_isrc,
)
from spotify_playlist_taxonomy.workflows.phase11 import (
    exact_spotify_population_mappings,
    is_exact_spotify_population_mapping,
)
from spotify_playlist_taxonomy.workflows.research_grounding import (
    CanonicalExternalSubjectResolver,
)


@dataclass(frozen=True, slots=True)
class ResearchGroundingAcquisitionTarget:
    canonical_track_id: str
    identity_id: str | None
    state: str
    reason: str

    @property
    def eligible(self) -> bool:
        return self.state == "eligible"


@dataclass(frozen=True, slots=True)
class ResearchGroundingAcquisitionPreview:
    research_needed_canonicals: int
    grounding_exact_canonicals: int
    grounding_acquisition_eligible_canonicals: int
    grounding_acquisition_unavailable_canonicals: int
    unavailable_reason_counts: tuple[tuple[str, int], ...]
    targets: tuple[ResearchGroundingAcquisitionTarget, ...]


@dataclass(frozen=True, slots=True)
class ResearchGroundingAcquisitionFetchReport:
    selected: int
    processed: int
    resolved: int
    ambiguous: int
    not_found: int
    insufficient_input: int
    lookup_failed: int
    skipped_stale_or_ineligible: int
    exactly_grounded_canonical_ids: tuple[str, ...]


class ResearchGroundingAcquisitionService:
    """Select only readiness-approved canonicals; it owns no matching decision."""

    def __init__(
        self, connection: sqlite3.Connection, readiness: ClassificationReadinessService,
        resolver: CanonicalExternalSubjectResolver, *, positive_ttl_hours: int,
        negative_ttl_hours: int, ambiguous_ttl_hours: int,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection = connection
        self.readiness = readiness
        self.resolver = resolver
        self.positive_ttl_hours = positive_ttl_hours
        self.negative_ttl_hours = negative_ttl_hours
        self.ambiguous_ttl_hours = ambiguous_ttl_hours
        self.now = now or (lambda: datetime.now(UTC))

    def preview(self) -> ResearchGroundingAcquisitionPreview:
        report = self.readiness.report()
        canonical_ids = report.research_needed_canonical_ids
        binding = self.connection.execute(
            "SELECT binding_id FROM platform_account_bindings WHERE active=1 ORDER BY bound_at LIMIT 1"
        ).fetchone()
        mappings = {} if binding is None else exact_spotify_population_mappings(
            self.connection, binding["binding_id"], canonical_ids,
        )
        identity_ids = tuple(sorted({
            rows[0][0] for rows in mappings.values()
            if is_exact_spotify_population_mapping(rows)
        }))
        isrc_by_identity = {
            row["identity_id"]: row["isrc"] for row in self.connection.execute(
                "SELECT identity_id, isrc FROM track_identities WHERE identity_id IN ("
                + ",".join("?" for _ in identity_ids) + ")",
                identity_ids,
            )
        } if identity_ids else {}
        evidence_by_identity = {
            item.identity_id: item
            for item in IdentityExternalEvidenceStore(self.connection).for_identities(identity_ids)
            if item.provider == "musicbrainz" and item.entity_type == "recording"
        }
        now = self.now()
        targets: list[ResearchGroundingAcquisitionTarget] = []
        for canonical_id in canonical_ids:
            rows = mappings.get(canonical_id, ())
            if not is_exact_spotify_population_mapping(rows):
                targets.append(ResearchGroundingAcquisitionTarget(
                    canonical_id, None, "unavailable", "mapping_not_safe",
                ))
                continue
            identity_id = rows[0][0]
            if self.resolver.resolve(canonical_id).is_exact:
                targets.append(ResearchGroundingAcquisitionTarget(
                    canonical_id, identity_id, "unavailable", "already_exact_grounded",
                ))
                continue
            isrc = isrc_by_identity.get(identity_id)
            if isrc is None:
                targets.append(ResearchGroundingAcquisitionTarget(
                    canonical_id, identity_id, "unavailable", "missing_isrc",
                ))
                continue
            if not _has_usable_isrc(isrc):
                targets.append(ResearchGroundingAcquisitionTarget(
                    canonical_id, identity_id, "unavailable", "invalid_isrc",
                ))
                continue
            evidence_state, _ = _evidence_state(evidence_by_identity.get(identity_id), now)
            if evidence_state not in {"no_evidence", "stale"}:
                targets.append(ResearchGroundingAcquisitionTarget(
                    canonical_id, identity_id, "unavailable", "external_evidence_not_due",
                ))
                continue
            targets.append(ResearchGroundingAcquisitionTarget(
                canonical_id, identity_id, "eligible", evidence_state,
            ))
        reason_counts = Counter(item.reason for item in targets if not item.eligible)
        exact_count = sum(item.reason == "already_exact_grounded" for item in targets)
        eligible_count = sum(item.eligible for item in targets)
        return ResearchGroundingAcquisitionPreview(
            len(targets), exact_count, eligible_count, len(targets) - eligible_count,
            tuple(sorted(reason_counts.items())), tuple(targets),
        )

    def plan(self, limit: int) -> tuple[ResearchGroundingAcquisitionTarget, ...]:
        if not 1 <= limit <= 25:
            raise ValueError("acquisition limit must be between 1 and 25")
        return tuple(item for item in self.preview().targets if item.eligible)[:limit]

    def fetch(
        self, selected: tuple[ResearchGroundingAcquisitionTarget, ...], lookup: RecordingLookup,
    ) -> ResearchGroundingAcquisitionFetchReport:
        counts: Counter[str] = Counter(selected=len(selected))
        exactly_grounded: list[str] = []
        fetcher = ExternalRecordingIdentityEvidenceFetcher(
            self.connection, positive_ttl_hours=self.positive_ttl_hours,
            negative_ttl_hours=self.negative_ttl_hours,
            ambiguous_ttl_hours=self.ambiguous_ttl_hours, now=self.now,
        )
        for target in selected:
            current = {item.canonical_track_id: item for item in self.preview().targets}
            refreshed = current.get(target.canonical_track_id)
            if refreshed is None or not refreshed.eligible or refreshed.identity_id != target.identity_id:
                counts["skipped_stale_or_ineligible"] += 1
                continue
            counts["processed"] += 1
            counts[fetcher.fetch(target.identity_id, lookup)] += 1
            if self.resolver.resolve(target.canonical_track_id).is_exact:
                exactly_grounded.append(target.canonical_track_id)
        return ResearchGroundingAcquisitionFetchReport(
            *(counts[name] for name in (
                "selected", "processed", "resolved", "ambiguous", "not_found",
                "insufficient_input", "lookup_failed", "skipped_stale_or_ineligible",
            )), tuple(exactly_grounded),
        )
