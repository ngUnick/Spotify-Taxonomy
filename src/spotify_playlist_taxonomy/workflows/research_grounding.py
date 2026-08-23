"""Read-only external Recording grounding for Phase 5 research subjects."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Callable

from spotify_playlist_taxonomy.persistence.identity_external_evidence import (
    IdentityExternalEvidenceStore,
    normalize_external_entity_id,
)


_UNAVAILABLE_REASONS = (
    "no_active_account_context",
    "no_current_spotify_identity",
    "missing_external_evidence",
    "stale_external_evidence",
    "nonresolved_external_evidence",
    "invalid_recording_id",
    "resolved_disagreement",
)


@dataclass(frozen=True, slots=True)
class ResearchSubjectGrounding:
    """One fail-closed decision about an exact external research subject."""

    canonical_track_id: str
    state: str
    reason: str
    musicbrainz_recording_id: str | None = None

    @property
    def is_exact(self) -> bool:
        return self.state == "exact"


@dataclass(frozen=True, slots=True)
class ResearchGroundingPreview:
    canonical_tracks_total: int
    grounding_exact_canonicals: int
    grounding_unavailable_canonicals: int
    no_active_account_context_canonicals: int
    no_current_spotify_identity_canonicals: int
    missing_external_evidence_canonicals: int
    stale_external_evidence_canonicals: int
    nonresolved_external_evidence_canonicals: int
    invalid_recording_id_canonicals: int
    resolved_disagreement_canonicals: int
    selected: tuple[ResearchSubjectGrounding, ...]


class CanonicalExternalSubjectResolver:
    """Resolve only unanimous fresh MusicBrainz Recording evidence for research."""

    def __init__(
        self, connection: sqlite3.Connection,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.connection = connection
        self.now = now or (lambda: datetime.now(UTC))

    def resolve(self, canonical_track_id: str) -> ResearchSubjectGrounding:
        binding = self.connection.execute(
            "SELECT binding_id FROM platform_account_bindings "
            "WHERE provider='spotify' AND active=1 ORDER BY bound_at LIMIT 1"
        ).fetchone()
        if binding is None:
            return self._unavailable(canonical_track_id, "no_active_account_context")
        identities = tuple(
            row["identity_id"] for row in self.connection.execute(
                "SELECT ti.identity_id FROM account_canonical_identity_links AS link "
                "JOIN track_identities AS ti ON ti.identity_id=link.identity_id "
                "JOIN platform_account_bindings AS account ON account.binding_id=link.account_binding_id "
                "WHERE link.account_binding_id=? AND account.active=1 "
                "AND ti.provider='spotify' AND link.canonical_track_id=? "
                "ORDER BY ti.identity_id",
                (binding["binding_id"], canonical_track_id),
            )
        )
        if not identities:
            return self._unavailable(canonical_track_id, "no_current_spotify_identity")
        evidence = {
            item.identity_id: item
            for item in IdentityExternalEvidenceStore(self.connection).for_identities(identities)
            if item.provider == "musicbrainz" and item.entity_type == "recording"
        }
        if any(identity_id not in evidence for identity_id in identities):
            return self._unavailable(canonical_track_id, "missing_external_evidence")
        current = tuple(evidence[identity_id] for identity_id in identities)
        now = self.now()
        if any(self._stale(item.lookup_state, item.fresh_until, now) for item in current):
            return self._unavailable(canonical_track_id, "stale_external_evidence")
        if any(item.lookup_state != "resolved" for item in current):
            return self._unavailable(canonical_track_id, "nonresolved_external_evidence")
        try:
            recording_ids = {
                normalize_external_entity_id("musicbrainz", "recording", item.external_id)
                for item in current
            }
        except ValueError:
            return self._unavailable(canonical_track_id, "invalid_recording_id")
        if None in recording_ids:
            return self._unavailable(canonical_track_id, "invalid_recording_id")
        if len(recording_ids) != 1:
            return self._unavailable(canonical_track_id, "resolved_disagreement")
        recording_id = next(iter(recording_ids))
        return ResearchSubjectGrounding(canonical_track_id, "exact", "unanimous_fresh_resolved_recording", recording_id)

    @staticmethod
    def _stale(state: str, fresh_until: str | None, now: datetime) -> bool:
        if state == "stale":
            return True
        if fresh_until is None:
            return False
        return datetime.fromisoformat(fresh_until.replace("Z", "+00:00")).astimezone(UTC) <= now

    @staticmethod
    def _unavailable(canonical_track_id: str, reason: str) -> ResearchSubjectGrounding:
        return ResearchSubjectGrounding(canonical_track_id, "unavailable", reason)


class ResearchGroundingPreviewService:
    """Deterministic local coverage projection with no provider dependency."""

    def __init__(self, resolver: CanonicalExternalSubjectResolver) -> None:
        self.resolver = resolver

    def preview(self, limit: int) -> ResearchGroundingPreview:
        if not 1 <= limit <= 25:
            raise ValueError("limit must be between 1 and 25")
        canonical_ids = tuple(
            row["canonical_track_id"] for row in self.resolver.connection.execute(
                "SELECT canonical_track_id FROM canonical_tracks ORDER BY canonical_track_id"
            )
        )
        all_results = tuple(self.resolver.resolve(canonical_id) for canonical_id in canonical_ids)
        counts = {reason: 0 for reason in _UNAVAILABLE_REASONS}
        for item in all_results:
            if not item.is_exact:
                counts[item.reason] += 1
        exact = sum(item.is_exact for item in all_results)
        return ResearchGroundingPreview(
            len(all_results), exact, len(all_results) - exact,
            *(counts[reason] for reason in _UNAVAILABLE_REASONS), all_results[:limit],
        )
