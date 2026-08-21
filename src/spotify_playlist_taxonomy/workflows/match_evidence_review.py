"""Read-only, bounded local review rows for canonical-match evidence anomalies."""

from __future__ import annotations

from dataclasses import dataclass

from spotify_playlist_taxonomy.persistence.phase3 import Phase3StateError, Phase3Store, _versions_conflict
from spotify_playlist_taxonomy.workflows.match_evidence import version_cue
from spotify_playlist_taxonomy.workflows.deterministic_match import evaluate_comparison_consistency


ANOMALY_CATEGORIES = (
    "same-isrc-duration-gt-5s",
    "same-isrc-duration-gt-2s",
    "different-isrc-duration-le-1s",
    "different-isrc-exact-duration",
    "same-isrc-exact-duration",
    "same-isrc-duration-501-to-1000ms",
    "same-isrc-duration-1001-to-2000ms",
    "same-isrc-duration-2001-to-5000ms",
    "same-isrc-duration-gt-5000ms",
    "different-isrc-duration-le-500ms",
    "different-isrc-duration-501-to-1000ms",
    "different-isrc-duration-1001-to-2000ms",
    "different-isrc-duration-2001-to-5000ms",
    "different-isrc-duration-gt-5000ms",
    "comparison-inconsistent", "multiple-comparisons", "multiple-comparisons-all-agree", "multiple-comparisons-duration-band-mixed",
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

@dataclass(frozen=True, slots=True)
class CandidateEvidenceDetail:
    candidate_id: str; identity_id: str; canonical_track_id: str; comparisons: tuple[EvidenceAnomaly, ...]
    comparison_consistent: bool; min_duration_delta_ms: int; max_duration_delta_ms: int; duration_band_mixed: bool



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
        if category in _CANDIDATE_CATEGORIES:
            return ()
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

    def candidate_details(self, category: str, limit: int) -> tuple[CandidateEvidenceDetail, ...]:
        if category not in _CANDIDATE_CATEGORIES or not 1 <= limit <= 100: raise ValueError("unknown evidence category")
        rows = self._store.connection.execute("""
            SELECT c.candidate_id,c.identity_id,c.canonical_track_id,i.title identity_title,i.version_label identity_version_label,i.duration_ms identity_duration_ms,i.isrc identity_isrc,l.identity_id comparison_identity_id,ct.title comparison_title,ct.version_label comparison_version_label,ct.duration_ms comparison_duration_ms,ct.isrc comparison_isrc
            FROM canonical_match_candidates c JOIN track_identities i ON i.identity_id=c.identity_id JOIN account_canonical_identity_links l ON l.account_binding_id=c.account_binding_id AND l.canonical_track_id=c.canonical_track_id JOIN track_identities ct ON ct.identity_id=l.identity_id
            WHERE c.account_binding_id=? AND c.action='link_existing' AND c.state='ambiguous' ORDER BY c.created_at,c.candidate_id,l.identity_id
        """, (self._binding_id,)).fetchall()
        groups: dict[str,list[EvidenceAnomaly]] = {}
        for row in rows:
            delta=abs(row['identity_duration_ms']-row['comparison_duration_ms'])
            item=EvidenceAnomaly(row['candidate_id'],row['identity_id'],row['identity_title'],self._artists(row['identity_id']),version_cue(row['identity_version_label'],row['identity_title']),row['identity_duration_ms'],row['identity_isrc'],row['canonical_track_id'],row['comparison_identity_id'],row['comparison_title'],self._artists(row['comparison_identity_id']),version_cue(row['comparison_version_label'],row['comparison_title']),row['comparison_duration_ms'],row['comparison_isrc'],delta,_band_name(delta),None)
            groups.setdefault(item.candidate_id,[]).append(item)
        result=[]
        for key, items in groups.items():
            comparisons=tuple(items); deltas=[x.duration_delta_ms for x in comparisons]
            facts = evaluate_comparison_consistency(tuple((x.identity_isrc == x.comparison_isrc, x.duration_delta_ms, _versions_conflict(x.identity_version_cue, x.comparison_version_cue)) for x in comparisons))
            consistent=not facts.comparison_inconsistent; mixed=len({_band_name(x) for x in deltas})>1
            include={"comparison-inconsistent":not consistent,"multiple-comparisons":len(comparisons)>1,"multiple-comparisons-all-agree":len(comparisons)>1 and consistent,"multiple-comparisons-duration-band-mixed":len(comparisons)>1 and mixed}[category]
            if include: result.append(CandidateEvidenceDetail(key,comparisons[0].identity_id,comparisons[0].canonical_track_id,comparisons,consistent,min(deltas),max(deltas),mixed))
            if len(result)==limit: break
        return tuple(result)


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
        "same-isrc-duration-501-to-1000ms": same_isrc and 501 <= delta <= 1000,
        "same-isrc-duration-1001-to-2000ms": same_isrc and 1001 <= delta <= 2000,
        "same-isrc-duration-2001-to-5000ms": same_isrc and 2001 <= delta <= 5000,
        "same-isrc-duration-gt-5000ms": same_isrc and delta > 5000,
        "different-isrc-duration-le-500ms": not same_isrc and delta <= 500,
        "different-isrc-duration-501-to-1000ms": not same_isrc and 501 <= delta <= 1000,
        "different-isrc-duration-1001-to-2000ms": not same_isrc and 1001 <= delta <= 2000,
        "different-isrc-duration-2001-to-5000ms": not same_isrc and 2001 <= delta <= 5000,
        "different-isrc-duration-gt-5000ms": not same_isrc and delta > 5000,
    }[category]

_CANDIDATE_CATEGORIES={"comparison-inconsistent","multiple-comparisons","multiple-comparisons-all-agree","multiple-comparisons-duration-band-mixed"}
def _band_name(delta:int)->str: return "le-500ms" if delta<=500 else "501-to-1000ms" if delta<=1000 else "1001-to-2000ms" if delta<=2000 else "2001-to-5000ms" if delta<=5000 else "gt-5000ms"
