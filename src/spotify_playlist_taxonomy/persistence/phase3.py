"""SQLite state used by Phase 3 ingestion and matching workflows."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator

from spotify_playlist_taxonomy.domain.ingestion import (
    AccountBinding,
    CandidateReview,
    CatalogueStatus,
    IdentityReview,
    InboxKind,
    IngestionSource,
    LinkDecisionOrigin,
    LinkEventAction,
    LinkReview,
    MatchCandidate,
    MatchCandidateAction,
    MatchCandidateState,
    OwnershipState,
    ScanStatus,
    SourceKind,
    StagedSourceEntry,
)
from spotify_playlist_taxonomy.domain.models import (
    CanonicalTrack,
    IdentityProvider,
    PlatformTrackIdentity,
    PlaylistRole,
)
from spotify_playlist_taxonomy.domain.policies import normalize_identity_text
from spotify_playlist_taxonomy.ports.music_platform import PlatformPlaylistSummary

from .database import utc_now


class Phase3StateError(RuntimeError):
    pass


class AccountMismatchError(Phase3StateError):
    pass


_REVIEWABLE_STATES = {
    MatchCandidateState.CANDIDATE.value,
    MatchCandidateState.AMBIGUOUS.value,
    MatchCandidateState.USER_CONFIRMATION_REQUIRED.value,
}

_LEGAL_SCAN_TRANSITIONS = {
    ScanStatus.PENDING.value: {
        ScanStatus.RUNNING.value,
        ScanStatus.ABANDONED.value,
    },
    ScanStatus.RUNNING.value: {
        ScanStatus.FAILED.value,
        ScanStatus.STALE.value,
        ScanStatus.ABANDONED.value,
    },
    ScanStatus.FAILED.value: {
        ScanStatus.RUNNING.value,
        ScanStatus.STALE.value,
        ScanStatus.ABANDONED.value,
    },
    ScanStatus.COMPLETED.value: set(),
    ScanStatus.STALE.value: set(),
    ScanStatus.ABANDONED.value: set(),
}


class Phase3Store:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def active_account(self) -> AccountBinding | None:
        row = self.connection.execute(
            "SELECT * FROM platform_account_bindings WHERE active = 1 ORDER BY bound_at LIMIT 1"
        ).fetchone()
        return None if row is None else AccountBinding(
            row["binding_id"], row["account_id"], row["current_user_id"], True
        )

    def bind_account(self, binding_id: str, account_id: str, current_user_id: str) -> AccountBinding:
        active = self.active_account()
        now = utc_now()
        if active is not None:
            if active.account_id != account_id:
                raise AccountMismatchError(
                    "authenticated Spotify account does not match the active local account binding"
                )
            self.connection.execute(
                "UPDATE platform_account_bindings SET current_user_id = ?, last_verified_at = ? "
                "WHERE binding_id = ?",
                (current_user_id, now, active.binding_id),
            )
            self.connection.commit()
            return AccountBinding(active.binding_id, account_id, current_user_id, True)

        existing = self.connection.execute(
            "SELECT binding_id FROM platform_account_bindings WHERE account_id = ?",
            (account_id,),
        ).fetchone()
        with self.transaction() as connection:
            if existing is None:
                selected_id = binding_id
                connection.execute(
                    "INSERT INTO platform_account_bindings(" 
                    "binding_id, provider, account_id, current_user_id, active, bound_at, last_verified_at" 
                    ") VALUES (?, 'spotify', ?, ?, 1, ?, ?)",
                    (selected_id, account_id, current_user_id, now, now),
                )
            else:
                selected_id = existing["binding_id"]
                connection.execute(
                    "UPDATE platform_account_bindings SET current_user_id = ?, active = 1, "
                    "last_verified_at = ?, unbound_at = NULL WHERE binding_id = ?",
                    (current_user_id, now, selected_id),
                )
        return AccountBinding(selected_id, account_id, current_user_id, True)

    def unbind_account(self) -> None:
        binding = self.active_account()
        if binding is None:
            raise Phase3StateError("no active Spotify account binding exists")
        active_sources = self.connection.execute(
            "SELECT COUNT(*) FROM ingestion_sources WHERE account_binding_id = ? AND active = 1",
            (binding.binding_id,),
        ).fetchone()[0]
        if active_sources:
            raise Phase3StateError(
                "active ingestion sources must be deliberately disabled before account unbind"
            )
        self.connection.execute(
            "UPDATE platform_account_bindings SET active = 0, unbound_at = ? WHERE binding_id = ?",
            (utc_now(), binding.binding_id),
        )
        self.connection.commit()

    def register_playlist_source(
        self,
        *,
        source_id: str,
        registration_id: str,
        binding_id: str,
        summary: PlatformPlaylistSummary,
        role: PlaylistRole,
        ownership: OwnershipState,
        inbox_kind: InboxKind | None = None,
    ) -> IngestionSource:
        if role not in {PlaylistRole.SOURCE, PlaylistRole.INBOX}:
            raise Phase3StateError("Phase 3 permits only SOURCE and INBOX registration")
        if inbox_kind not in {None, InboxKind.SHAZAM}:
            raise Phase3StateError("playlist sources may use only the Shazam inbox kind")
        if inbox_kind is InboxKind.SHAZAM and role is not PlaylistRole.INBOX:
            raise Phase3StateError("the Shazam source must be an INBOX")
        self._require_active_binding(binding_id)
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                """
                INSERT INTO platform_playlist_registrations(
                    registration_id, provider, provider_playlist_id, role, logical_playlist_id,
                    owner_account_id, display_name, registered_at, account_binding_id, active
                ) VALUES (?, 'spotify', ?, ?, NULL, NULL, ?, ?, ?, 1)
                """,
                (registration_id, summary.playlist_id, role.value, summary.name, now, binding_id),
            )
            connection.execute(
                """
                INSERT INTO ingestion_sources(
                    source_id, account_binding_id, source_kind, playlist_registration_id,
                    role, inbox_kind, display_label, active, registered_at
                ) VALUES (?, ?, 'playlist', ?, ?, ?, ?, 1, ?)
                """,
                (
                    source_id, binding_id, registration_id, role.value,
                    None if inbox_kind is None else inbox_kind.value, summary.name, now,
                ),
            )
            self._insert_playlist_observation(
                connection, binding_id, registration_id, summary, ownership, "unknown", now
            )
        return IngestionSource(
            source_id, binding_id, SourceKind.PLAYLIST, role,
            summary.playlist_id, inbox_kind, True,
        )

    def register_liked_source(self, source_id: str, binding_id: str) -> IngestionSource:
        self._require_active_binding(binding_id)
        self.connection.execute(
            """
            INSERT INTO ingestion_sources(
                source_id, account_binding_id, source_kind, playlist_registration_id,
                role, inbox_kind, display_label, active, registered_at
            ) VALUES (?, ?, 'saved_tracks', NULL, 'inbox', 'liked_songs', 'Liked Songs', 1, ?)
            """,
            (source_id, binding_id, utc_now()),
        )
        self.connection.commit()
        return IngestionSource(
            source_id, binding_id, SourceKind.SAVED_TRACKS, PlaylistRole.INBOX,
            None, InboxKind.LIKED_SONGS, True,
        )

    def source(self, source_id: str) -> IngestionSource:
        row = self.connection.execute(
            """
            SELECT s.*, r.provider_playlist_id
            FROM ingestion_sources AS s
            LEFT JOIN platform_playlist_registrations AS r
              ON r.registration_id = s.playlist_registration_id
             AND r.account_binding_id = s.account_binding_id
            WHERE s.source_id = ?
            """,
            (source_id,),
        ).fetchone()
        if row is None:
            raise Phase3StateError("ingestion source was not found")
        return self._source(row)

    def source_for_binding(self, source_id: str, binding_id: str) -> IngestionSource:
        source = self.source(source_id)
        if source.binding_id != binding_id:
            raise Phase3StateError("source belongs to a different Spotify account binding")
        return source

    def list_sources(
        self, binding_id: str, *, active_only: bool = False
    ) -> tuple[IngestionSource, ...]:
        active_clause = "AND s.active = 1" if active_only else ""
        rows = self.connection.execute(
            f"""
            SELECT s.*, r.provider_playlist_id
            FROM ingestion_sources AS s
            LEFT JOIN platform_playlist_registrations AS r
              ON r.registration_id = s.playlist_registration_id
             AND r.account_binding_id = s.account_binding_id
            WHERE s.account_binding_id = ? {active_clause}
            ORDER BY s.registered_at, s.source_id
            """,
            (binding_id,),
        )
        return tuple(self._source(row) for row in rows)

    def disable_source(self, source_id: str, binding_id: str) -> None:
        source = self.source_for_binding(source_id, binding_id)
        if not source.active:
            raise Phase3StateError("ingestion source is already disabled")
        now = utc_now()
        with self.transaction() as connection:
            connection.execute(
                "UPDATE ingestion_sources SET active = 0, disabled_at = ? "
                "WHERE source_id = ? AND account_binding_id = ?",
                (now, source_id, binding_id),
            )
            if source.kind is SourceKind.PLAYLIST:
                connection.execute(
                    "UPDATE platform_playlist_registrations SET active = 0, disabled_at = ? "
                    "WHERE registration_id = (SELECT playlist_registration_id FROM ingestion_sources "
                    "WHERE source_id = ?) AND account_binding_id = ?",
                    (now, source_id, binding_id),
                )

    def observe_playlist(
        self,
        source_id: str,
        binding_id: str,
        summary: PlatformPlaylistSummary,
        ownership: OwnershipState,
        accessibility: str,
    ) -> None:
        row = self.connection.execute(
            """
            SELECT s.playlist_registration_id, r.provider_playlist_id
            FROM ingestion_sources AS s
            JOIN platform_playlist_registrations AS r
              ON r.registration_id = s.playlist_registration_id
             AND r.account_binding_id = s.account_binding_id
            WHERE s.source_id = ? AND s.account_binding_id = ?
            """,
            (source_id, binding_id),
        ).fetchone()
        if row is None or row["provider_playlist_id"] != summary.playlist_id:
            raise Phase3StateError("account-scoped playlist source registration was not found")
        self._insert_playlist_observation(
            self.connection, binding_id, row["playlist_registration_id"], summary,
            ownership, accessibility, utc_now(),
        )
        self.connection.commit()

    def record_playlist_accessibility(
        self, source_id: str, binding_id: str, accessibility: str
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT o.*
                FROM ingestion_sources AS s
                JOIN platform_playlist_observations AS o
                  ON o.registration_id = s.playlist_registration_id
                 AND o.account_binding_id = s.account_binding_id
                WHERE s.source_id = ? AND s.account_binding_id = ?
                ORDER BY o.observation_id DESC LIMIT 1
                """,
                (source_id, binding_id),
            ).fetchone()
            if row is None:
                raise Phase3StateError("playlist observation is unavailable")
            connection.execute(
                """
                INSERT INTO platform_playlist_observations(
                    account_binding_id, registration_id, display_name, owner_user_id,
                    ownership_state, collaborative, public, item_count, snapshot_id,
                    accessibility_state, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    binding_id, row["registration_id"], row["display_name"],
                    row["owner_user_id"], row["ownership_state"], row["collaborative"],
                    row["public"], row["item_count"], row["snapshot_id"], accessibility, now,
                ),
            )

    def start_scan(
        self,
        *,
        scan_id: str,
        source_id: str,
        binding_id: str,
        starting_snapshot_id: str | None,
        starting_total: int | None,
        initial_page_signature: str | None,
        digest_version: str,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            self._require_active_source(connection, source_id, binding_id)
            connection.execute(
                "UPDATE source_scans SET status = 'abandoned', failure_kind = 'superseded', "
                "updated_at = ?, completed_at = COALESCE(completed_at, ?) "
                "WHERE source_id = ? AND status IN ('pending', 'running', 'failed')",
                (now, now, source_id),
            )
            connection.execute(
                """
                INSERT INTO source_scans(
                    scan_id, source_id, account_binding_id, status,
                    starting_snapshot_id, starting_total,
                    initial_page_signature, digest_version, started_at, updated_at
                ) VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id, source_id, binding_id, starting_snapshot_id, starting_total,
                    initial_page_signature, digest_version, now, now,
                ),
            )

    def resumable_playlist_scan(self, source_id: str, binding_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT scan.* FROM source_scans AS scan
            JOIN ingestion_sources AS source ON source.source_id = scan.source_id
            WHERE scan.source_id = ? AND source.account_binding_id = ?
              AND source.active = 1 AND scan.status IN ('running', 'failed')
            ORDER BY scan.started_at DESC LIMIT 1
            """,
            (source_id, binding_id),
        ).fetchone()

    def stage_page(
        self,
        *,
        scan_id: str,
        binding_id: str,
        entries: tuple[StagedSourceEntry, ...],
        next_cursor: str | None,
        next_offset: int,
    ) -> None:
        with self.transaction() as connection:
            scan = connection.execute(
                """
                SELECT scan.*, source.account_binding_id, source.active AS source_active,
                       account.active AS account_active, publication.scan_id AS published_scan_id
                FROM source_scans AS scan
                JOIN ingestion_sources AS source ON source.source_id = scan.source_id
                JOIN platform_account_bindings AS account
                  ON account.binding_id = source.account_binding_id
                LEFT JOIN source_publications AS publication ON publication.scan_id = scan.scan_id
                WHERE scan.scan_id = ? AND source.account_binding_id = ?
                """,
                (scan_id, binding_id),
            ).fetchone()
            if scan is None:
                raise Phase3StateError("account-scoped scan was not found")
            if (
                scan["status"] != ScanStatus.RUNNING.value
                or scan["published_scan_id"] is not None
                or not scan["source_active"]
                or not scan["account_active"]
            ):
                raise Phase3StateError("pages may be staged only into an unpublished running scan")
            if scan["enumeration_complete"]:
                raise Phase3StateError("the scan has already reached its terminal page")
            if next_offset < scan["next_offset"]:
                raise Phase3StateError("scan offsets cannot move backwards")

            for entry in entries:
                identity_id = None
                if entry.identity is not None:
                    identity_id = self._upsert_identity(
                        connection, binding_id, scan_id, scan["source_id"], entry.identity
                    )
                connection.execute(
                    """
                    INSERT INTO source_scan_entries(
                        scan_id, source_id, source_position, page_ordinal, identity_id, issue_kind
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        scan_id, scan["source_id"], entry.source_position,
                        entry.page_ordinal, identity_id, entry.issue_kind,
                    ),
                )
            counts = connection.execute(
                """
                SELECT COUNT(*) AS occurrences,
                       SUM(CASE WHEN issue_kind IS NOT NULL THEN 1 ELSE 0 END) AS issues
                FROM source_scan_entries WHERE scan_id = ?
                """,
                (scan_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE source_scans
                SET resume_cursor = ?, next_offset = ?, page_count = page_count + 1,
                    occurrence_count = ?, issue_count = ?, enumeration_complete = ?,
                    failure_kind = NULL, updated_at = ?
                WHERE scan_id = ? AND status = 'running'
                """,
                (
                    next_cursor, next_offset, counts["occurrences"], counts["issues"] or 0,
                    int(next_cursor is None), utc_now(), scan_id,
                ),
            )

    def mark_scan(
        self, scan_id: str, binding_id: str, status: ScanStatus, failure_kind: str | None = None
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                """
                SELECT scan.status, publication.scan_id AS published_scan_id
                FROM source_scans AS scan
                JOIN ingestion_sources AS source ON source.source_id = scan.source_id
                LEFT JOIN source_publications AS publication ON publication.scan_id = scan.scan_id
                WHERE scan.scan_id = ? AND source.account_binding_id = ?
                """,
                (scan_id, binding_id),
            ).fetchone()
            if row is None:
                raise Phase3StateError("account-scoped scan was not found")
            if row["published_scan_id"] is not None:
                raise Phase3StateError("published scans are immutable")
            if status.value not in _LEGAL_SCAN_TRANSITIONS[row["status"]]:
                raise Phase3StateError(
                    f"illegal scan transition: {row['status']} -> {status.value}"
                )
            completed_at = utc_now() if status in {ScanStatus.STALE, ScanStatus.ABANDONED} else None
            connection.execute(
                """
                UPDATE source_scans
                SET status = ?, failure_kind = ?, updated_at = ?,
                    completed_at = CASE WHEN ? IS NULL THEN NULL ELSE ? END
                WHERE scan_id = ?
                """,
                (
                    status.value,
                    None if status is ScanStatus.RUNNING else failure_kind,
                    utc_now(), completed_at, completed_at, scan_id,
                ),
            )

    def publish_scan(self, scan_id: str, binding_id: str, ending_snapshot_id: str | None) -> str:
        now = utc_now()
        with self.transaction() as connection:
            scan = connection.execute(
                """
                SELECT scan.*, source.source_kind, source.account_binding_id,
                       source.active AS source_active, account.active AS account_active,
                       publication.scan_id AS published_scan_id
                FROM source_scans AS scan
                JOIN ingestion_sources AS source ON source.source_id = scan.source_id
                JOIN platform_account_bindings AS account
                  ON account.binding_id = source.account_binding_id
                LEFT JOIN source_publications AS publication ON publication.scan_id = scan.scan_id
                WHERE scan.scan_id = ? AND source.account_binding_id = ?
                """,
                (scan_id, binding_id),
            ).fetchone()
            if scan is None:
                raise Phase3StateError("account-scoped scan was not found")
            if (
                scan["status"] != ScanStatus.RUNNING.value
                or scan["published_scan_id"] is not None
                or not scan["source_active"]
                or not scan["account_active"]
            ):
                raise Phase3StateError("only an active unpublished running scan can be published")
            if not scan["enumeration_complete"] or scan["resume_cursor"] is not None:
                raise Phase3StateError("scan enumeration must reach its terminal page before publication")
            if (
                scan["source_kind"] == SourceKind.PLAYLIST.value
                and scan["starting_snapshot_id"] != ending_snapshot_id
            ):
                raise Phase3StateError("playlist snapshot changed before publication")
            digest = self._scan_digest(connection, scan_id, scan["digest_version"])
            connection.execute(
                """
                UPDATE source_scans
                SET status = 'completed', ending_snapshot_id = ?, content_digest = ?,
                    resume_cursor = NULL, failure_kind = NULL, updated_at = ?, completed_at = ?
                WHERE scan_id = ? AND status = 'running'
                """,
                (ending_snapshot_id, digest, now, now, scan_id),
            )
            connection.execute(
                """
                INSERT INTO source_publications(
                    source_id, account_binding_id, scan_id, published_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(source_id) DO UPDATE SET scan_id = excluded.scan_id,
                    published_at = excluded.published_at
                """,
                (scan["source_id"], binding_id, scan_id, now),
            )
            connection.execute(
                """
                UPDATE account_track_identity_state
                SET first_published_scan_id = COALESCE(first_published_scan_id, ?),
                    first_published_source_id = COALESCE(first_published_source_id, ?)
                WHERE account_binding_id = ? AND identity_id IN (
                    SELECT identity_id FROM source_scan_entries
                    WHERE scan_id = ? AND identity_id IS NOT NULL
                )
                """,
                (scan_id, scan["source_id"], binding_id, scan_id),
            )
        return digest

    def scan_row(self, scan_id: str, binding_id: str | None = None) -> sqlite3.Row:
        if binding_id is None:
            row = self.connection.execute(
                "SELECT * FROM source_scans WHERE scan_id = ?", (scan_id,)
            ).fetchone()
        else:
            row = self.connection.execute(
                """
                SELECT scan.* FROM source_scans AS scan
                JOIN ingestion_sources AS source ON source.source_id = scan.source_id
                WHERE scan.scan_id = ? AND source.account_binding_id = ?
                """,
                (scan_id, binding_id),
            ).fetchone()
        if row is None:
            raise Phase3StateError("scan was not found")
        return row

    def scan_rows(
        self, binding_id: str, source_id: str | None = None
    ) -> tuple[sqlite3.Row, ...]:
        source_clause = "AND scan.source_id = ?" if source_id is not None else ""
        params: tuple[object, ...] = (
            (binding_id,) if source_id is None else (binding_id, source_id)
        )
        return tuple(self.connection.execute(
            f"""
            SELECT scan.* FROM source_scans AS scan
            JOIN ingestion_sources AS source ON source.source_id = scan.source_id
            WHERE source.account_binding_id = ? {source_clause}
            ORDER BY scan.started_at, scan.scan_id
            """,
            params,
        ))

    def current_publication(self, source_id: str, binding_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            """
            SELECT scan.* FROM source_publications AS publication
            JOIN source_scans AS scan
              ON scan.scan_id = publication.scan_id
             AND scan.source_id = publication.source_id
            JOIN ingestion_sources AS source ON source.source_id = publication.source_id
            WHERE publication.source_id = ? AND source.account_binding_id = ?
              AND publication.account_binding_id = source.account_binding_id
              AND source.active = 1 AND scan.status = 'completed'
              AND scan.enumeration_complete = 1 AND scan.content_digest IS NOT NULL
            """,
            (source_id, binding_id),
        ).fetchone()

    def published_identity_ids(
        self, binding_id: str, scan_id: str | None = None
    ) -> tuple[str, ...]:
        condition = "AND publication.scan_id = ?" if scan_id is not None else ""
        params: tuple[object, ...] = (
            (binding_id,) if scan_id is None else (binding_id, scan_id)
        )
        rows = self.connection.execute(
            f"""
            SELECT DISTINCT entry.identity_id
            FROM source_publications AS publication
            JOIN source_scans AS scan
              ON scan.scan_id = publication.scan_id
             AND scan.source_id = publication.source_id
            JOIN ingestion_sources AS source ON source.source_id = publication.source_id
            JOIN platform_account_bindings AS account
              ON account.binding_id = source.account_binding_id
            JOIN source_scan_entries AS entry
              ON entry.scan_id = publication.scan_id
             AND entry.source_id = publication.source_id
            WHERE source.account_binding_id = ?
              AND publication.account_binding_id = source.account_binding_id
              AND source.active = 1 AND account.active = 1
              AND scan.status = 'completed' AND scan.enumeration_complete = 1
              AND scan.content_digest IS NOT NULL
              AND entry.identity_id IS NOT NULL {condition}
            ORDER BY entry.identity_id
            """,
            params,
        )
        return tuple(row[0] for row in rows)

    def reconcile_published_identity(
        self,
        binding_id: str,
        identity_id: str,
        *,
        matcher_version: str,
        candidate_id: str,
        canonical_id: str,
        link_id: str,
        event_id: str,
    ) -> str:
        if identity_id not in set(self.published_identity_ids(binding_id)):
            raise Phase3StateError("matching requires a currently published source occurrence")
        active = self.connection.execute(
            "SELECT canonical_track_id FROM account_canonical_identity_links "
            "WHERE account_binding_id = ? AND identity_id = ?",
            (binding_id, identity_id),
        ).fetchone()
        if active is not None:
            return active["canonical_track_id"]
        state = self.connection.execute(
            "SELECT * FROM account_track_identity_state "
            "WHERE account_binding_id = ? AND identity_id = ?",
            (binding_id, identity_id),
        ).fetchone()
        if state is None:
            raise Phase3StateError("account identity state is missing")
        identity = self._identity(identity_id)
        evidence, foreign_mapping = self._candidate_canonicals(binding_id, identity)
        user_touched = bool(state["matching_touched"])
        actionable = 0
        if evidence:
            for index, item in enumerate(evidence):
                requires_confirmation = (
                    bool(state["preexisting"])
                    or user_touched
                    or foreign_mapping
                    or _versions_conflict(
                        identity.version_label, _version_from_title(item.display_title)
                    )
                )
                basis = (
                    "other_account_mapping_requires_confirmation"
                    if foreign_mapping else
                    "prior_user_matching_intervention"
                    if user_touched else
                    "normalized_title_artist_match_only"
                )
                candidate = self._insert_candidate(
                    f"{candidate_id}.link.{index}", binding_id, identity,
                    item.canonical_track_id, MatchCandidateAction.LINK_EXISTING,
                    MatchCandidateState.USER_CONFIRMATION_REQUIRED
                    if requires_confirmation else MatchCandidateState.AMBIGUOUS,
                    0.25 if requires_confirmation else 0.5,
                    (basis,), matcher_version,
                )
                actionable += int(candidate.state.value in _REVIEWABLE_STATES)
            create_candidate = self._insert_candidate(
                f"{candidate_id}.create", binding_id, identity, None,
                MatchCandidateAction.CREATE_NEW,
                MatchCandidateState.USER_CONFIRMATION_REQUIRED, 0.0,
                ("preserve_distinct_recording_option",), matcher_version,
            )
            actionable += int(create_candidate.state.value in _REVIEWABLE_STATES)
            return "review_required" if actionable else "user_resolved"

        if bool(state["preexisting"]) or user_touched:
            candidate = self._insert_candidate(
                f"{candidate_id}.create", binding_id, identity, None,
                MatchCandidateAction.CREATE_NEW,
                MatchCandidateState.USER_CONFIRMATION_REQUIRED, 0.0,
                ("existing_or_user_touched_identity",), matcher_version,
            )
            return (
                "review_required"
                if candidate.state.value in _REVIEWABLE_STATES else "user_resolved"
            )

        with self.transaction() as connection:
            self._require_active_binding(binding_id, connection)
            connection.execute(
                "INSERT INTO canonical_tracks(canonical_track_id, display_title, created_at) "
                "VALUES (?, ?, ?)",
                (canonical_id, identity.title, utc_now()),
            )
            connection.executemany(
                "INSERT INTO canonical_track_artists(canonical_track_id, position, artist_name) "
                "VALUES (?, ?, ?)",
                [(canonical_id, index, artist) for index, artist in enumerate(identity.artists)],
            )
            connection.execute(
                "INSERT INTO account_canonical_identity_links(" 
                "link_id, account_binding_id, canonical_track_id, identity_id, linked_at" 
                ") VALUES (?, ?, ?, ?, ?)",
                (link_id, binding_id, canonical_id, identity_id, utc_now()),
            )
            connection.execute(
                """
                INSERT INTO canonical_identity_link_events(
                    event_id, account_binding_id, identity_id, canonical_track_id,
                    link_id, action, origin, event_at
                ) VALUES (?, ?, ?, ?, ?, 'create_new', 'system_bootstrap', ?)
                """,
                (event_id, binding_id, identity_id, canonical_id, link_id, utc_now()),
            )
        return canonical_id

    def candidates(
        self,
        binding_id: str,
        state: MatchCandidateState | None = None,
        limit: int | None = None,
    ) -> tuple[MatchCandidate, ...]:
        if limit is not None and not 1 <= limit <= 100:
            raise ValueError("candidate limit must be between 1 and 100")
        state_clause = "AND state = ?" if state is not None else ""
        limit_clause = "" if limit is None else "LIMIT ?"
        params: tuple[object, ...] = (
            (binding_id,) if state is None else (binding_id, state.value)
        )
        if limit is not None:
            params += (limit,)
        rows = self.connection.execute(
            f"SELECT * FROM canonical_match_candidates WHERE account_binding_id = ? "
            f"{state_clause} ORDER BY created_at, candidate_id {limit_clause}",
            params,
        )
        return tuple(self._candidate(row) for row in rows)

    def confirm_candidate(
        self,
        binding_id: str,
        candidate_id: str,
        *,
        canonical_id: str,
        create_new: bool,
        link_id: str,
        event_id: str,
    ) -> None:
        row = self._candidate_row(binding_id, candidate_id)
        if row["state"] not in _REVIEWABLE_STATES:
            raise Phase3StateError("candidate is not reviewable")
        expected_action = (
            MatchCandidateAction.CREATE_NEW.value
            if create_new else MatchCandidateAction.LINK_EXISTING.value
        )
        if row["action"] != expected_action:
            raise Phase3StateError("candidate action does not match the requested confirmation")
        if not create_new and row["canonical_track_id"] != canonical_id:
            raise Phase3StateError("confirmation must use the candidate canonical track")
        identity = self._identity(row["identity_id"])
        origin = (
            LinkDecisionOrigin.USER_CREATE_NEW
            if create_new else LinkDecisionOrigin.USER_CONFIRMATION
        )
        action = LinkEventAction.CREATE_NEW if create_new else LinkEventAction.LINK
        with self.transaction() as connection:
            self._require_active_binding(binding_id, connection)
            if connection.execute(
                "SELECT 1 FROM account_canonical_identity_links "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (binding_id, row["identity_id"]),
            ).fetchone() is not None:
                raise Phase3StateError("identity already has an active account canonical link")
            if create_new:
                connection.execute(
                    "INSERT INTO canonical_tracks(canonical_track_id, display_title, created_at) "
                    "VALUES (?, ?, ?)",
                    (canonical_id, identity.title, utc_now()),
                )
                connection.executemany(
                    "INSERT INTO canonical_track_artists(" 
                    "canonical_track_id, position, artist_name) VALUES (?, ?, ?)",
                    [(canonical_id, index, artist) for index, artist in enumerate(identity.artists)],
                )
            elif connection.execute(
                "SELECT 1 FROM canonical_tracks WHERE canonical_track_id = ?", (canonical_id,)
            ).fetchone() is None:
                raise Phase3StateError("canonical track was not found")
            connection.execute(
                "INSERT INTO account_canonical_identity_links(" 
                "link_id, account_binding_id, canonical_track_id, identity_id, linked_at" 
                ") VALUES (?, ?, ?, ?, ?)",
                (link_id, binding_id, canonical_id, row["identity_id"], utc_now()),
            )
            connection.execute(
                """
                INSERT INTO canonical_identity_link_events(
                    event_id, account_binding_id, identity_id, canonical_track_id,
                    link_id, action, origin, candidate_id, event_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id, binding_id, row["identity_id"], canonical_id, link_id,
                    action.value, origin.value, candidate_id, utc_now(),
                ),
            )
            connection.execute(
                "UPDATE canonical_match_candidates SET state = 'confirmed', decision_origin = ?, "
                "decided_at = ? WHERE candidate_id = ? AND account_binding_id = ?",
                (origin.value, utc_now(), candidate_id, binding_id),
            )
            connection.execute(
                "UPDATE canonical_match_candidates SET state = 'superseded', decided_at = ? "
                "WHERE account_binding_id = ? AND identity_id = ? AND candidate_id <> ? "
                "AND state IN ('candidate', 'ambiguous', 'user_confirmation_required')",
                (utc_now(), binding_id, row["identity_id"], candidate_id),
            )
            connection.execute(
                "UPDATE account_track_identity_state SET matching_touched = 1 "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (binding_id, row["identity_id"]),
            )

    def reject_candidate(self, binding_id: str, candidate_id: str) -> None:
        row = self._candidate_row(binding_id, candidate_id)
        if row["state"] not in _REVIEWABLE_STATES:
            raise Phase3StateError("candidate is not reviewable")
        with self.transaction() as connection:
            connection.execute(
                "UPDATE canonical_match_candidates SET state = 'rejected', "
                "decision_origin = 'user_rejection', decided_at = ? "
                "WHERE candidate_id = ? AND account_binding_id = ?",
                (utc_now(), candidate_id, binding_id),
            )
            connection.execute(
                "UPDATE account_track_identity_state SET matching_touched = 1 "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (binding_id, row["identity_id"]),
            )

    def unlink_identity(self, binding_id: str, identity_id: str, event_id: str) -> None:
        row = self.connection.execute(
            "SELECT * FROM account_canonical_identity_links "
            "WHERE account_binding_id = ? AND identity_id = ?",
            (binding_id, identity_id),
        ).fetchone()
        if row is None:
            raise Phase3StateError("identity has no active account canonical link")
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM account_canonical_identity_links "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (binding_id, identity_id),
            )
            connection.execute(
                """
                INSERT INTO canonical_identity_link_events(
                    event_id, account_binding_id, identity_id, canonical_track_id,
                    link_id, action, origin, event_at
                ) VALUES (?, ?, ?, ?, ?, 'unlink', 'user_unlink', ?)
                """,
                (
                    event_id, binding_id, identity_id, row["canonical_track_id"],
                    row["link_id"], utc_now(),
                ),
            )
            connection.execute(
                "UPDATE account_track_identity_state SET matching_touched = 1 "
                "WHERE account_binding_id = ? AND identity_id = ?",
                (binding_id, identity_id),
            )

    def catalogue_status(self, binding_id: str) -> CatalogueStatus:
        self._require_active_binding(binding_id)
        published = len(self.published_identity_ids(binding_id))
        linked = self.connection.execute(
            "SELECT COUNT(*) FROM account_canonical_identity_links WHERE account_binding_id = ?",
            (binding_id,),
        ).fetchone()[0]
        reviewable = self.connection.execute(
            "SELECT COUNT(*) FROM canonical_match_candidates WHERE account_binding_id = ? "
            "AND state IN ('candidate', 'ambiguous', 'user_confirmation_required')",
            (binding_id,),
        ).fetchone()[0]
        return CatalogueStatus(binding_id, published, linked, reviewable)

    def identity_review(self, binding_id: str, identity_id: str) -> IdentityReview:
        if self.connection.execute(
            "SELECT 1 FROM account_track_identity_state "
            "WHERE account_binding_id = ? AND identity_id = ?",
            (binding_id, identity_id),
        ).fetchone() is None:
            raise Phase3StateError("identity is not associated with the active account")
        link = self.connection.execute(
            "SELECT canonical_track_id FROM account_canonical_identity_links "
            "WHERE account_binding_id = ? AND identity_id = ?",
            (binding_id, identity_id),
        ).fetchone()
        canonical = None if link is None else self._canonical(link["canonical_track_id"])
        candidates = tuple(
            self._candidate(row) for row in self.connection.execute(
                "SELECT * FROM canonical_match_candidates "
                "WHERE account_binding_id = ? AND identity_id = ? "
                "ORDER BY created_at, candidate_id",
                (binding_id, identity_id),
            )
        )
        return IdentityReview(self._identity(identity_id), canonical, candidates)

    def candidate_review(self, binding_id: str, candidate_id: str) -> CandidateReview:
        row = self._candidate_row(binding_id, candidate_id)
        candidate = self._candidate(row)
        canonical = (
            None if row["canonical_track_id"] is None
            else self._canonical(row["canonical_track_id"])
        )
        return CandidateReview(candidate, self._identity(row["identity_id"]), canonical)

    def links(self, binding_id: str) -> tuple[LinkReview, ...]:
        rows = self.connection.execute(
            "SELECT * FROM account_canonical_identity_links "
            "WHERE account_binding_id = ? ORDER BY linked_at, link_id",
            (binding_id,),
        )
        return tuple(
            LinkReview(
                row["link_id"], self._identity(row["identity_id"]),
                self._canonical(row["canonical_track_id"]),
            )
            for row in rows
        )

    def linked_identity_ids_for_canonical(
        self, binding_id: str, canonical_track_id: str
    ) -> tuple[str, ...]:
        self._require_active_binding(binding_id)
        rows = self.connection.execute(
            "SELECT identity_id FROM account_canonical_identity_links "
            "WHERE account_binding_id = ? AND canonical_track_id = ? "
            "ORDER BY identity_id",
            (binding_id, canonical_track_id),
        )
        return tuple(str(row[0]) for row in rows)

    def _require_active_binding(
        self, binding_id: str, connection: sqlite3.Connection | None = None
    ) -> None:
        selected = self.connection if connection is None else connection
        row = selected.execute(
            "SELECT 1 FROM platform_account_bindings WHERE binding_id = ? AND active = 1",
            (binding_id,),
        ).fetchone()
        if row is None:
            raise Phase3StateError("active account binding was not found")

    @staticmethod
    def _require_active_source(
        connection: sqlite3.Connection, source_id: str, binding_id: str
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM ingestion_sources AS source
            JOIN platform_account_bindings AS account
              ON account.binding_id = source.account_binding_id
            WHERE source.source_id = ? AND source.account_binding_id = ?
              AND source.active = 1 AND account.active = 1
            """,
            (source_id, binding_id),
        ).fetchone()
        if row is None:
            raise Phase3StateError("active account-scoped ingestion source was not found")

    @staticmethod
    def _source(row: sqlite3.Row) -> IngestionSource:
        return IngestionSource(
            row["source_id"], row["account_binding_id"], SourceKind(row["source_kind"]),
            PlaylistRole(row["role"]), row["provider_playlist_id"],
            None if row["inbox_kind"] is None else InboxKind(row["inbox_kind"]),
            bool(row["active"]),
        )

    @staticmethod
    def _insert_playlist_observation(
        connection: sqlite3.Connection,
        binding_id: str,
        registration_id: str,
        summary: PlatformPlaylistSummary,
        ownership: OwnershipState,
        accessibility: str,
        observed_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO platform_playlist_observations(
                account_binding_id, registration_id, display_name, owner_user_id,
                ownership_state, collaborative, public, item_count, snapshot_id,
                accessibility_state, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id, registration_id, summary.name, summary.owner_user_id,
                ownership.value, int(summary.collaborative),
                None if summary.public is None else int(summary.public),
                summary.item_count, summary.snapshot_id, accessibility, observed_at,
            ),
        )

    def _upsert_identity(
        self,
        connection: sqlite3.Connection,
        binding_id: str,
        scan_id: str,
        source_id: str,
        identity: PlatformTrackIdentity,
    ) -> str:
        row = connection.execute(
            "SELECT identity_id FROM track_identities WHERE provider = ? AND provider_track_id = ?",
            (identity.provider.value, identity.provider_track_id),
        ).fetchone()
        globally_preexisting = row is not None
        selected_id = identity.identity_id if row is None else row["identity_id"]
        if row is None:
            connection.execute(
                """
                INSERT INTO track_identities(
                    identity_id, provider, provider_track_id, title, normalized_title,
                    version_label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.identity_id, identity.provider.value, identity.provider_track_id,
                    identity.title, normalize_identity_text(identity.title),
                    identity.version_label, utc_now(),
                ),
            )
        else:
            connection.execute(
                "UPDATE track_identities SET title = ?, normalized_title = ?, version_label = ? "
                "WHERE identity_id = ?",
                (
                    identity.title, normalize_identity_text(identity.title),
                    identity.version_label, selected_id,
                ),
            )
            connection.execute(
                "DELETE FROM track_identity_artists WHERE identity_id = ?", (selected_id,)
            )
        connection.executemany(
            """
            INSERT INTO track_identity_artists(
                identity_id, position, artist_name, normalized_artist_name
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (selected_id, index, artist, normalize_identity_text(artist))
                for index, artist in enumerate(identity.artists)
            ],
        )
        connection.execute(
            """
            INSERT INTO account_track_identity_state(
                account_binding_id, identity_id, first_seen_scan_id,
                first_seen_source_id, preexisting, last_observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(account_binding_id, identity_id)
            DO UPDATE SET last_observed_at = excluded.last_observed_at
            """,
            (
                binding_id, selected_id, scan_id, source_id,
                int(globally_preexisting), utc_now(),
            ),
        )
        return selected_id

    @staticmethod
    def _scan_digest(
        connection: sqlite3.Connection, scan_id: str, digest_version: str
    ) -> str:
        digest = hashlib.sha256()
        digest.update((digest_version + "\n").encode("utf-8"))
        for row in connection.execute(
            "SELECT source_position, identity_id, issue_kind FROM source_scan_entries "
            "WHERE scan_id = ? ORDER BY source_position",
            (scan_id,),
        ):
            value = row["identity_id"] if row["identity_id"] is not None else f"!{row['issue_kind']}"
            digest.update(f"{row['source_position']}:{value}\n".encode("utf-8"))
        return digest.hexdigest()

    def _identity(self, identity_id: str) -> PlatformTrackIdentity:
        row = self.connection.execute(
            "SELECT * FROM track_identities WHERE identity_id = ?", (identity_id,)
        ).fetchone()
        if row is None:
            raise Phase3StateError("track identity was not found")
        artists = tuple(
            item[0] for item in self.connection.execute(
                "SELECT artist_name FROM track_identity_artists "
                "WHERE identity_id = ? ORDER BY position",
                (identity_id,),
            )
        )
        return PlatformTrackIdentity(
            row["identity_id"], IdentityProvider(row["provider"]), row["title"], artists,
            row["provider_track_id"], row["version_label"],
        )

    def _canonical(self, canonical_id: str) -> CanonicalTrack:
        row = self.connection.execute(
            "SELECT * FROM canonical_tracks WHERE canonical_track_id = ?", (canonical_id,)
        ).fetchone()
        if row is None:
            raise Phase3StateError("canonical track was not found")
        artists = tuple(
            item[0] for item in self.connection.execute(
                "SELECT artist_name FROM canonical_track_artists "
                "WHERE canonical_track_id = ? ORDER BY position",
                (canonical_id,),
            )
        )
        return CanonicalTrack(canonical_id, row["display_title"], artists)

    def _candidate_canonicals(
        self, binding_id: str, identity: PlatformTrackIdentity
    ) -> tuple[tuple[CanonicalTrack, ...], bool]:
        direct_ids = {
            row[0] for row in self.connection.execute(
                "SELECT canonical_track_id FROM account_canonical_identity_links "
                "WHERE identity_id = ? AND account_binding_id <> ?",
                (identity.identity_id, binding_id),
            )
        }
        direct_ids.update(
            row[0] for row in self.connection.execute(
                "SELECT canonical_track_id FROM canonical_track_identity_links WHERE identity_id = ?",
                (identity.identity_id,),
            )
        )
        matches = {item.canonical_track_id: item for item in self._matching_canonicals(identity)}
        for canonical_id in direct_ids:
            matches[canonical_id] = self._canonical(canonical_id)
        return tuple(matches[key] for key in sorted(matches)), bool(direct_ids)

    def _matching_canonicals(self, identity: PlatformTrackIdentity) -> tuple[CanonicalTrack, ...]:
        target = (
            normalize_identity_text(identity.title),
            tuple(sorted(normalize_identity_text(item) for item in identity.artists)),
        )
        matches: list[CanonicalTrack] = []
        for row in self.connection.execute("SELECT * FROM canonical_tracks ORDER BY canonical_track_id"):
            artists = tuple(
                item[0] for item in self.connection.execute(
                    "SELECT artist_name FROM canonical_track_artists "
                    "WHERE canonical_track_id = ? ORDER BY position",
                    (row["canonical_track_id"],),
                )
            )
            key = (
                normalize_identity_text(row["display_title"]),
                tuple(sorted(normalize_identity_text(item) for item in artists)),
            )
            if key == target:
                matches.append(CanonicalTrack(row["canonical_track_id"], row["display_title"], artists))
        return tuple(matches)

    def _insert_candidate(
        self,
        candidate_id: str,
        binding_id: str,
        identity: PlatformTrackIdentity,
        canonical_id: str | None,
        action: MatchCandidateAction,
        state: MatchCandidateState,
        confidence: float,
        basis: tuple[str, ...],
        matcher_version: str,
    ) -> MatchCandidate:
        fingerprint = hashlib.sha256(json.dumps({
            "identity": identity.identity_id,
            "title": identity.title,
            "artists": identity.artists,
            "version": identity.version_label,
            "canonical": canonical_id,
        }, sort_keys=True).encode("utf-8")).hexdigest()
        candidate_key = hashlib.sha256(
            f"{action.value}:{canonical_id or '<new>'}:{fingerprint}".encode("utf-8")
        ).hexdigest()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO canonical_match_candidates(
                candidate_id, account_binding_id, identity_id, canonical_track_id,
                action, state, confidence, basis_json, matcher_version,
                input_fingerprint, candidate_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate_id, binding_id, identity.identity_id, canonical_id, action.value,
                state.value, confidence, json.dumps(basis), matcher_version,
                fingerprint, candidate_key, utc_now(),
            ),
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM canonical_match_candidates WHERE account_binding_id = ? "
            "AND identity_id = ? AND matcher_version = ? AND candidate_key = ?",
            (binding_id, identity.identity_id, matcher_version, candidate_key),
        ).fetchone()
        assert row is not None
        return self._candidate(row)

    def _candidate_row(self, binding_id: str, candidate_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM canonical_match_candidates "
            "WHERE account_binding_id = ? AND candidate_id = ?",
            (binding_id, candidate_id),
        ).fetchone()
        if row is None:
            raise Phase3StateError("account-scoped match candidate was not found")
        return row

    @staticmethod
    def _candidate(row: sqlite3.Row) -> MatchCandidate:
        return MatchCandidate(
            row["candidate_id"], row["account_binding_id"], row["identity_id"],
            row["canonical_track_id"], MatchCandidateAction(row["action"]),
            MatchCandidateState(row["state"]), row["confidence"],
            tuple(json.loads(row["basis_json"])), row["matcher_version"],
        )


_VERSION_WORDS = (
    "live", "acoustic", "remix", "remaster", "edit", "explicit", "clean", "instrumental"
)


def _version_from_title(title: str) -> str | None:
    normalized = normalize_identity_text(title)
    words = tuple(word for word in _VERSION_WORDS if word in normalized.split())
    return " ".join(words) if words else None


def _versions_conflict(left: str | None, right: str | None) -> bool:
    return bool(left and right and normalize_identity_text(left) != normalize_identity_text(right))
