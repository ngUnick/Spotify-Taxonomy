"""Read-only aggregate evidence analysis for unresolved canonical matches."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from spotify_playlist_taxonomy.persistence.phase3 import (
    Phase3Store,
    _version_from_title,
    _versions_conflict,
)


@dataclass(frozen=True, slots=True)
class MatchEvidenceReport:
    metrics: tuple[tuple[str, int], ...]


class MatchEvidenceService:
    """Projects local aggregate evidence only; it never changes match state."""

    def __init__(self, store: Phase3Store, binding_id: str) -> None:
        self.store, self.binding_id = store, binding_id

    def report(self) -> MatchEvidenceReport:
        self.store._require_active_binding(self.binding_id)
        counts: Counter[str] = Counter()
        rows = self.store.connection.execute(
            """
            SELECT candidate.canonical_track_id, identity.duration_ms, identity.isrc,
                   identity.version_label, identity.title
            FROM canonical_match_candidates AS candidate
            JOIN track_identities AS identity ON identity.identity_id = candidate.identity_id
            WHERE candidate.account_binding_id = ? AND candidate.action = 'link_existing'
              AND candidate.state = 'ambiguous'
            ORDER BY candidate.created_at, candidate.candidate_id
            """,
            (self.binding_id,),
        )
        for unresolved in rows:
            counts["unresolved_identity_items"] += 1
            if unresolved["duration_ms"] is None:
                counts["unresolved_duration_missing"] += 1
            if unresolved["isrc"] is None:
                counts["unresolved_isrc_missing"] += 1
            comparisons = tuple(self.store.connection.execute(
                """
                SELECT identity.duration_ms, identity.isrc, identity.version_label, identity.title
                FROM account_canonical_identity_links AS link
                JOIN track_identities AS identity ON identity.identity_id = link.identity_id
                WHERE link.account_binding_id = ? AND link.canonical_track_id = ?
                ORDER BY link.identity_id
                """,
                (self.binding_id, unresolved["canonical_track_id"]),
            ))
            if not comparisons:
                counts["no_comparison_identity"] += 1
                continue
            if len(comparisons) == 1:
                counts["exactly_one_comparison_identity"] += 1
            else:
                counts["multiple_comparison_identities"] += 1
            deltas: list[int] = []
            for comparison in comparisons:
                self._comparison(counts, unresolved, comparison, deltas)
            if deltas:
                counts["candidate_any_duration_le_1s"] += int(any(value <= 1000 for value in deltas))
                counts["candidate_all_duration_le_1s"] += int(all(value <= 1000 for value in deltas))
        keys = (
            "unresolved_identity_items", "no_comparison_identity",
            "exactly_one_comparison_identity", "multiple_comparison_identities",
            "unresolved_duration_missing", "comparison_duration_missing",
            "comparison_pairs_with_duration", "unresolved_isrc_missing",
            "comparison_isrc_missing", "comparison_pairs_with_isrc",
            "delta_exact", "delta_le_250ms", "delta_le_500ms", "delta_le_1s",
            "delta_le_2s", "delta_le_5s", "delta_gt_5s",
            "same_isrc", "different_isrc", "missing_isrc",
            "same_isrc_exact_duration", "same_isrc_duration_le_250ms",
            "same_isrc_duration_le_500ms", "same_isrc_duration_le_1s",
            "same_isrc_duration_le_2s", "same_isrc_duration_gt_2s",
            "version_no_explicit_conflict", "version_same_or_compatible",
            "version_explicit_conflict", "version_neither_explicit_cue",
            "version_uncertain_or_unavailable",
            "same_isrc_duration_le_1s_no_version_conflict",
            "duration_le_1s_no_version_conflict_isrc_missing",
            "duration_le_1s_different_isrc", "same_isrc_version_conflict",
            "duration_gt_5s_same_isrc", "duration_gt_5s_no_version_conflict",
            "candidate_any_duration_le_1s", "candidate_all_duration_le_1s",
        )
        return MatchEvidenceReport(tuple((key, counts[key]) for key in keys))

    @staticmethod
    def _comparison(counts: Counter[str], unresolved: object, comparison: object, deltas: list[int]) -> None:
        # sqlite Row supports mapping access; keep only aggregate values after this method returns.
        unresolved_duration = unresolved["duration_ms"]  # type: ignore[index]
        comparison_duration = comparison["duration_ms"]  # type: ignore[index]
        same_isrc = False
        if unresolved["isrc"] is None or comparison["isrc"] is None:  # type: ignore[index]
            counts["missing_isrc"] += 1; counts["comparison_isrc_missing"] += 1
        else:
            counts["comparison_pairs_with_isrc"] += 1
            same_isrc = unresolved["isrc"] == comparison["isrc"]  # type: ignore[index]
            counts["same_isrc" if same_isrc else "different_isrc"] += 1
        unresolved_cue = version_cue(unresolved["version_label"], unresolved["title"])  # type: ignore[index]
        comparison_cue = version_cue(comparison["version_label"], comparison["title"])  # type: ignore[index]
        conflict = _versions_conflict(unresolved_cue, comparison_cue)
        if conflict:
            counts["version_explicit_conflict"] += 1
        elif unresolved_cue is None and comparison_cue is None:
            counts["version_neither_explicit_cue"] += 1; counts["version_no_explicit_conflict"] += 1
        elif unresolved_cue is None or comparison_cue is None:
            counts["version_uncertain_or_unavailable"] += 1; counts["version_no_explicit_conflict"] += 1
        else:
            counts["version_same_or_compatible"] += 1; counts["version_no_explicit_conflict"] += 1
        if unresolved_duration is None or comparison_duration is None:
            counts["comparison_duration_missing"] += 1
            return
        counts["comparison_pairs_with_duration"] += 1
        delta = abs(unresolved_duration - comparison_duration)
        deltas.append(delta)
        if delta == 0: counts["delta_exact"] += 1
        if delta <= 250: counts["delta_le_250ms"] += 1
        if delta <= 500: counts["delta_le_500ms"] += 1
        if delta <= 1000: counts["delta_le_1s"] += 1
        if delta <= 2000: counts["delta_le_2s"] += 1
        if delta <= 5000: counts["delta_le_5s"] += 1
        if delta > 5000: counts["delta_gt_5s"] += 1
        if same_isrc:
            if delta == 0: counts["same_isrc_exact_duration"] += 1
            if delta <= 250: counts["same_isrc_duration_le_250ms"] += 1
            if delta <= 500: counts["same_isrc_duration_le_500ms"] += 1
            if delta <= 1000: counts["same_isrc_duration_le_1s"] += 1
            if delta <= 2000: counts["same_isrc_duration_le_2s"] += 1
            if delta > 2000: counts["same_isrc_duration_gt_2s"] += 1
            if conflict: counts["same_isrc_version_conflict"] += 1
            if delta > 5000: counts["duration_gt_5s_same_isrc"] += 1
        if delta <= 1000 and not conflict and not same_isrc and (
            unresolved["isrc"] is None or comparison["isrc"] is None  # type: ignore[index]
        ):
            counts["duration_le_1s_no_version_conflict_isrc_missing"] += 1
        if delta <= 1000 and not conflict and same_isrc:
            counts["same_isrc_duration_le_1s_no_version_conflict"] += 1
        if delta <= 1000 and not conflict and not same_isrc and (
            unresolved["isrc"] is not None and comparison["isrc"] is not None  # type: ignore[index]
        ):
            counts["duration_le_1s_different_isrc"] += 1
        if delta > 5000 and not conflict:
            counts["duration_gt_5s_no_version_conflict"] += 1


def version_cue(version_label: str | None, title: str) -> str | None:
    """Use persisted label when available, otherwise existing title-marker semantics."""
    return version_label or _version_from_title(title)
