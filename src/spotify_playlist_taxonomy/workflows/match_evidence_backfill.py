"""Narrow, operator-confirmed read-only Spotify evidence backfill for match review."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from spotify_playlist_taxonomy.adapters.spotify.errors import SpotifyAdapterError, SpotifyErrorKind
from spotify_playlist_taxonomy.adapters.spotify.reader import SpotifyRecordingEvidence
from spotify_playlist_taxonomy.domain.models import IdentityProvider, PlatformTrackIdentity
from spotify_playlist_taxonomy.persistence.phase3 import AccountMismatchError, Phase3Store


class RecordingEvidenceReader(Protocol):
    def read_track_recording_evidence(self, provider_track_id: str) -> SpotifyRecordingEvidence: ...


@dataclass(frozen=True, slots=True)
class EvidenceBackfillPlan:
    target_identities: int
    already_complete: int
    needs_backfill: int
    already_had_duration: int
    already_had_isrc: int


@dataclass(frozen=True, slots=True)
class EvidenceBackfillReport:
    target_identities: int
    already_had_duration: int
    already_had_isrc: int
    requested: int
    updated_duration: int
    updated_isrc: int
    updated_both: int
    provider_missing_or_unavailable: int
    failed: int
    remaining_duration_missing: int
    remaining_isrc_missing: int


@dataclass(frozen=True, slots=True)
class _Target:
    identity_id: str
    provider_track_id: str
    duration_ms: int | None
    isrc: str | None


class MatchEvidenceBackfillService:
    """Backfill only local identities selected by current ambiguous link candidates."""

    def __init__(self, store: Phase3Store, binding_id: str) -> None:
        self._store = store
        self._binding_id = binding_id

    def plan(self) -> EvidenceBackfillPlan:
        targets = self._targets()
        return EvidenceBackfillPlan(
            target_identities=len(targets),
            already_complete=sum(item.duration_ms is not None and item.isrc is not None for item in targets),
            needs_backfill=sum(item.duration_ms is None for item in targets),
            already_had_duration=sum(item.duration_ms is not None for item in targets),
            already_had_isrc=sum(item.isrc is not None for item in targets),
        )

    def backfill(self, reader: RecordingEvidenceReader) -> EvidenceBackfillReport:
        targets = self._targets()
        plan = self.plan()
        requested = updated_duration = updated_isrc = updated_both = unavailable = failed = 0
        for item in targets:
            # Spotify's successful single-track response supplies duration.  A present
            # duration is the durable completion marker, even if optional ISRC is absent.
            if item.duration_ms is not None:
                continue
            requested += 1
            try:
                evidence = reader.read_track_recording_evidence(item.provider_track_id)
            except SpotifyAdapterError as error:
                if error.kind in {SpotifyErrorKind.NOT_AUTHORIZED, SpotifyErrorKind.FORBIDDEN,
                                  SpotifyErrorKind.RATE_LIMITED, SpotifyErrorKind.QUOTA_EXHAUSTED}:
                    raise
                if error.kind is SpotifyErrorKind.NOT_FOUND:
                    unavailable += 1
                else:
                    failed += 1
                continue
            try:
                isrc = PlatformTrackIdentity(
                    "spotify:evidence", IdentityProvider.SPOTIFY, "Synthetic", ("Synthetic",),
                    duration_ms=evidence.duration_ms, isrc=evidence.isrc,
                ).isrc
            except ValueError:
                failed += 1
                continue
            duration_changed, isrc_changed = self._store.update_identity_recording_evidence(
                item.identity_id, duration_ms=evidence.duration_ms, isrc=isrc
            )
            updated_duration += duration_changed
            updated_isrc += isrc_changed
            updated_both += duration_changed and isrc_changed
        remaining = self._targets()
        return EvidenceBackfillReport(
            target_identities=len(targets), already_had_duration=plan.already_had_duration,
            already_had_isrc=plan.already_had_isrc, requested=requested,
            updated_duration=updated_duration, updated_isrc=updated_isrc,
            updated_both=updated_both, provider_missing_or_unavailable=unavailable,
            failed=failed,
            remaining_duration_missing=sum(item.duration_ms is None for item in remaining),
            remaining_isrc_missing=sum(item.isrc is None for item in remaining),
        )

    def require_matching_account(self, account_id: str) -> None:
        binding = self._store.active_account()
        if binding is None or binding.binding_id != self._binding_id or binding.account_id != account_id:
            raise AccountMismatchError("authenticated Spotify account does not match the active local account binding")

    def _targets(self) -> tuple[_Target, ...]:
        rows = self._store.connection.execute(
            """
            WITH candidate_identities AS (
                SELECT c.identity_id
                FROM canonical_match_candidates AS c
                WHERE c.account_binding_id = ? AND c.action = 'link_existing'
                  AND c.state = 'ambiguous'
                UNION
                SELECT l.identity_id
                FROM canonical_match_candidates AS c
                JOIN account_canonical_identity_links AS l
                  ON l.account_binding_id = c.account_binding_id
                 AND l.canonical_track_id = c.canonical_track_id
                WHERE c.account_binding_id = ? AND c.action = 'link_existing'
                  AND c.state = 'ambiguous'
            )
            SELECT i.identity_id, i.provider_track_id, i.duration_ms, i.isrc
            FROM track_identities AS i
            JOIN candidate_identities AS selected ON selected.identity_id = i.identity_id
            WHERE i.provider = 'spotify'
            ORDER BY i.identity_id
            """,
            (self._binding_id, self._binding_id),
        ).fetchall()
        return tuple(_Target(row["identity_id"], row["provider_track_id"], row["duration_ms"], row["isrc"]) for row in rows)
