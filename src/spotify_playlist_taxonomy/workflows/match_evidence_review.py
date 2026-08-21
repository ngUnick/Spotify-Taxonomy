"""Read-only, bounded local review rows for canonical-match evidence anomalies."""

from __future__ import annotations

from dataclasses import dataclass

from spotify_playlist_taxonomy.persistence.phase3 import Phase3StateError, Phase3Store
from spotify_playlist_taxonomy.workflows.match_evidence import version_cue


ANOMALY_CATEGORIES = (
    "same-isrc-duration-gt-5s",
    "same-isrc-duration-gt-2s",
    "different-isrc-duration-le-1s",
    "different-isrc-exact-duration",
    "same-isrc-exact-duration",
)


@dataclass(frozen=True, slots=True)
class EvidenceAnomaly:
    candidate_id: str
    identity_id: str
    identity_title: str
    identity_artists: tuple[str, ...]
    identity_version_cue: str | None
    identity_duration_ms: int
    identity_isrc: str
    canonical_track_id: str
    comparison_identity_id: str
    comparison_title: str
    comparison_artists: tuple[str, ...]
    comparison_version_cue: str | None
    comparison_duration_ms: int
    comparison_isrc: str
    duration_delta_ms: int
    category: str
    paired_create_new_candidate_id: str | None


class MatchEvidenceReviewService:
    """Projects one deterministic row per comparison; never mutates local state."""

    def __init__(self, store: Phase3Store, binding_id: str) -> None:
        self._store, self._binding_id = store, binding_id

    def anomalies(self, category: str, limit: int) -> tuple[EvidenceAnomaly, ...]:
        if category not in ANOMALY_CATEGORIES:
            raise ValueError("unknown evidence category")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self._store._require_active_binding(self._binding_id)
        rows = self._store.connection.execute(
            """
            SELECT candidate.candidate_id, candidate.identity_id, candidate.canonical_track_id,
                   identity.title AS identity_title, identity.version_label AS identity_version_label,
                   identity.duration_ms AS identity_duration_ms, identity.isrc AS identity_isrc,
                   comparison.identity_id AS comparison_identity_id,
                   comparison_track.title AS comparison_title,
                   comparison_track.version_label AS comparison_version_label,
                   comparison_track.duration_ms AS comparison_duration_ms,
                   comparison_track.isrc AS comparison_isrc,
                   paired.candidate_id AS paired_create_new_candidate_id
            FROM canonical_match_candidates AS candidate
            JOIN track_identities AS identity ON identity.identity_id = candidate.identity_id
            JOIN account_canonical_identity_links AS comparison
              ON comparison.account_binding_id = candidate.account_binding_id
             AND comparison.canonical_track_id = candidate.canonical_track_id
            JOIN track_identities AS comparison_track ON comparison_track.identity_id = comparison.identity_id
            LEFT JOIN canonical_match_candidates AS paired
              ON paired.account_binding_id = candidate.account_binding_id
             AND paired.identity_id = candidate.identity_id
             AND paired.action = 'create_new'
             AND paired.state IN ('candidate', 'ambiguous', 'user_confirmation_required')
            WHERE candidate.account_binding_id = ? AND candidate.action = 'link_existing'
              AND candidate.state = 'ambiguous'
              AND identity.duration_ms IS NOT NULL AND identity.isrc IS NOT NULL
              AND comparison_track.duration_ms IS NOT NULL AND comparison_track.isrc IS NOT NULL
            ORDER BY candidate.candidate_id, comparison.identity_id, paired.candidate_id
            """,
            (self._binding_id,),
        ).fetchall()
        selected: list[EvidenceAnomaly] = []
        for row in rows:
            delta = abs(row["identity_duration_ms"] - row["comparison_duration_ms"])
            if not _matches_category(category, row["identity_isrc"] == row["comparison_isrc"], delta):
                continue
            selected.append(EvidenceAnomaly(
                row["candidate_id"], row["identity_id"], row["identity_title"],
                self._artists(row["identity_id"]),
                version_cue(row["identity_version_label"], row["identity_title"]),
                row["identity_duration_ms"], row["identity_isrc"], row["canonical_track_id"],
                row["comparison_identity_id"], row["comparison_title"],
                self._artists(row["comparison_identity_id"]),
                version_cue(row["comparison_version_label"], row["comparison_title"]),
                row["comparison_duration_ms"], row["comparison_isrc"], delta,
                category, row["paired_create_new_candidate_id"],
            ))
            if len(selected) == limit:
                break
        return tuple(selected)

    def _artists(self, identity_id: str) -> tuple[str, ...]:
        return tuple(row[0] for row in self._store.connection.execute(
            "SELECT artist_name FROM track_identity_artists WHERE identity_id = ? ORDER BY position",
            (identity_id,),
        ))


def _matches_category(category: str, same_isrc: bool, delta: int) -> bool:
    return {
        "same-isrc-duration-gt-5s": same_isrc and delta > 5000,
        "same-isrc-duration-gt-2s": same_isrc and delta > 2000,
        "different-isrc-duration-le-1s": not same_isrc and delta <= 1000,
        "different-isrc-exact-duration": not same_isrc and delta == 0,
        "same-isrc-exact-duration": same_isrc and delta == 0,
    }[category]
