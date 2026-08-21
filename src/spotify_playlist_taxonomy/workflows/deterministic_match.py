"""Pure Tier A recording-evidence eligibility and local preview only."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from spotify_playlist_taxonomy.domain.ingestion import MatchCandidateAction, MatchCandidateState
from spotify_playlist_taxonomy.domain.models import IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.domain.policies import normalize_identity_text
from spotify_playlist_taxonomy.persistence.phase3 import (
    Phase3Store, _effective_version_cue, _versions_conflict,
)

TIER_A_POLICY_ID = "canonical-tier-a.v1"
TIER_A_DURATION_DELTA_MS = 500


@dataclass(frozen=True, slots=True)
class TierAComparison:
    identity_id: str
    title: str
    artists: tuple[str, ...]
    version_cue: str | None
    duration_ms: int | None
    isrc: str | None
    isrc_evidence_state: str
    duration_delta_ms: int | None


@dataclass(frozen=True, slots=True)
class TierAEligibility:
    candidate_id: str
    eligible: bool
    blocked_reasons: tuple[str, ...]
    identity_id: str | None
    identity_title: str | None
    identity_artists: tuple[str, ...]
    identity_version_cue: str | None
    identity_duration_ms: int | None
    identity_isrc: str | None
    canonical_track_id: str | None
    canonical_version_cue: str | None
    comparisons: tuple[TierAComparison, ...]


@dataclass(frozen=True, slots=True)
class TierAPreview:
    total_candidates: int
    eligible: int
    blocked: int
    blocked_reasons: tuple[tuple[str, int], ...]
    eligible_exact_duration: int
    eligible_duration_le_250ms: int
    eligible_duration_le_500ms: int
    eligible_with_explicit_compatible_version_cue: int
    eligible_with_no_explicit_version_cue: int
    eligible_multiple_comparisons: int
    rows: tuple[TierAEligibility, ...]


class TierAEligibilityService:
    """Read-only evaluator; it deliberately has no mutation methods."""

    def __init__(self, store: Phase3Store, binding_id: str) -> None:
        self.store, self.binding_id = store, binding_id

    def evaluate(self, candidate_id: str) -> TierAEligibility:
        row = self.store.connection.execute(
            "SELECT * FROM canonical_match_candidates WHERE candidate_id=? AND account_binding_id=?",
            (candidate_id, self.binding_id),
        ).fetchone()
        if row is None:
            return self._blocked(candidate_id, "account_scope_invalid")
        reasons: list[str] = []
        if row["action"] != MatchCandidateAction.LINK_EXISTING.value: reasons.append("wrong_candidate_action")
        if row["state"] != MatchCandidateState.AMBIGUOUS.value: reasons.append("wrong_candidate_state")
        identity = self._identity(row["identity_id"])
        if identity is None: return self._blocked(candidate_id, *reasons, "identity_missing")
        linked = self.store.connection.execute(
            "SELECT 1 FROM account_canonical_identity_links WHERE account_binding_id=? AND identity_id=?",
            (self.binding_id, identity["identity_id"]),
        ).fetchone()
        if linked is not None: reasons.append("identity_already_linked")
        if identity["identity_id"] not in set(self.store.published_identity_ids(self.binding_id)):
            reasons.append("identity_not_currently_eligible")
        canonical = self.store.connection.execute(
            "SELECT * FROM canonical_tracks WHERE canonical_track_id=?", (row["canonical_track_id"],)
        ).fetchone()
        if canonical is None:
            return self._result(candidate_id, reasons + ["canonical_missing"], identity, None, ())
        if not self._same_context(identity, canonical["canonical_track_id"]): reasons.append("normalized_context_changed")
        comparisons = self._comparisons(canonical["canonical_track_id"], identity)
        if not comparisons: reasons.append("no_comparison_identity")
        identity_isrc, identity_isrc_state = self._isrc_evidence(identity)
        if identity_isrc_state == "missing": reasons.append("isrc_missing")
        if identity_isrc_state == "malformed": reasons.append("malformed_evidence")
        identity_duration = identity["duration_ms"]
        if identity_duration is None: reasons.append("duration_missing")
        elif not _valid_duration(identity_duration): reasons.append("malformed_evidence")
        identity_cue = _effective_version_cue(identity["version_label"], identity["title"])
        canonical_cue = _effective_version_cue(None, canonical["display_title"])
        if _versions_conflict(identity_cue, canonical_cue): reasons.append("version_conflict")
        for comparison in comparisons:
            if comparison.isrc_evidence_state == "missing": reasons.append("isrc_missing")
            elif comparison.isrc_evidence_state == "malformed": reasons.append("malformed_evidence")
            elif identity_isrc is None: pass
            elif comparison.isrc != identity_isrc: reasons.append("isrc_mismatch")
            if comparison.duration_ms is None: reasons.append("duration_missing")
            elif not _valid_duration(comparison.duration_ms): reasons.append("malformed_evidence")
            elif _valid_duration(identity_duration) and comparison.duration_delta_ms is not None and comparison.duration_delta_ms > TIER_A_DURATION_DELTA_MS:
                reasons.append("duration_over_threshold")
            if _versions_conflict(identity_cue, comparison.version_cue): reasons.append("version_conflict")
        if len(comparisons) > 1 and any(reason in reasons for reason in ("isrc_mismatch", "duration_over_threshold", "version_conflict", "malformed_evidence", "duration_missing")):
            reasons.append("comparison_inconsistent")
        return self._result(candidate_id, reasons, identity, canonical["canonical_track_id"], comparisons)

    def preview(self, *, limit: int, eligible_only: bool) -> TierAPreview:
        if not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
        self.store._require_active_binding(self.binding_id)
        ids = tuple(row[0] for row in self.store.connection.execute(
            "SELECT candidate_id FROM canonical_match_candidates WHERE account_binding_id=? "
            "AND action='link_existing' AND state='ambiguous' ORDER BY created_at, candidate_id", (self.binding_id,)
        ))
        all_items = tuple(self.evaluate(candidate_id) for candidate_id in ids)
        eligible_items = tuple(item for item in all_items if item.eligible)
        selected = eligible_items if eligible_only else all_items
        blocked = Counter(reason for item in all_items if not item.eligible for reason in item.blocked_reasons)
        return TierAPreview(
            len(all_items), len(eligible_items), len(all_items)-len(eligible_items), tuple(sorted(blocked.items())),
            sum(item.eligible and all(delta == 0 for delta in (c.duration_delta_ms for c in item.comparisons)) for item in eligible_items),
            sum(item.eligible and all((delta or 0) <= 250 for delta in (c.duration_delta_ms for c in item.comparisons)) for item in eligible_items),
            len(eligible_items),
            sum(any(cue is not None for cue in (item.identity_version_cue, item.canonical_version_cue, *(c.version_cue for c in item.comparisons))) for item in eligible_items),
            sum(all(cue is None for cue in (item.identity_version_cue, item.canonical_version_cue, *(c.version_cue for c in item.comparisons))) for item in eligible_items),
            sum(len(item.comparisons) > 1 for item in eligible_items),
            selected[:limit],
        )

    def _identity(self, identity_id: str):
        return self.store.connection.execute("SELECT * FROM track_identities WHERE identity_id=?", (identity_id,)).fetchone()

    def _comparisons(self, canonical_id: str, identity) -> tuple[TierAComparison, ...]:
        rows = self.store.connection.execute(
            "SELECT i.* FROM account_canonical_identity_links l JOIN track_identities i ON i.identity_id=l.identity_id "
            "WHERE l.account_binding_id=? AND l.canonical_track_id=? ORDER BY i.identity_id", (self.binding_id, canonical_id)
        ).fetchall()
        return tuple(
            TierAComparison(
                row["identity_id"], row["title"], self._artists(row["identity_id"]),
                _effective_version_cue(row["version_label"], row["title"]), row["duration_ms"],
                *self._isrc_evidence(row),
                None if not _valid_duration(identity["duration_ms"]) or not _valid_duration(row["duration_ms"])
                else abs(identity["duration_ms"] - row["duration_ms"]),
            )
            for row in rows
        )

    def _same_context(self, identity, canonical_id: str) -> bool:
        canonical = self.store.connection.execute("SELECT display_title FROM canonical_tracks WHERE canonical_track_id=?", (canonical_id,)).fetchone()
        artists = tuple(row[0] for row in self.store.connection.execute("SELECT artist_name FROM canonical_track_artists WHERE canonical_track_id=? ORDER BY position", (canonical_id,)))
        return (normalize_identity_text(identity["title"]), tuple(sorted(normalize_identity_text(x) for x in self._artists(identity["identity_id"])))) == (normalize_identity_text(canonical["display_title"]), tuple(sorted(normalize_identity_text(x) for x in artists)))

    def _artists(self, identity_id: str) -> tuple[str, ...]:
        return tuple(row[0] for row in self.store.connection.execute("SELECT artist_name FROM track_identity_artists WHERE identity_id=? ORDER BY position", (identity_id,)))

    @staticmethod
    def _isrc_evidence(row) -> tuple[str | None, str]:
        value = row["isrc"]
        if value is None:
            return None, "missing"
        try:
            return PlatformTrackIdentity("synthetic", IdentityProvider.SPOTIFY, "Synthetic", ("Synthetic",), isrc=value).isrc, "valid"
        except (TypeError, ValueError):
            return None, "malformed"

    def _result(self, candidate_id: str, reasons: list[str], identity, canonical_id: str | None, comparisons: tuple[TierAComparison, ...]) -> TierAEligibility:
        unique = tuple(sorted(set(reasons)))
        canonical_cue = None
        if canonical_id is not None:
            row = self.store.connection.execute(
                "SELECT display_title FROM canonical_tracks WHERE canonical_track_id=?", (canonical_id,)
            ).fetchone()
            canonical_cue = _effective_version_cue(None, row["display_title"])
        identity_isrc, _ = self._isrc_evidence(identity)
        return TierAEligibility(
            candidate_id, not unique, unique, identity["identity_id"], identity["title"],
            self._artists(identity["identity_id"]),
            _effective_version_cue(identity["version_label"], identity["title"]), identity["duration_ms"],
            identity_isrc, canonical_id, canonical_cue, comparisons,
        )

    def _blocked(self, candidate_id: str, *reasons: str) -> TierAEligibility:
        return TierAEligibility(candidate_id, False, tuple(sorted(set(reasons))), None, None, (), None, None, None, None, None, ())


def _valid_duration(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0
