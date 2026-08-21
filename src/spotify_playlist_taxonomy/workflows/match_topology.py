"""Shared read-only topology for current unresolved canonical match candidates."""

from __future__ import annotations

from dataclasses import dataclass

from spotify_playlist_taxonomy.persistence.phase3 import Phase3Store


@dataclass(frozen=True, slots=True)
class UnresolvedMatchCandidateTopology:
    candidate_id: str
    identity_id: str
    canonical_track_id: str
    comparison_identity_ids: tuple[str, ...]


class UnresolvedMatchTopologyService:
    """Projects current account-scoped ambiguous link candidates without mutation."""

    def __init__(self, store: Phase3Store, binding_id: str) -> None:
        self._store, self._binding_id = store, binding_id

    def candidates(self) -> tuple[UnresolvedMatchCandidateTopology, ...]:
        self._store._require_active_binding(self._binding_id)
        rows = self._store.connection.execute(
            """
            SELECT candidate.candidate_id, candidate.identity_id,
                   candidate.canonical_track_id, comparison.identity_id AS comparison_identity_id
            FROM canonical_match_candidates AS candidate
            JOIN canonical_tracks AS canonical
              ON canonical.canonical_track_id = candidate.canonical_track_id
            JOIN track_identities AS identity ON identity.identity_id = candidate.identity_id
            LEFT JOIN account_canonical_identity_links AS candidate_link
              ON candidate_link.account_binding_id = candidate.account_binding_id
             AND candidate_link.identity_id = candidate.identity_id
            LEFT JOIN account_canonical_identity_links AS comparison
              ON comparison.account_binding_id = candidate.account_binding_id
             AND comparison.canonical_track_id = candidate.canonical_track_id
            WHERE candidate.account_binding_id = ?
              AND candidate.action = 'link_existing'
              AND candidate.state = 'ambiguous'
              AND candidate_link.identity_id IS NULL
            ORDER BY candidate.created_at, candidate.candidate_id, comparison.identity_id
            """,
            (self._binding_id,),
        ).fetchall()
        grouped: dict[str, list[str]] = {}
        candidate_rows: dict[str, tuple[str, str]] = {}
        for row in rows:
            candidate_rows.setdefault(
                row["candidate_id"], (row["identity_id"], row["canonical_track_id"])
            )
            if row["comparison_identity_id"] is not None:
                grouped.setdefault(row["candidate_id"], []).append(row["comparison_identity_id"])
        return tuple(
            UnresolvedMatchCandidateTopology(
                candidate_id, identity_id, canonical_track_id,
                tuple(grouped.get(candidate_id, ())),
            )
            for candidate_id, (identity_id, canonical_track_id) in candidate_rows.items()
        )
